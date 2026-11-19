"""Golden template builds, batch deploys and teardown.

The tool acts as the cloud-init server: every VM gets an SMBIOS serial of the
form ``ds=nocloud;s=http://<tool>/ci/<key>/`` so cloud-init fetches its
user-data and meta-data from this service. No snippets storage is needed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from .pve import PVE, PVEError

STATE_DIR = Path(os.environ.get("VDI_STATE_DIR", "/var/lib/vdi-tool"))
IMAGES = {
    "ubuntu-24.04": {
        "label": "Ubuntu 24.04 + XFCE",
        "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "file": "vdi-noble-server-cloudimg-amd64.qcow2",
    },
}
TEMPLATE_TAG = "vdi-template"
BUILD_TIMEOUT = 45 * 60


class State:
    def __init__(self) -> None:
        self.path = STATE_DIR / "state.json"
        self.data: dict[str, dict] = {"templates": {}, "batches": {}, "hosts": {}}
        try:
            self.data.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass
        # Anything in flight when the service stopped cannot resume.
        for t in self.data["templates"].values():
            if t["status"] == "building":
                t.update(status="failed", error="interrupted by a service restart")
        for b in self.data["batches"].values():
            if b["status"] in ("deploying", "deleting"):
                b.update(status="interrupted")

    def save(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
        except OSError:
            pass


state = State()
_tasks: set[asyncio.Task] = set()
_done_events: dict[str, asyncio.Event] = {}


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def smbios(ci_url: str) -> str:
    serial = base64.b64encode(f"ds=nocloud;s={ci_url}".encode()).decode()
    return f"uuid={uuid.uuid4()},base64=1,serial={serial}"


async def used_vmids(pve: PVE) -> set[int]:
    res = await pve.get("/cluster/resources", type="vm")
    return {int(r["vmid"]) for r in res}


def free_ids(used: set[int], start: int, count: int) -> list[int]:
    out, i = [], start
    while len(out) < count:
        if i not in used:
            out.append(i)
        i += 1
    return out


# ---------------------------------------------------------------- cloud-init

def ci_meta(key: str) -> str:
    name = state.data["hosts"].get(key, f"vdi-{key}")
    return f"instance-id: vdi-{key}\nlocal-hostname: {name}\n"


def ci_user(key: str) -> str:
    t = state.data["templates"].get(key[1:]) if key.startswith("t") else None
    if not t:
        # Clones: everything is already baked into the template.
        return "#cloud-config\nusers: []\nssh_pwauth: true\n"
    pw = json.dumps(t["password"])
    done = f"{t['ci_base']}/ci/{key}/done"
    return f"""#cloud-config
users:
  - name: vdi
    gecos: VDI User
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: false
chpasswd:
  expire: false
  users:
    - {{name: vdi, password: {pw}, type: text}}
ssh_pwauth: true
package_update: true
packages: [qemu-guest-agent, xfce4, xfce4-terminal, xrdp, dbus-x11, fio, curl]
runcmd:
  - systemctl enable qemu-guest-agent xrdp
  - adduser xrdp ssl-cert
  - echo xfce4-session > /home/vdi/.xsession
  - chown vdi:vdi /home/vdi/.xsession
  - curl -fsS -m 20 -X POST {done} || true
  - truncate -s 0 /etc/machine-id
  - rm -f /var/lib/dbus/machine-id
power_state:
  mode: poweroff
  timeout: 120
