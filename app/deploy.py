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
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from . import windows
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
FILE_STORES = ("dir", "nfs", "cifs", "glusterfs", "cephfs", "btrfs")
BUILD_TIMEOUT = 45 * 60


class State:
    def __init__(self) -> None:
        self.path = STATE_DIR / "state.json"
        self.data: dict[str, dict] = {"templates": {}, "batches": {}, "hosts": {}, "pw": {}}
        try:
            self.data.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass
        # Anything in flight when the service stopped cannot resume.
        for t in self.data["templates"].values():
            if t["status"] == "building":
                t.update(status="failed", error="interrupted by a service restart")
            t.pop("password", None)
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


def hash_pw(password: str) -> str:
    """SHA-512 crypt hash, so the plain password is never stored or served."""
    return subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password, text=True,
                          capture_output=True, check=True).stdout.strip()


state = State()
_tasks: set[asyncio.Task] = set()
on_batch_ready: list = []  # callbacks run with the batch id once a deploy finishes
_done_events: dict[str, asyncio.Event] = {}
_secrets: dict[str, str] = {}  # plain passwords needed during a Windows build; memory only


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
    if not t or "pwhash" not in t:
        # Clones: everything is baked into the template; a batch may set its own password.
        out = "#cloud-config\nusers: []\nssh_pwauth: true\n"
        h = state.data["pw"].get(key)
        if h:
            out += f"chpasswd:\n  expire: false\n  users:\n    - {{name: vdi, password: {json.dumps(h)}}}\n"
        return out
    pw = json.dumps(t["pwhash"])
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
    - {{name: vdi, password: {pw}}}
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

_image_lock = asyncio.Lock()


async def _ensure_image(pve: PVE, node: str, img: dict, log) -> str:
    """Make sure the base cloud image is on an 'import' storage; return its volid."""
    log("waiting for the base image")
    async with _image_lock:  # concurrent builds share one download
        return await _ensure_image_locked(pve, node, img, log)


