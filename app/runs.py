"""Benchmark runs: start workloads in a batch's desktops, sample host metrics, collect results.

Workloads are started through the QEMU guest agent, so nothing has to be
pre-installed beyond what the golden template already carries. Each desktop
downloads the workload script from this tool and posts its results back.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

from .deploy import STATE_DIR, new_id, spawn, state
from .pve import PVE, PVEError

RUNS_DIR = STATE_DIR / "runs"
def _fio(name: str, **fio) -> dict:
    return {"name": name, "kind": "fio", "fio": fio}


# Built-in workload library. Custom fio profiles are stored beside it in profiles.json.
BUILTIN: dict[str, dict] = {
    "office": {"name": "Office worker (synthetic: documents, compress, compute, downloads)", "kind": "office"},
    "vdi-kw": _fio("VDI knowledge worker disk (25 IOPS each, mixed blocks, 70% write)",
                   rw="randrw", rwmixread=30, bssplit="4k/60:8k/20:16k/10:64k/10", iodepth=2, numjobs=1, rate_iops=25),
    "disk": _fio("Disk stress (4k random, 70% read, QD16)", rw="randrw", rwmixread=70, bs="4k", iodepth=16, numjobs=1),
    "db": _fio("Database (8k random, 60% read, 4 jobs x QD32)", rw="randrw", rwmixread=60, bs="8k", iodepth=32, numjobs=4),
    "seq-read": _fio("Sequential read throughput (1M, 4 jobs x QD32)", rw="read", bs="1M", iodepth=32, numjobs=4),
    "seq-write": _fio("Sequential write throughput (1M, 4 jobs x QD32)", rw="write", bs="1M", iodepth=32, numjobs=4),
    "max": _fio("Max throughput (1M mixed read/write, 8 jobs x QD64)", rw="rw", rwmixread=50, bs="1M", iodepth=64, numjobs=8),
    "cpu": {"name": "CPU stress (all cores)", "kind": "cpu"},
    "fill": {"name": "Fill disk (write and keep data, for capacity tests)", "kind": "fill"},
    "bootstorm": {"name": "Boot storm (restart every VM at once)", "kind": "bootstorm"},
}
CUSTOM_FILE = STATE_DIR / "profiles.json"


def custom_profiles() -> dict[str, dict]:
    try:
        return json.loads(CUSTOM_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_custom(profiles: dict[str, dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOM_FILE.write_text(json.dumps(profiles))


def all_profiles() -> dict[str, dict]:
    return {**BUILTIN, **{k: {**v, "custom": True} for k, v in custom_profiles().items()}}


SAMPLE_EVERY = 10
runs: dict[str, dict] = {}


def _load() -> None:
    try:
        for f in RUNS_DIR.glob("*.json"):
            r = json.loads(f.read_text())
            if r["status"] == "running":
                r.update(status="failed", error="interrupted by a service restart")
            r.setdefault("kind", "fio" if r["profile"] == "disk" else r["profile"])
            r.setdefault("profile_name", BUILTIN.get(r["profile"], {}).get("name", r["profile"]))
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
    spec = all_profiles()[profile]
    r = {"id": new_id(), "batch": bid, "batch_name": f"{b['prefix']}-{bid}", "profile": profile,
         "profile_name": spec["name"], "kind": spec["kind"], "spec": {k: spec[k] for k in ("kind", "fio") if k in spec},
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
    if kind in ("preparing", "prepared"):
        if vm["status"] in ("starting", "preparing"):
            vm["status"] = kind
    elif kind == "started":
        vm["status"] = "running"
    elif kind == "sample":
        # 10 second buckets of action latency across all desktops.
        k = str(int((time.time() - r["created"]) // SAMPLE_EVERY * SAMPLE_EVERY))
        bucket = r["latency"].setdefault(k, [0.0, 0])
        bucket[0] += float(body.get("sum_ms", 0))
        bucket[1] += int(body.get("n", 0))
    elif kind == "result":
        vm.update(status="done", result=body.get("result", {}))
        vm.pop("error", None)
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
        spec = base64.b64encode(json.dumps(r["spec"]).encode()).decode()
        # Stop anything left over from an earlier run first, so two workloads never overlap.
        script = ("pkill -f '^python3 /tmp/vdi-workload' ; pkill -x fio; "
                  f"curl -fsS -m 30 {r['base']}/agent/workload.py -o /tmp/vdi-workload.py && "
                  f"nohup python3 /tmp/vdi-workload.py --spec {spec} --duration {r['duration']} "
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

    async def wait(seconds: float, busy: tuple[str, ...]) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline and any(vm["status"] in busy for vm in vms):
            await asyncio.sleep(2)
        for vm in vms:
            if vm["status"] in busy:
                vm.update(status="failed", error=f"no report while {vm['status']} (the desktop may not reach this tool)")

    if r["kind"] == "fio":
        # Every desktop writes its test file first and they all share the storage, so the
        # allowance grows with the total amount written (assumes at least 200 MB/s overall).
        prep = asyncio.create_task(wait(180 + r.get("size_gb", 1) * max(20, len(vms) * 5), ("starting", "preparing")))
        # Do not let a few stragglers hold everyone: once 90% are ready and nothing has
        # changed for two minutes, start without them.
        seen, since = -1, time.time()
        while not prep.done():
            ready = sum(vm["status"] == "prepared" for vm in vms)
            if ready != seen:
                seen, since = ready, time.time()
            if ready >= 0.9 * len(vms) and time.time() - since > 120:
                prep.cancel()
                for vm in vms:
                    if vm["status"] in ("starting", "preparing"):
                        vm.update(status="failed", error="test file was not ready in time, the run started without this desktop")
                break
            await asyncio.sleep(2)
        r["go"] = True
        r["measure_from"] = round(time.time() - r["created"])
        await wait(r["duration"] + 180, ("prepared", "running"))
    else:
        layout = r.get("size_gb", 1) * max(20, len(vms) * 5) if r["kind"] == "fill" else 0
        await wait(r["duration"] + 180 + layout, ("starting", "running"))


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
    p = r["kind"]
    if p == "office":
        means = [d["mean_ms"] for d in done]
        out.update(mean_ms=round(sum(means) / len(means), 1), p95_ms=round(max(d["p95_ms"] for d in done), 1),
                   worst_desktop_ms=round(max(means), 1), actions=sum(d["actions"] for d in done),
                   errors=sum(d.get("errors", 0) for d in done))
    elif p == "fio":
        mbps = sum(d.get("mbps", d["read"]["mbps"] + d["write"]["mbps"]) for d in done)
        out.update(total_iops=sum(d["iops"] for d in done), iops_per_desktop=round(sum(d["iops"] for d in done) / len(done)),
                   total_mbps=round(mbps), total_gbit=round(mbps * 8 / 1000, 2),
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
        timed = [s for s in r["samples"] if s["t"] >= r.get("measure_from", 0)] or r["samples"]
        out["peak_host_cpu"] = max(s["host_cpu"] for s in timed)
    return out


async def execute(pve: PVE, r: dict) -> None:
    spawn(_sampler(pve, r))
    try:
        await (_bootstorm if r["kind"] == "bootstorm" else _workload)(pve, r)
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
    return {k: r.get(k) for k in ("id", "batch", "batch_name", "profile", "profile_name", "kind", "duration", "ramp", "size_gb", "status",
                                  "created", "finished", "summary", "error", "storage")} | {"counts": counts}


def go(rid: str) -> dict:
    """Desktops poll this before the timed part of a disk run, so they all start together."""
    r = runs.get(rid)
    if not r or r["status"] != "running":
        return {"go": False, "over": True}
    return {"go": bool(r.get("go")), "over": False}
