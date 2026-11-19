"""vdi-tool web API and GUI server."""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles

from . import deploy, runs
from .deploy import state
from .pve import PVE, PVEError

ROOT = Path(__file__).resolve().parent.parent
pve = PVE()


def _version() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "dev"


VERSION = _version()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await pve.close()


app = FastAPI(title="vdi-tool", lifespan=lifespan)


async def _call(coro):
    try:
        return await coro
    except PVEError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/api/health")
async def health():
    out = {"version": VERSION, "configured": pve.configured, "pve_host": pve.host}
    if pve.configured:
        try:
            v = await pve.get("/version")
            out["pve_version"] = v.get("version")
            out["connected"] = True
        except PVEError as e:
            out["connected"] = False
            out["error"] = str(e)
    return out


@app.get("/api/overview")
async def overview():
    """Everything the dashboard needs in one call."""
    resources, status = await _call(
        asyncio.gather(pve.get("/cluster/resources"), pve.get("/cluster/status"))
    )
    cluster = next((s for s in status if s.get("type") == "cluster"), None)

    nodes, storage, guests = [], [], []
    for r in resources:
        t = r.get("type")
        if t == "node":
            nodes.append({
                "name": r["node"], "status": r.get("status"),
                "cpu": r.get("cpu", 0), "maxcpu": r.get("maxcpu", 0),
                "mem": r.get("mem", 0), "maxmem": r.get("maxmem", 0),
                "uptime": r.get("uptime", 0),
            })
        elif t == "storage":
            storage.append({
                "id": r["storage"], "node": r["node"], "type": r.get("plugintype"),
                "status": r.get("status"), "shared": bool(r.get("shared")),
                "content": r.get("content", ""),
                "used": r.get("disk", 0), "total": r.get("maxdisk", 0),
            })
        elif t in ("qemu", "lxc"):
            guests.append({
                "vmid": r["vmid"], "name": r.get("name", ""), "type": t,
                "node": r["node"], "status": r.get("status"),
                "template": bool(r.get("template")),
                "cpu": r.get("cpu", 0), "maxcpu": r.get("maxcpu", 0),
                "mem": r.get("mem", 0), "maxmem": r.get("maxmem", 0),
                "netin": r.get("netin", 0), "netout": r.get("netout", 0),
                "diskread": r.get("diskread", 0), "diskwrite": r.get("diskwrite", 0),
                "tags": r.get("tags", ""),
            })

    # Per-node bridges.
    async def bridges(node: str):
        try:
            nets = await pve.get(f"/nodes/{node}/network")
        except PVEError:
            return []
        return [
            {"node": node, "iface": n["iface"], "type": n.get("type"),
             "cidr": n.get("cidr", ""), "ports": n.get("bridge_ports", ""),
             "active": bool(n.get("active")), "comments": n.get("comments", "")}
            for n in nets if n.get("type") in ("bridge", "OVSBridge")
        ]

    online = [n["name"] for n in nodes if n["status"] == "online"]
    net_lists = await asyncio.gather(*(bridges(n) for n in online))
    networks = [b for lst in net_lists for b in lst]

    return {
        "cluster": {"name": cluster["name"], "quorate": bool(cluster.get("quorate"))} if cluster else None,
        "nodes": sorted(nodes, key=lambda n: n["name"]),
        "storage": sorted(storage, key=lambda s: (s["node"], s["id"])),
        "networks": networks,
        "guests": sorted(guests, key=lambda g: g["vmid"]),
    }


# ------------------------------------------------------------- cloud-init

