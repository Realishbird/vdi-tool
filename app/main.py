"""vdi-tool web API and GUI server."""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
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
    storage: str | None = None       # set to a different storage for full clones
    nodes: list[str] = []            # extra target nodes (shared storage only)
    cores: int | None = Field(None, ge=1, le=128)
    memory: int | None = Field(None, ge=512, le=1048576)
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
    full = bool(req.storage and req.storage != src_storage)
    target_storage = req.storage or src_storage
    shared = any(r.get("type") == "storage" and r.get("storage") == target_storage and r.get("shared") for r in res)
    nodes = [n for n in req.nodes if n] or [node]
    if nodes != [node] and not shared:
        raise HTTPException(400, f"storage {target_storage} is local to {node}; other nodes need shared storage")
    used = {int(r["vmid"]) for r in res if r.get("type") in ("qemu", "lxc")}
    # No await between here and saving the batch, so two requests cannot pick the same IDs.
    ids = deploy.free_ids(used | deploy.reserved_ids(), 1000, req.count)
    bid = deploy.new_id()
    width = max(3, len(str(req.count)))
    vms = [{"vmid": vmid, "name": f"{req.prefix}-{bid}-{i + 1:0{width}d}", "node": nodes[i % len(nodes)],
            "status": "queued"} for i, vmid in enumerate(ids)]
    state.data["batches"][bid] = {
        "template": req.template, "template_name": tmpl.get("name", ""), "template_node": node,
        "storage": target_storage, "full": full, "cores": req.cores, "memory": req.memory,
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


class RunReq(BaseModel):
    batch: str
    profile: str = "office"
    duration: int = Field(120, ge=10, le=3600)
    ramp: int = Field(0, ge=0, le=3600)


@app.get("/api/runs")
async def list_runs():
    return {"profiles": runs.PROFILES,
            "runs": sorted((runs.brief(r) for r in runs.runs.values()), key=lambda r: r["created"], reverse=True)}


@app.get("/api/runs/{rid}")
async def get_run(rid: str):
    r = runs.runs.get(rid)
    if not r:
        raise HTTPException(404, "run not found")
    return r


@app.post("/api/runs")
async def start_run(req: RunReq, request: Request):
    if req.profile not in runs.PROFILES:
        raise HTTPException(400, "unknown profile")
    b = state.data["batches"].get(req.batch)
    if not b or b["status"] not in ("ready", "partial"):
        raise HTTPException(400, "batch not found or not ready")
    if b.get("os") == "windows" and req.profile != "bootstorm":
        raise HTTPException(400, "Windows desktops only support the boot storm run for now")
    if runs.active():
        raise HTTPException(409, "another run is in progress")
    r = runs.create(req.batch, req.profile, req.duration, req.ramp, _ci_base(request))
    if not r["vms"]:
        runs.delete(r["id"])
        raise HTTPException(400, "the batch has no running desktops")
    deploy.spawn(runs.execute(pve, r))
    return {"id": r["id"]}


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
