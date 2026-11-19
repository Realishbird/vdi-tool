"""Benchmark runs: start workloads in a batch's desktops, sample host metrics, collect results.

Workloads are started through the QEMU guest agent, so nothing has to be
pre-installed beyond what the golden template already carries. Each desktop
downloads the workload script from this tool and posts its results back.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .deploy import STATE_DIR, new_id, spawn, state
from .pve import PVE, PVEError

RUNS_DIR = STATE_DIR / "runs"
PROFILES = {
    "office": "Office worker (documents, compress, compute, downloads)",
    "disk": "Disk stress (fio 4k random, 70% read)",
    "cpu": "CPU stress (all cores)",
    "fill": "Fill disk (write and keep data, for capacity tests)",
    "bootstorm": "Boot storm (restart every desktop at once)",
}
SAMPLE_EVERY = 10
runs: dict[str, dict] = {}


def _load() -> None:
    try:
        for f in RUNS_DIR.glob("*.json"):
            r = json.loads(f.read_text())
            if r["status"] == "running":
                r.update(status="failed", error="interrupted by a service restart")
            runs[r["id"]] = r
    except (OSError, ValueError):
        pass


def save(r: dict) -> None:
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RUNS_DIR / f"{r['id']}.tmp"
        tmp.write_text(json.dumps(r))
        tmp.replace(RUNS_DIR / f"{r['id']}.json")
    except OSError:
        pass


def delete(rid: str) -> None:
    runs.pop(rid, None)
    try:
        (RUNS_DIR / f"{rid}.json").unlink()
    except OSError:
        pass


_load()


def active() -> dict | None:
    return next((r for r in runs.values() if r["status"] == "running"), None)


def pctl(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def create(bid: str, profile: str, duration: int, ramp: int, base: str, size_gb: int = 1) -> dict:
    b = state.data["batches"][bid]
    vms = {str(vm["vmid"]): {"vmid": vm["vmid"], "name": vm["name"], "node": vm["node"], "status": "queued"}
           for vm in b["vms"] if vm["status"] in ("running", "created")}
    r = {"id": new_id(), "batch": bid, "batch_name": f"{b['prefix']}-{bid}", "profile": profile,
         "duration": duration, "ramp": ramp, "base": base, "size_gb": size_gb, "status": "running", "created": time.time(),
         "storage": b["storage"], "vms": vms, "samples": [], "latency": {}, "summary": {}}
    runs[r["id"]] = r
    save(r)
    return r


def report(rid: str, vmid: str, body: dict[str, Any]) -> None:
    r = runs.get(rid)
    vm = r and r["vms"].get(vmid)
    if not vm or r["status"] != "running":
        return
    kind = body.get("type")
    if kind == "started":
        vm["status"] = "running"
    elif kind == "sample":
        # 10 second buckets of action latency across all desktops.
        k = str(int((time.time() - r["created"]) // SAMPLE_EVERY * SAMPLE_EVERY))
        bucket = r["latency"].setdefault(k, [0.0, 0])
        bucket[0] += float(body.get("sum_ms", 0))
        bucket[1] += int(body.get("n", 0))
    elif kind == "result":
        vm.update(status="done", result=body.get("result", {}))
    elif kind == "error":
        vm.update(status="failed", error=str(body.get("error", ""))[:300])


async def _sampler(pve: PVE, r: dict) -> None:
    """Record host and per-batch VM metrics every few seconds while the run is active."""
    ids = {int(v) for v in r["vms"]}
    prev: dict | None = None
    first: dict[int, dict] = {}
    while r["status"] == "running":
        try:
            res = await pve.get("/cluster/resources")
        except PVEError:
            await asyncio.sleep(SAMPLE_EVERY)
            continue
        now = time.time()
        nodes = [x for x in res if x.get("type") == "node" and x.get("status") == "online"]
        vms = [x for x in res if x.get("type") == "qemu" and int(x["vmid"]) in ids]
        tot = {k: sum(x.get(k, 0) for x in vms) for k in ("diskread", "diskwrite", "netin", "netout")}
        for x in vms:
            first.setdefault(int(x["vmid"]), {k: x.get(k, 0) for k in tot})
            vm = r["vms"][str(x["vmid"])]
            f = first[int(x["vmid"])]
            vm["io"] = {k: max(0, x.get(k, 0) - f[k]) for k in tot}
        maxcpu = sum(n.get("maxcpu", 0) for n in nodes) or 1
        s = {"t": round(now - r["created"]),
             "host_cpu": round(100 * sum(n.get("cpu", 0) * n.get("maxcpu", 0) for n in nodes) / maxcpu, 1),
             "host_mem": round(100 * sum(n.get("mem", 0) for n in nodes) / (sum(n.get("maxmem", 0) for n in nodes) or 1), 1),
             "vm_cpu": round(sum(x.get("cpu", 0) * x.get("maxcpu", 0) for x in vms), 1)}
        if prev:
            dt = max(1.0, now - prev["now"])
            for k in tot:
                s[k] = round(max(0, tot[k] - prev["tot"][k]) / dt / 1e6, 2)  # MB/s
        prev = {"now": now, "tot": tot}
        r["samples"].append(s)
        await asyncio.sleep(SAMPLE_EVERY)


async def _exec(pve: PVE, vm: dict, command: list[str]) -> None:
    await pve.request("POST", f"/nodes/{vm['node']}/qemu/{vm['vmid']}/agent/exec", command=command)


async def _agent_up(pve: PVE, vm: dict) -> bool:
    try:
        await pve.request("POST", f"/nodes/{vm['node']}/qemu/{vm['vmid']}/agent/ping", timeout=5)
        return True
    except PVEError:
        return False


async def _workload(pve: PVE, r: dict) -> None:
    vms = list(r["vms"].values())
    step = r["ramp"] / max(1, len(vms) - 1) if r["ramp"] else 0

    async def launch(i: int, vm: dict) -> None:
        await asyncio.sleep(i * step)
        url = f"{r['base']}/api/runs/{r['id']}/report/{vm['vmid']}"
        script = (f"curl -fsS -m 30 {r['base']}/agent/workload.py -o /tmp/vdi-workload.py && "
                  f"nohup python3 /tmp/vdi-workload.py --profile {r['profile']} --duration {r['duration']} "
                  f"--report {url} --base {r['base']} --size-gb {r.get('size_gb', 1)} >/tmp/vdi-workload.log 2>&1 &")
        try:
            # A freshly deployed desktop may still be booting; give its guest agent time to answer.
            for _ in range(60):
                if await _agent_up(pve, vm):
                    break
                await asyncio.sleep(5)
            await _exec(pve, vm, ["bash", "-c", script])
            vm["status"] = "starting"
        except PVEError as e:
            vm.update(status="failed", error=f"could not start the workload: {e}")

    await asyncio.gather(*(launch(i, vm) for i, vm in enumerate(vms)))
    deadline = time.time() + r["duration"] + 120 + (r.get("size_gb", 1) * 20 if r["profile"] == "disk" else 0)
    while time.time() < deadline and any(vm["status"] in ("starting", "running") for vm in vms):
        await asyncio.sleep(2)
    for vm in vms:
        if vm["status"] in ("starting", "running"):
            vm.update(status="failed", error="no result reported (the desktop may not reach this tool)")


async def _bootstorm(pve: PVE, r: dict) -> None:
    vms = list(r["vms"].values())

    async def stop(vm: dict) -> None:
        try:
            await pve.wait_task(vm["node"], await pve.post(f"/nodes/{vm['node']}/qemu/{vm['vmid']}/status/stop"))
        except PVEError:
            pass  # already stopped

    await asyncio.gather(*(stop(vm) for vm in vms))
    await asyncio.sleep(3)
    t0 = time.time()

    async def boot(vm: dict) -> None:
        try:
            vm["status"] = "running"
            await pve.wait_task(vm["node"], await pve.post(f"/nodes/{vm['node']}/qemu/{vm['vmid']}/status/start"))
            while time.time() - t0 < 600:
                if await _agent_up(pve, vm):
                    vm.update(status="done", result={"boot_s": round(time.time() - t0, 1)})
                    return
                await asyncio.sleep(1)
            vm.update(status="failed", error="guest agent did not answer within 10 minutes")
        except PVEError as e:
            vm.update(status="failed", error=str(e))

    await asyncio.gather(*(boot(vm) for vm in vms))


def summarize(r: dict) -> dict:
    done = [vm["result"] for vm in r["vms"].values() if vm["status"] == "done" and vm.get("result")]
    out: dict[str, Any] = {"desktops": len(r["vms"]), "completed": len(done),
                           "failed": sum(vm["status"] == "failed" for vm in r["vms"].values())}
    if not done:
        return out
    p = r["profile"]
    if p == "office":
        means = [d["mean_ms"] for d in done]
        out.update(mean_ms=round(sum(means) / len(means), 1), p95_ms=round(max(d["p95_ms"] for d in done), 1),
                   worst_desktop_ms=round(max(means), 1), actions=sum(d["actions"] for d in done),
                   errors=sum(d.get("errors", 0) for d in done))
    elif p == "disk":
        out.update(total_iops=sum(d["iops"] for d in done), iops_per_desktop=round(sum(d["iops"] for d in done) / len(done)),
                   read_lat_ms=round(sum(d["read"]["lat_ms"] for d in done) / len(done), 2),
                   write_lat_ms=round(sum(d["write"]["lat_ms"] for d in done) / len(done), 2),
                   worst_p99_ms=round(max(max(d["read"]["p99_ms"], d["write"]["p99_ms"]) for d in done), 2))
    elif p == "cpu":
        out.update(total_mb_per_s=round(sum(d["mb_per_s"] for d in done)),
                   mb_per_s_per_desktop=round(sum(d["mb_per_s"] for d in done) / len(done)))
    elif p == "fill":
        out.update(written_gb=round(sum(d["written_gb"] for d in done), 1), filled_gb_per_desktop=round(sum(d["total_gb"] for d in done) / len(done), 1),
                   total_write_mb_per_s=round(sum(d["mb_per_s"] for d in done)), stopped_early=sum(1 for d in done if d.get("stopped")))
    elif p == "bootstorm":
        t = [d["boot_s"] for d in done]
        out.update(mean_boot_s=round(sum(t) / len(t), 1), p95_boot_s=round(pctl(t, 95), 1), all_up_s=round(max(t), 1))
    if r["samples"]:
        out["peak_host_cpu"] = max(s["host_cpu"] for s in r["samples"])
    return out


async def execute(pve: PVE, r: dict) -> None:
    spawn(_sampler(pve, r))
    try:
        await (_bootstorm if r["profile"] == "bootstorm" else _workload)(pve, r)
        await asyncio.sleep(SAMPLE_EVERY)  # one last host sample
        r["summary"] = summarize(r)
        r.update(status="done" if r["summary"].get("completed") else "failed", finished=time.time())
    except Exception as e:  # noqa: BLE001
        r.update(status="failed", error=str(e), finished=time.time())
    save(r)


def brief(r: dict) -> dict:
    counts: dict[str, int] = {}
    for vm in r["vms"].values():
        counts[vm["status"]] = counts.get(vm["status"], 0) + 1
    return {k: r.get(k) for k in ("id", "batch", "batch_name", "profile", "duration", "ramp", "size_gb", "status",
                                  "created", "finished", "summary", "error", "storage")} | {"counts": counts}