def _ci_base(request: Request) -> str:
    return os.environ.get("VDI_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


@app.get("/ci/{key}/meta-data", response_class=PlainTextResponse)
async def ci_meta(key: str):
    return deploy.ci_meta(key)


@app.get("/ci/{key}/user-data", response_class=PlainTextResponse)
async def ci_user(key: str):
    return deploy.ci_user(key)


@app.get("/ci/{key}/vendor-data", response_class=PlainTextResponse)
async def ci_vendor(key: str):
    return ""


@app.post("/ci/{key}/done")
async def ci_done(key: str):
    deploy.ci_done(key)
    return {"ok": True}


# -------------------------------------------------------------- templates

class TemplateReq(BaseModel):
    os: str = Field("linux", pattern="^(linux|windows)$")
    win_iso: str | None = Field(None, pattern=r"^[\w.-]+:iso/[\w. ()-]+\.iso$")
    virtio_iso: str | None = Field(None, pattern=r"^[\w.-]+:iso/[\w. ()-]+\.iso$")
    image: str = "ubuntu-24.04"
    name: str = Field("vdi-linux-golden", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,40}$")
    node: str
    storage: str
    bridge: str
    cores: int = Field(4, ge=1, le=64)
    memory: int = Field(4096, ge=1024, le=262144)
    disk_gb: int = Field(32, ge=8, le=1024)
    password: str = Field(min_length=4, max_length=64)


def _public(t: dict) -> dict:
    return {k: v for k, v in t.items() if k not in ("password", "pwhash")}


@app.get("/api/deploy")
async def deploy_state():
    res = await _call(pve.get("/cluster/resources", type="vm"))
    live = {int(r["vmid"]): r for r in res}
    templates = {k: _public(t) for k, t in state.data["templates"].items()}
    for k, t in templates.items():
        t["exists"] = t["vmid"] in live
    # Templates made outside the tool can be used for batches too.
    for vmid, r in live.items():
        if r.get("template") and str(vmid) not in templates:
            templates[str(vmid)] = {"vmid": vmid, "name": r.get("name", ""), "node": r["node"],
                                    "status": "ready", "external": True, "exists": True}
    batches = []
    for bid, b in state.data["batches"].items():
        counts: dict[str, int] = {}
        for vm in b["vms"]:
            s = vm["status"]
            if s in ("created", "running") and vm["vmid"] in live:
                s = live[vm["vmid"]].get("status", s)
            counts[s] = counts.get(s, 0) + 1
        errors = [f"{vm['name']}: {vm['error']}" for vm in b["vms"] if vm.get("error")][:5]
        batches.append({**{k: v for k, v in b.items() if k not in ("vms", "pwhash")}, "custom_password": bool(b.get("pwhash")), "id": bid,
                        "count": len(b["vms"]), "counts": counts, "errors": errors,
                        "first": b["vms"][0]["vmid"], "last": b["vms"][-1]["vmid"]})
    isos = []
    try:
        node_names = [n["node"] for n in await pve.get("/nodes") if n.get("status") == "online"]
        for st in await pve.get("/storage"):
            if "iso" in st.get("content", ""):
                for n in node_names:
                    for c in await pve.get(f"/nodes/{n}/storage/{st['storage']}/content", content="iso"):
                        isos.append({"node": n, "volid": c["volid"], "size": c.get("size", 0)})
    except PVEError:
        pass  # ISO listing is a convenience; the page still works without it
    return {"isos": isos, "images": {k: v["label"] for k, v in deploy.IMAGES.items()},
            "templates": sorted(templates.values(), key=lambda t: t["vmid"]),
            "batches": sorted(batches, key=lambda b: b["created"], reverse=True)}


@app.post("/api/templates")
async def create_template(req: TemplateReq, request: Request):
    if req.os == "windows" and not (req.win_iso and req.virtio_iso):
        raise HTTPException(400, "pick the Windows ISO and the VirtIO driver ISO")
    if req.os == "linux" and req.image not in deploy.IMAGES:
        raise HTTPException(400, "unknown image")
    used = await _call(deploy.used_vmids(pve))
    vmid = deploy.free_ids(used | {int(k) for k in state.data["templates"]}, 9000, 1)[0]
    state.data["templates"][str(vmid)] = {
        **req.model_dump(exclude={"password"}), "pwhash": deploy.hash_pw(req.password), "vmid": vmid, "status": "building", "step": "queued",
        "created": time.time(), "ci_base": _ci_base(request)}
    if req.os == "windows":
        deploy._secrets[str(vmid)] = req.password  # needed in the answer file; never written to state
    state.save()
    deploy.spawn(deploy.build_template(pve, str(vmid)))
    return {"vmid": vmid}


@app.delete("/api/templates/{vmid}")
async def delete_template(vmid: int):
    t = state.data["templates"].get(str(vmid))
    if not t:
        raise HTTPException(404, "not a template built by this tool")
    if t["status"] == "building":
        raise HTTPException(409, "template is still building")
    if vmid in await _call(deploy.used_vmids(pve)):
        node = t["node"]
        cfg = await _call(pve.get(f"/nodes/{node}/qemu/{vmid}/config"))
        if deploy.TEMPLATE_TAG not in cfg.get("tags", ""):
            raise HTTPException(409, "VM no longer carries the vdi-template tag; left untouched")
        st = await _call(pve.get(f"/nodes/{node}/qemu/{vmid}/status/current"))
        if st.get("status") != "stopped":
            await _call(pve.wait_task(node, await _call(pve.post(f"/nodes/{node}/qemu/{vmid}/status/stop"))))
        upid = await _call(pve.delete(f"/nodes/{node}/qemu/{vmid}", purge=1))
        await _call(pve.wait_task(node, upid))
    if t.get("win_iso"):
        # A failed Windows build can leave its answer-file ISO (which holds the password) behind.
        store = t["win_iso"].split(":")[0]
        try:
            await pve.delete(f"/nodes/{t['node']}/storage/{store}/content/{store}:iso/vdi-unattend-{vmid}.iso")
        except PVEError:
            pass
    del state.data["templates"][str(vmid)]
    state.data["hosts"].pop(f"t{vmid}", None)
    state.save()
    return {"ok": True}


# ---------------------------------------------------------------- batches

class BatchReq(BaseModel):
    template: int
    count: int = Field(ge=1, le=2000)
    prefix: str = Field("vdi", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,30}$")
    storage: str | None = None       # a different storage always means full clones
    full: bool = False               # full clones even on the template's own storage
    nodes: list[str] = []            # extra target nodes (shared storage only)
    cores: int | None = Field(None, ge=1, le=128)
    memory: int | None = Field(None, ge=512, le=1048576)
    disk_gb: int | None = Field(None, ge=8, le=4096)  # grow each desktop's disk to this size
    fill: bool = False               # after deploy, fill each desktop's disk with real data
    fill_gb: int | None = Field(None, ge=1, le=4000)
    password: str | None = Field(None, min_length=4, max_length=64)  # blank keeps the template's
    start: bool = True
    concurrency: int = Field(4, ge=1, le=32)


@app.post("/api/batches")
async def create_batch(req: BatchReq, request: Request):
    res = await _call(pve.get("/cluster/resources"))
    tmpl = next((r for r in res if r.get("type") == "qemu" and int(r["vmid"]) == req.template), None)
    if not tmpl or not tmpl.get("template"):
        raise HTTPException(400, "template not found")
    node = tmpl["node"]
    cfg = await _call(pve.get(f"/nodes/{node}/qemu/{req.template}/config"))
    disk = next((v for k, v in cfg.items() if k in ("scsi0", "virtio0", "sata0", "ide0") and isinstance(v, str)), "")
    src_storage = disk.split(":")[0]
    full = req.full or bool(req.storage and req.storage != src_storage)
    target_storage = req.storage or src_storage
    shared = any(r.get("type") == "storage" and r.get("storage") == target_storage and r.get("shared") for r in res)
    nodes = [n for n in req.nodes if n] or [node]
    if nodes != [node] and not shared:
        raise HTTPException(400, f"storage {target_storage} is local to {node}; other nodes need shared storage")
    fill_gb = None
    if req.fill:
        if not req.fill_gb:
            raise HTTPException(400, "say how many GB to fill per desktop")
        if not req.start:
            raise HTTPException(400, "filling needs the desktops started after creating")
        size = req.disk_gb or int((re.search(r"size=(\d+)G", disk) or [0, 0])[1])
        if size and req.fill_gb > size - 10:
            raise HTTPException(400, f"each desktop's disk is {size} GB and the system needs about 10 GB; "
                                     f"fill at most {max(1, size - 10)} GB or raise Disk GB")
        await _check_capacity(target_storage, node, req.count, req.fill_gb + 8)
        fill_gb = req.fill_gb
    used = {int(r["vmid"]) for r in res if r.get("type") in ("qemu", "lxc")}
    # No await between here and saving the batch, so two requests cannot pick the same IDs.
    ids = deploy.free_ids(used | deploy.reserved_ids(), 1000, req.count)
    bid = deploy.new_id()
    width = max(3, len(str(req.count)))
    vms = [{"vmid": vmid, "name": f"{req.prefix}-{bid}-{i + 1:0{width}d}", "node": nodes[i % len(nodes)],
            "status": "queued"} for i, vmid in enumerate(ids)]
    state.data["batches"][bid] = {
        "template": req.template, "template_name": tmpl.get("name", ""), "template_node": node,
        "storage": target_storage, "full": full, "cores": req.cores, "memory": req.memory, "disk_gb": req.disk_gb, "fill_gb": fill_gb,
        "disk_key": next((k for k in ("scsi0", "virtio0", "sata0", "ide0") if isinstance(cfg.get(k), str)), "scsi0"),
        "start": req.start, "concurrency": req.concurrency, "prefix": req.prefix,
        "pwhash": deploy.hash_pw(req.password) if req.password else None,
        "os": "windows" if str(cfg.get("ostype", "")).startswith("win") else "linux",
        "status": "deploying", "created": time.time(), "ci_base": _ci_base(request), "vms": vms}
    state.save()
    deploy.spawn(deploy.deploy_batch(pve, bid))
    return {"id": bid}


class PasswordReq(BaseModel):
    password: str = Field(min_length=5, max_length=64)


@app.post("/api/batches/{bid}/password")
async def set_batch_password(bid: str, req: PasswordReq):
    """Set the vdi user's password on every running desktop through the guest agent."""
    b = state.data["batches"].get(bid)
    if not b or b["status"] not in ("ready", "partial"):
        raise HTTPException(400, "batch not found or not ready")
    sem = asyncio.Semaphore(8)

    async def one(vm: dict) -> str | None:
        async with sem:
            try:
                await pve.request("POST", f"/nodes/{vm['node']}/qemu/{vm['vmid']}/agent/set-user-password",
                                  username="vdi", password=req.password)
                return None
            except PVEError as e:
                return f"{vm['name']}: {e}"

    results = await asyncio.gather(*(one(vm) for vm in b["vms"] if vm["status"] in ("running", "created")))
    errors = [r for r in results if r]
    if b.get("os") != "windows":
        # Desktops that have not booted yet pick this up from cloud-init.
        b["pwhash"] = deploy.hash_pw(req.password)
        for vm in b["vms"]:
            state.data["pw"][f"c{vm['vmid']}"] = b["pwhash"]
        state.save()
    return {"ok": len(results) - len(errors), "failed": len(errors), "errors": errors[:5]}


@app.delete("/api/batches/{bid}")
async def remove_batch(bid: str):
    b = state.data["batches"].get(bid)
    if not b:
        raise HTTPException(404, "batch not found")
    if b["status"] in ("deploying", "deleting"):
        raise HTTPException(409, f"batch is {b['status']}")
    act = runs.active()
    if act and act["batch"] == bid:
        raise HTTPException(409, "a benchmark is running on this batch")
    b["status"] = "deleting"
    state.save()
    deploy.spawn(deploy.delete_batch(pve, bid))
    return {"ok": True}


# ------------------------------------------------------------- benchmarks

_BLOB = os.urandom(1 << 20)


@app.get("/agent/workload.py", response_class=PlainTextResponse)
async def agent_script():
    return (ROOT / "agent" / "workload.py").read_text()


@app.get("/agent/blob")
async def agent_blob():
    """1 MB of incompressible data for the workload's download action."""
    return Response(_BLOB, media_type="application/octet-stream")


async def _check_capacity(storage: str, node: str, desktops: int, gb_each: int) -> None:
    """A thin pool that reaches 100% freezes every desktop on it, so never plan past 90%."""
    res = await _call(pve.get("/cluster/resources", type="storage"))
    st = next((x for x in res if x.get("storage") == storage and x.get("node") == node), None)
    if st and st.get("maxdisk"):
        after = st.get("disk", 0) + desktops * gb_each * (1 << 30)
        if after > 0.9 * st["maxdisk"]:
            room = max(0, int((0.9 * st["maxdisk"] - st.get("disk", 0)) / max(1, desktops) / (1 << 30)))
            raise HTTPException(400, f"{desktops} desktops x {gb_each} GB would take storage {storage} past 90% full. "
                                     f"The most that fits is {room} GB per desktop.")


def _fill_after_deploy(bid: str) -> None:
    """Batches deployed with the fill option start a Fill disk run as soon as they are up."""
    b = state.data["batches"].get(bid)
    if not b or not b.get("fill_gb") or not b.get("start") or b.get("os") == "windows" or runs.active():
        return
    r = runs.create(bid, "fill", max(900, b["fill_gb"] * 40), 0, b["ci_base"], b["fill_gb"])
    if r["vms"]:
        deploy.spawn(runs.execute(pve, r))
    else:
        runs.delete(r["id"])


deploy.on_batch_ready.append(_fill_after_deploy)


class RunReq(BaseModel):
    batch: str
    profile: str = "office"
    duration: int = Field(120, ge=10, le=3600)
    ramp: int = Field(0, ge=0, le=3600)
    size_gb: int = Field(1, ge=1, le=2000)  # per desktop: fio file size, or data to write for Fill disk


@app.get("/api/runs")
async def list_runs():
    profs = runs.all_profiles()
    return {"profiles": {k: v["name"] for k, v in profs.items()},
            "kinds": {k: v["kind"] for k, v in profs.items()},
            "custom": [k for k, v in profs.items() if v.get("custom")],
            "runs": sorted((runs.brief(r) for r in runs.runs.values()), key=lambda r: r["created"], reverse=True)}


class ProfileReq(BaseModel):
    name: str = Field(min_length=3, max_length=80)
    rw: str = Field(pattern="^(randrw|rw|randread|randwrite|read|write)$")
    bs: str = Field("4k", pattern=r"^\d{1,4}[kKmM]$")
    iodepth: int = Field(16, ge=1, le=1024)
    numjobs: int = Field(1, ge=1, le=64)
    rwmixread: int = Field(70, ge=0, le=100)
    rate_iops: int | None = Field(None, ge=1, le=10_000_000)


@app.post("/api/profiles")
async def add_profile(req: ProfileReq):
    """Save a custom fio workload so it can be picked for runs."""
    custom = runs.custom_profiles()
    pid = "c-" + deploy.new_id()
    fio = req.model_dump(exclude={"name"}, exclude_none=True)
    custom[pid] = {"name": req.name, "kind": "fio", "fio": fio}
    runs.save_custom(custom)
    return {"id": pid}


@app.delete("/api/profiles/{pid}")
async def delete_profile(pid: str):
    custom = runs.custom_profiles()
    if pid not in custom:
        raise HTTPException(404, "not a custom workload")
    del custom[pid]
    runs.save_custom(custom)
    return {"ok": True}


@app.get("/api/runs/{rid}")
async def get_run(rid: str):
    r = runs.runs.get(rid)
    if not r:
        raise HTTPException(404, "run not found")
    return r


@app.post("/api/runs")
async def start_run(req: RunReq, request: Request):
    prof = runs.all_profiles().get(req.profile)
    if not prof:
        raise HTTPException(400, "unknown workload")
    b = state.data["batches"].get(req.batch)
    if not b or b["status"] not in ("ready", "partial"):
        raise HTTPException(400, "batch not found or not ready")
    if b.get("os") == "windows" and prof["kind"] != "bootstorm":
        raise HTTPException(400, "Windows desktops only support the boot storm run for now")
    if runs.active():
        raise HTTPException(409, "another run is in progress")
    if prof["kind"] in ("fill", "fio"):
        n = sum(vm["status"] in ("running", "created") for vm in b["vms"])
        await _check_capacity(b["storage"], b["template_node"], n, req.size_gb)
    r = runs.create(req.batch, req.profile, req.duration, req.ramp, _ci_base(request), req.size_gb, await _env(b))
    if not r["vms"]:
        runs.delete(r["id"])
        raise HTTPException(400, "the batch has no running desktops")
    deploy.spawn(runs.execute(pve, r))
    return {"id": r["id"]}


async def _env(b: dict) -> dict:
    """Snapshot of the host and storage at run time, so a report still reads right months later."""
    env: dict = {"tool_version": VERSION, "pve_host": pve.host, "node": b["template_node"]}
    try:
        env["pve_version"] = (await pve.get("/version")).get("version")
        st = await pve.get(f"/nodes/{b['template_node']}/status")
        cpu = st.get("cpuinfo", {})
        env.update(cpu_model=cpu.get("model"), cpus=cpu.get("cpus"), sockets=cpu.get("sockets"),
                   memory_gb=round(st.get("memory", {}).get("total", 0) / 2**30), kernel=st.get("kversion", "").split(" #")[0])
        sc = await pve.get(f"/storage/{b['storage']}")
        env.update(storage_type=sc.get("type"), storage_target=sc.get("server") or sc.get("portal") or sc.get("vgname") or sc.get("pool") or sc.get("path"))
        ss = await pve.get(f"/nodes/{b['template_node']}/storage/{b['storage']}/status")
        env.update(storage_total_gb=round(ss.get("total", 0) / 2**30), storage_used_gb=round(ss.get("used", 0) / 2**30))
    except PVEError:
        pass  # a report with fewer environment facts is better than no run
    return env


class RunNote(BaseModel):
    note: str = Field("", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=12)


@app.patch("/api/runs/{rid}")
async def annotate_run(rid: str, req: RunNote):
    r = runs.runs.get(rid)
    if not r:
        raise HTTPException(404, "run not found")
    r["note"] = req.note.strip()
    r["tags"] = [t.strip()[:40] for t in req.tags if t.strip()]
    runs.save(r)
    return {"ok": True}


@app.get("/api/report")
async def report_data(runs_: str = Query(alias="runs")):
    out = []
    for rid in [x for x in runs_.split(",") if x][:12]:
        r = runs.runs.get(rid)
        if not r:
            raise HTTPException(404, f"run {rid} not found")
        out.append(runs.report_data(r))
    return {"tool_version": VERSION, "generated": time.time(), "runs": out}


def _download(body: str, name: str) -> Response:
    return Response(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/runs.csv")
async def all_runs_csv():
    return _download(runs.runs_csv(), "vdi-tool-runs.csv")


@app.get("/api/runs/{rid}/desktops.csv")
async def run_csv(rid: str):
    r = runs.runs.get(rid)
    if not r:
        raise HTTPException(404, "run not found")
    return _download(runs.desktops_csv(r), f"vdi-tool-run-{rid}-desktops.csv")


@app.get("/report")
async def report_page():
    html = (ROOT / "web" / "report.html").read_text()
    for f in ("report.css", "report.js"):
        html = html.replace(f"/static/{f}", f"/static/{f}?v={VERSION}")
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/api/runs/{rid}/go")
async def run_go(rid: str):
    return runs.go(rid)


@app.post("/api/runs/{rid}/report/{vmid}")
async def run_report(rid: str, vmid: str, body: dict):
    runs.report(rid, vmid, body)
    return {"ok": True}


@app.delete("/api/runs/{rid}")
async def delete_run(rid: str):
    r = runs.runs.get(rid)
    if not r:
        raise HTTPException(404, "run not found")
    if r["status"] == "running":
        raise HTTPException(409, "run is in progress")
    runs.delete(rid)
    return {"ok": True}


# ----------------------------------------------------------------- update

@app.post("/api/update")
async def self_update():
    """Pull the latest revision of the current branch and restart."""
    cmd = ("cd /opt/vdi-tool && b=$(git rev-parse --abbrev-ref HEAD) && git fetch --depth 1 origin \"$b\" "
           "&& git checkout -B \"$b\" FETCH_HEAD && bash scripts/ct-setup.sh")
    try:
        subprocess.run(["systemd-run", "--unit", f"vdi-tool-update-{int(time.time())}", "bash", "-c", cmd],
                       check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HTTPException(500, f"could not start the update: {getattr(e, 'stderr', e)}") from e
    return {"ok": True}


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.get("/")
async def index():
    # Version the asset URLs so browsers never pair a new page with cached old scripts.
    html = (ROOT / "web" / "index.html").read_text()
    html = html.replace("/static/style.css", f"/static/style.css?v={VERSION}").replace("/static/app.js", f"/static/app.js?v={VERSION}")
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})