async def _ensure_image_locked(pve: PVE, node: str, img: dict, log) -> str:
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
        if "403" not in str(e):
            raise
        # The API token lacks Sys.AccessNetwork; fetch the image here and upload it.
        log("downloading the base image through the tool (slower)")
        with tempfile.NamedTemporaryFile(dir=STATE_DIR if STATE_DIR.exists() else None) as f:
            async with httpx.AsyncClient(follow_redirects=True, timeout=None) as c:
                async with c.stream("GET", img["url"]) as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
            f.flush(); f.seek(0)
            log("uploading the base image to the host")
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
        if t.get("os") == "windows":
            await _build_windows(pve, t, log)
            t.update(status="ready", step="", finished=time.time())
            return
        volid = await _ensure_image(pve, node, IMAGES[t["image"]], log)
        log("creating the VM")
        # File-based storage (NFS, directory) needs qcow2 disks for snapshots and linked clones.
        st = await pve.get(f"/storage/{t['storage']}")
        fmt = ",format=qcow2" if st.get("type") in FILE_STORES else ""
        ci_key = f"t{vmid}"
        state.data["hosts"][ci_key] = t["name"]
        ev = _done_events[ci_key] = asyncio.Event()
        upid = await pve.request(
            "POST", f"/nodes/{node}/qemu", timeout=120,
            vmid=vmid, name=t["name"], memory=t["memory"], cores=t["cores"], cpu="host",
            ostype="l26", scsihw="virtio-scsi-single", agent="enabled=1", vga="virtio",
            net0=f"virtio,bridge={t['bridge']}",
            scsi0=f"{t['storage']}:0,import-from={volid},discard=on,ssd=1,iothread=1{fmt}",
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
        _secrets.pop(key, None)
        t.pop("pwhash", None)  # only needed while the build runs
        _done_events.pop(f"t{vmid}", None)
        state.save()


async def _build_windows(pve: PVE, t: dict, log) -> None:
    node, vmid, storage = t["node"], t["vmid"], t["storage"]
    ci_key = f"t{vmid}"
    iso_store = t["win_iso"].split(":")[0]
    answer_name = f"vdi-unattend-{vmid}.iso"
    answer_vol = f"{iso_store}:iso/{answer_name}"
    ev = _done_events[ci_key] = asyncio.Event()
    try:
        st = await pve.get(f"/storage/{storage}")
        fmt = ",format=qcow2" if st.get("type") in FILE_STORES else ""
        log("preparing the answer file")
        iso = windows.make_iso(
            windows.unattend_xml(t["name"][:15], _secrets[str(vmid)]),
            windows.setup_ps1(f"{t['ci_base']}/ci/{ci_key}/done"))
        try:
            with open(iso, "rb") as f:
                await pve.wait_task(node, await pve.upload(node, iso_store, "iso", answer_name, f))
        finally:
            for p in (iso, iso.parent / "src" / "autounattend.xml", iso.parent / "src" / "vdi-setup.ps1"):
                p.unlink(missing_ok=True)
        log("creating the VM")
        upid = await pve.request(
            "POST", f"/nodes/{node}/qemu", timeout=120,
            vmid=vmid, name=t["name"], memory=t["memory"], cores=t["cores"], cpu="host",
            ostype="win11", machine="q35", bios="ovmf", scsihw="virtio-scsi-single", agent="enabled=1",
            efidisk0=f"{storage}:1,efitype=4m,pre-enrolled-keys=1", tpmstate0=f"{storage}:1,version=v2.0",
            scsi0=f"{storage}:{t['disk_gb']},discard=on,ssd=1,iothread=1{fmt}",
            net0=f"virtio,bridge={t['bridge']}",
            ide2=f"{t['win_iso']},media=cdrom", sata0=f"{t['virtio_iso']},media=cdrom",
            sata1=f"{answer_vol},media=cdrom", boot="order=scsi0;ide2", tags=TEMPLATE_TAG,
            description="Built by vdi-tool. User: vdi")
        await pve.wait_task(node, upid)
        log("installing Windows (20 to 40 minutes)")
        await pve.wait_task(node, await pve.post(f"/nodes/{node}/qemu/{vmid}/status/start"))
        await windows.press_any_key(pve, node, vmid)
        try:
            await asyncio.wait_for(ev.wait(), 120 * 60)
        except asyncio.TimeoutError:
            raise PVEError("Windows setup did not finish in 2 hours; open the VM console to see where it stopped")
        log("waiting for shutdown")
        for _ in range(200):
            st = await pve.get(f"/nodes/{node}/qemu/{vmid}/status/current")
            if st.get("status") == "stopped":
                break
            await asyncio.sleep(3)
        else:
            raise PVEError("Windows reported finished but did not shut down")
        log("converting to a template")
        await pve.put(f"/nodes/{node}/qemu/{vmid}/config", delete="ide2,sata0,sata1", boot="order=scsi0")
        await pve.wait_task(node, await pve.post(f"/nodes/{node}/qemu/{vmid}/template"))
    finally:
        _done_events.pop(ci_key, None)
        try:  # the answer file holds the password; never leave it on the host
            cfg = await pve.get(f"/nodes/{node}/qemu/{vmid}/config") if vmid in await used_vmids(pve) else {}
            if answer_vol not in str(cfg.get("sata1", "")):
                await pve.delete(f"/nodes/{node}/storage/{iso_store}/content/{answer_vol}")
        except PVEError:
            pass


# -------------------------------------------------------------------- batches

# Transient Proxmox failures: lock contention, or a task that died as it was forked.
RETRYABLE = ("lock", "timeout", "busy", "worker", "unexpected status")


async def _retry(fn, tries: int = 5):
    for i in range(tries):
        try:
            return await fn()
        except PVEError as e:
            if i == tries - 1 or not any(w in str(e).lower() for w in RETRYABLE):
                raise
            await asyncio.sleep(2 + i * 4)


async def deploy_batch(pve: PVE, bid: str) -> None:
    b = state.data["batches"][bid]
    sem = asyncio.Semaphore(b["concurrency"])
    src_node, tmpl = b["template_node"], b["template"]

    async def one(vm: dict) -> None:
        async with sem:
            vmid, node = vm["vmid"], vm["node"]
            try:
                vm["status"] = "cloning"

                attempts = 0

                async def clone():
                    nonlocal attempts
                    attempts += 1
                    if attempts > 1 and vmid in await used_vmids(pve):
                        # An earlier attempt left a half-made VM behind; clear it if it is ours.
                        cfg = await pve.get(f"/nodes/{src_node}/qemu/{vmid}/config")
                        if cfg.get("name") != vm["name"]:
                            raise PVEError(f"VM {vmid} already exists and is not part of this batch")
                        await pve.wait_task(src_node, await pve.delete(f"/nodes/{src_node}/qemu/{vmid}", purge=1))
                    upid = await pve.request(
                        "POST", f"/nodes/{src_node}/qemu/{tmpl}/clone", timeout=120,
                        newid=vmid, name=vm["name"], full=b["full"],
                        storage=b["storage"] if b["full"] else None,
                        target=node if node != src_node else None)
                    await pve.wait_task(src_node, upid)
                await _retry(clone)
                ci_key = f"c{vmid}"
                state.data["hosts"][ci_key] = vm["name"]
                if b.get("pwhash"):
                    state.data["pw"][ci_key] = b["pwhash"]
                await _retry(lambda: pve.put(
                    f"/nodes/{node}/qemu/{vmid}/config",
                    smbios1=None if b.get("os") == "windows" else smbios(f"{b['ci_base']}/ci/{ci_key}/"),
                    tags=f"vdi;vdi-batch-{bid}", cores=b.get("cores"), memory=b.get("memory")))
                if b.get("disk_gb"):
                    # Growing only; the guest expands its filesystem on first boot.
                    cur = await pve.get(f"/nodes/{node}/qemu/{vmid}/config")
                    m = re.search(r"size=(\d+)G", str(cur.get(b.get("disk_key", "scsi0"), "")))
                    if m and int(m.group(1)) < b["disk_gb"]:
                        await _retry(lambda: pve.put(f"/nodes/{node}/qemu/{vmid}/resize",
                                                     disk=b.get("disk_key", "scsi0"), size=f"{b['disk_gb']}G"))
                vm["status"] = "created"
                if b["start"]:
                    async def start():
                        st = await pve.get(f"/nodes/{node}/qemu/{vmid}/status/current")
                        if st.get("status") != "running":
                            await pve.wait_task(node, await pve.post(f"/nodes/{node}/qemu/{vmid}/status/start"))
                    await _retry(start)
                    vm["status"] = "running"
            except Exception as e:  # noqa: BLE001
                vm.update(status="failed", error=str(e))

    await asyncio.gather(*(one(vm) for vm in b["vms"]))
    failed = any(vm["status"] == "failed" for vm in b["vms"])
    b.update(status="partial" if failed else "ready", finished=time.time())
    state.save()
    for hook in on_batch_ready:
        try:
            hook(bid)
        except Exception:  # noqa: BLE001 - a follow-up action must not undo a finished deploy
            pass


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
                        # Not ours (for example the clone failed because the ID was taken): leave it alone.
                        vm["status"] = "deleted"
                        return
                    async def stop():
                        st = await pve.get(f"/nodes/{node}/qemu/{vmid}/status/current")
                        if st.get("status") != "stopped":
                            await pve.wait_task(node, await pve.post(f"/nodes/{node}/qemu/{vmid}/status/stop"))

                    async def destroy():
                        if vmid in await used_vmids(pve):
                            await pve.wait_task(node, await pve.delete(
                                f"/nodes/{node}/qemu/{vmid}", purge=1, **{"destroy-unreferenced-disks": 1}))
                    await _retry(stop)
                    await _retry(destroy)
                state.data["hosts"].pop(f"c{vmid}", None)
                state.data["pw"].pop(f"c{vmid}", None)
                vm["status"] = "deleted"
            except Exception as e:  # noqa: BLE001
                vm.update(status="failed", error=str(e))

    await asyncio.gather(*(one(vm) for vm in b["vms"]))
    if all(vm["status"] == "deleted" for vm in b["vms"]):
        del state.data["batches"][bid]
    else:
        b["status"] = "delete-incomplete"
    state.save()


def reserved_ids() -> set[int]:
    """IDs promised to batches that are still deploying."""
    return {vm["vmid"] for b in state.data["batches"].values() if b["status"] == "deploying" for vm in b["vms"]}


def new_id() -> str:
    return uuid.uuid4().hex[:6]