"""


def ci_done(key: str) -> None:
    ev = _done_events.get(key)
    if ev:
        ev.set()


# ------------------------------------------------------------------ templates

async def _ensure_image(pve: PVE, node: str, img: dict, log) -> str:
    """Make sure the base cloud image is on an 'import' storage; return its volid."""
    stores = await pve.get(f"/nodes/{node}/storage", enabled=1)
    store = next((s["storage"] for s in stores if "import" in s.get("content", "") and s.get("active")), None)
    if not store:
        cand = next((s for s in stores if s.get("type") in ("dir", "nfs", "cifs", "cephfs")), None)
        if not cand:
            raise PVEError("no file-based storage found to hold the base image")
        store = cand["storage"]
        log(f"enabling disk-image imports on storage {store}")
        await pve.put(f"/storage/{store}", content=cand.get("content", "") + ",import")
    volid = f"{store}:import/{img['file']}"
    have = await pve.get(f"/nodes/{node}/storage/{store}/content", content="import")
    if any(c["volid"] == volid for c in have):
        return volid
    log("downloading the base image")
    try:
        upid = await pve.post(f"/nodes/{node}/storage/{store}/download-url",
                              content="import", url=img["url"], filename=img["file"])
        await pve.wait_task(node, upid)
    except PVEError as e:
        log(f"host-side download failed ({e}); downloading through the tool instead")
        with tempfile.NamedTemporaryFile(dir=STATE_DIR if STATE_DIR.exists() else None) as f:
            async with httpx.AsyncClient(follow_redirects=True, timeout=None) as c:
                async with c.stream("GET", img["url"]) as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
            f.flush(); f.seek(0)
            upid = await pve.upload(node, store, "import", img["file"], f)
            await pve.wait_task(node, upid)
    return volid


async def build_template(pve: PVE, key: str) -> None:
    t = state.data["templates"][key]
    node, vmid = t["node"], t["vmid"]

    def log(msg: str) -> None:
        t["step"] = msg
        state.save()

    try:
        volid = await _ensure_image(pve, node, IMAGES[t["image"]], log)
        log("creating the VM")
        ci_key = f"t{vmid}"
        state.data["hosts"][ci_key] = t["name"]
        ev = _done_events[ci_key] = asyncio.Event()
        upid = await pve.request(
            "POST", f"/nodes/{node}/qemu", timeout=120,
            vmid=vmid, name=t["name"], memory=t["memory"], cores=t["cores"], cpu="host",
            ostype="l26", scsihw="virtio-scsi-single", agent="enabled=1", vga="virtio",
            net0=f"virtio,bridge={t['bridge']}",
            scsi0=f"{t['storage']}:0,import-from={volid},discard=on,ssd=1,iothread=1",
            boot="order=scsi0", tags=TEMPLATE_TAG, smbios1=smbios(f"{t['ci_base']}/ci/{ci_key}/"),
            description="Built by vdi-tool. User: vdi",
        )
        await pve.wait_task(node, upid)
        await pve.put(f"/nodes/{node}/qemu/{vmid}/resize", disk="scsi0", size=f"{t['disk_gb']}G")
        log("installing the desktop (takes several minutes)")
        await pve.wait_task(node, await pve.post(f"/nodes/{node}/qemu/{vmid}/status/start"))
        try:
            await asyncio.wait_for(ev.wait(), BUILD_TIMEOUT)
        except asyncio.TimeoutError:
            raise PVEError("the desktop install did not finish in 45 minutes; check the VM console "
                           "(it needs DHCP and internet access, and must reach this tool)")
        log("waiting for shutdown")
        for _ in range(120):
            st = await pve.get(f"/nodes/{node}/qemu/{vmid}/status/current")
            if st.get("status") == "stopped":
                break
            await asyncio.sleep(3)
        else:
            await pve.wait_task(node, await pve.post(f"/nodes/{node}/qemu/{vmid}/status/stop"))
        log("converting to a template")
        await pve.wait_task(node, await pve.post(f"/nodes/{node}/qemu/{vmid}/template"))
        t.update(status="ready", step="", finished=time.time())
    except Exception as e:  # noqa: BLE001 - surface every failure in the GUI
        t.update(status="failed", error=str(e))
    finally:
        _done_events.pop(f"t{vmid}", None)
        state.save()


# -------------------------------------------------------------------- batches

async def _retry(fn, tries: int = 4):
    for i in range(tries):
        try:
            return await fn()
        except PVEError as e:
            if i == tries - 1 or not any(w in str(e).lower() for w in ("lock", "timeout", "busy")):
                raise
            await asyncio.sleep(2 + i * 3)


async def deploy_batch(pve: PVE, bid: str) -> None:
    b = state.data["batches"][bid]
    sem = asyncio.Semaphore(b["concurrency"])
    src_node, tmpl = b["template_node"], b["template"]

    async def one(vm: dict) -> None:
        async with sem:
            vmid, node = vm["vmid"], vm["node"]
            try:
                vm["status"] = "cloning"

                async def clone():
                    upid = await pve.request(
                        "POST", f"/nodes/{src_node}/qemu/{tmpl}/clone", timeout=120,
                        newid=vmid, name=vm["name"], full=b["full"],
                        storage=b["storage"] if b["full"] else None,
                        target=node if node != src_node else None)
                    await pve.wait_task(src_node, upid)
                await _retry(clone)
                ci_key = f"c{vmid}"
                state.data["hosts"][ci_key] = vm["name"]
                await _retry(lambda: pve.put(
                    f"/nodes/{node}/qemu/{vmid}/config",
                    smbios1=smbios(f"{b['ci_base']}/ci/{ci_key}/"),
                    tags=f"vdi;vdi-batch-{bid}", cores=b.get("cores"), memory=b.get("memory")))
                vm["status"] = "created"
                if b["start"]:
                    await pve.wait_task(node, await pve.post(f"/nodes/{node}/qemu/{vmid}/status/start"))
                    vm["status"] = "running"
            except Exception as e:  # noqa: BLE001
                vm.update(status="failed", error=str(e))

    await asyncio.gather(*(one(vm) for vm in b["vms"]))
    b.update(status="ready", finished=time.time())
    state.save()


async def delete_batch(pve: PVE, bid: str) -> None:
    b = state.data["batches"][bid]
    sem = asyncio.Semaphore(8)
    existing = await used_vmids(pve)

    async def one(vm: dict) -> None:
        async with sem:
            vmid, node = vm["vmid"], vm["node"]
            try:
                if vmid in existing:
                    # Only ever remove VMs that still carry this batch's tag.
                    cfg = await pve.get(f"/nodes/{node}/qemu/{vmid}/config")
                    if f"vdi-batch-{bid}" not in cfg.get("tags", ""):
                        raise PVEError("VM no longer carries this batch's tag; left untouched")
                    st = await pve.get(f"/nodes/{node}/qemu/{vmid}/status/current")
                    if st.get("status") != "stopped":
                        await pve.wait_task(node, await pve.post(f"/nodes/{node}/qemu/{vmid}/status/stop"))
                    upid = await _retry(lambda: pve.delete(
                        f"/nodes/{node}/qemu/{vmid}", purge=1, **{"destroy-unreferenced-disks": 1}))
                    await pve.wait_task(node, upid)
                state.data["hosts"].pop(f"c{vmid}", None)
                vm["status"] = "deleted"
            except Exception as e:  # noqa: BLE001
                vm.update(status="failed", error=str(e))

    await asyncio.gather(*(one(vm) for vm in b["vms"]))
    if all(vm["status"] == "deleted" for vm in b["vms"]):
        del state.data["batches"][bid]
    else:
        b["status"] = "delete-incomplete"
    state.save()


def new_id() -> str:
    return uuid.uuid4().hex[:6]
