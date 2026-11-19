"""vdi-tool web API and GUI server."""
from __future__ import annotations

import asyncio
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.get("/")
async def index():
    return FileResponse(ROOT / "web" / "index.html")
