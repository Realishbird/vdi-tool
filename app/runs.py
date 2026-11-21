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
LATE_AFTER = 30  # seconds after the shared start
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


series_busy: set[str] = set()  # density series that still have steps to run


def active() -> dict | None:
    return next((r for r in runs.values() if r["status"] == "running"), None) or ({"id": "series"} if series_busy else None)


def pctl(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def setup_of(b: dict) -> dict:
    """The facts about a batch that a report needs, kept with the run so they outlive the batch."""
    return {"clone": "full" if b.get("full") else "linked", "template": b.get("template_name"), "os": b.get("os", "linux"),
            "cores": b.get("cores"), "memory": b.get("memory"), "disk_gb": b.get("disk_gb"), "desktops": b.get("count")}


def create(bid: str, profile: str, duration: int, ramp: int, base: str, size_gb: int = 1, env: dict | None = None,
           limit: int | None = None, series: str | None = None, extra: list[str] | None = None) -> dict:
    """extra: more batches to load at the same moment, for a combined result across several storages."""
    b = state.data["batches"][bid]
    vms = {str(vm["vmid"]): {"vmid": vm["vmid"], "name": vm["name"], "node": vm["node"], "status": "queued"}
           for vm in [v for v in b["vms"] if v["status"] in ("running", "created")][:limit]}
    stores = [b["storage"]]
    for other in extra or []:
        ob = state.data["batches"][other]
        stores.append(ob["storage"])
        vms.update({str(vm["vmid"]): {"vmid": vm["vmid"], "name": vm["name"], "node": vm["node"], "status": "queued",
                                      "storage": ob["storage"]}
                    for vm in [v for v in ob["vms"] if v["status"] in ("running", "created")][:limit]})
    if extra:
        for vm in vms.values():
            vm.setdefault("storage", b["storage"])
    spec = all_profiles()[profile]
    r = {"id": new_id(), "batch": bid, "batch_name": f"{b['prefix']}-{bid}", "profile": profile,
         "profile_name": spec["name"], "kind": spec["kind"], "spec": {k: spec[k] for k in ("kind", "fio") if k in spec},
         "duration": duration, "ramp": ramp, "base": base, "size_gb": size_gb, "status": "running", "created": time.time(),
         "storage": b["storage"], "vms": vms, "samples": [], "latency": {}, "summary": {},
         "setup": setup_of(b), "env": env or {}, "note": "", "tags": []}
    if extra:
        names = sorted(set(stores))
        r.update(storage=" + ".join(names), batches=[bid, *extra], batch_name=f"{len(extra) + 1} batches")
        r["setup"]["desktops"] = len(vms)
    if series:
        r["series"] = series
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
        # A desktop that begins well after the shared start runs partly alone, so its
        # number is not part of what the storage did under the full load.
        if r.get("go_at") and time.time() - r["go_at"] > LATE_AFTER:
            vm["late"] = True
    elif kind == "sample":
        # 10 second buckets of action latency across all desktops.
        k = str(int((time.time() - r["created"]) // SAMPLE_EVERY * SAMPLE_EVERY))
        bucket = r["latency"].setdefault(k, [0.0, 0])
        bucket[0] += float(body.get("sum_ms", 0))
        bucket[1] += int(body.get("n", 0))
    elif kind == "io":
        # Per-second MB moved inside the desktop, put on this tool's clock (desktop clocks may be off).
        skew = time.time() - float(body.get("now") or time.time())
        per = r.setdefault("io", {}).setdefault(vm.get("storage") or r["storage"], {})
        for ts, rd, wr in (body.get("points") or [])[:600]:
            k = str(int(float(ts) + skew - r["created"]))
            b = per.setdefault(k, [0.0, 0.0])
            b[0], b[1] = round(b[0] + float(rd), 1), round(b[1] + float(wr), 1)
    elif kind == "result":
        vm.update(status="done", result=body.get("result", {}))
        vm.pop("error", None)
    elif kind == "error":
        vm.update(status="failed", error=str(body.get("error", ""))[:300])


def _diskstats() -> dict[str, tuple[float, float]]:
    """MB read and written since boot per block device, from the kernel the tool's container shares with the host."""
    out = {}
    try:
        for line in open("/proc/diskstats"):
            f = line.split()
            out[f[2]] = (int(f[5]) * 512 / 1e6, int(f[9]) * 512 / 1e6)
    except (OSError, ValueError, IndexError):
        pass
    return out


async def _drive_map(pve: PVE, r: dict) -> dict[str, list[str]]:
    """Which local drives sit behind each storage in the run (LVM storages only), read from Proxmox."""
    stores = {vm.get("storage") or r["storage"] for vm in r["vms"].values()}
    node = next(iter(r["vms"].values()))["node"]
    try:
        vgs = {c["name"]: [pv["name"].rsplit("/", 1)[-1] for pv in c.get("children", [])]
               for c in (await pve.get(f"/nodes/{node}/disks/lvm")).get("children", [])}
        conf = {x["storage"]: x for x in await pve.get("/storage")}
    except (PVEError, KeyError, AttributeError, TypeError):
        return {}
    seen = _diskstats()
    found = {st: [d for d in vgs.get(conf.get(st, {}).get("vgname", ""), []) if d in seen] for st in stores}
    # All or nothing, so a total never mixes two ways of counting.
    return found if all(found.values()) else {}


async def _drives(pve: PVE, r: dict) -> None:
    """Every second, record what each physical drive behind the run's storages moved."""
    devs = await _drive_map(pve, r)
    if not devs:
        return
    prev, last = _diskstats(), int(time.time() - r["created"])
    while r["status"] == "running":
        await asyncio.sleep(1)
        now, k = _diskstats(), int(time.time() - r["created"])
        if k <= last:
            continue
        # A busy tool can skip a second: spread what moved over every second since the last sample,
        # so nothing is lost and no second reads as zero.
        for st, names in devs.items():
            rd = sum(now[d][0] - prev[d][0] for d in names if d in now and d in prev) / (k - last)
            wr = sum(now[d][1] - prev[d][1] for d in names if d in now and d in prev) / (k - last)
            per = r.setdefault("drives", {}).setdefault(st, {})
            for t in range(last + 1, k + 1):
                per[str(t)] = [round(rd, 1), round(wr, 1)]
        prev, last = now, k


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


def _is_windows(r: dict) -> bool:
    return (r.get("setup") or {}).get("os") == "windows"


async def _why(pve: PVE, vm: dict, windows: bool = False) -> str:
    """Ask a silent desktop what its workload was doing, to explain a missing report."""
    base = f"/nodes/{vm['node']}/qemu/{vm['vmid']}/agent"
    # No double quotes in this command: the guest agent's argument quoting on Windows mangles them.
    wcmd = ("$p=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*run.ps1*-Spec*' }).Count; "
            "'procs: ' + $p + ' agent, ' + @(Get-Process diskspd -ErrorAction SilentlyContinue).Count + ' diskspd'; "
            "Get-ChildItem C:\\vdi-workload -ErrorAction SilentlyContinue | ForEach-Object { $_.Name + ' ' + $_.Length }; "
            "Get-Content C:\\vdi-workload\\err.log -Tail 6 -ErrorAction SilentlyContinue")
    cmd = ("echo \"procs: $(pgrep -fc vdi-workload.py) agent, $(pgrep -xc fio) fio\"; "
           "ls -la --time-style=+%H:%M:%S /var/tmp/vdi-workload 2>&1 | tail -n 3; tail -c 300 /tmp/vdi-workload.log 2>&1")
    try:
        argv = ["powershell.exe", "-NoProfile", "-Command", wcmd] if windows else ["bash", "-c", cmd]
        pid = (await pve.request("POST", f"{base}/exec", command=argv))["pid"]
        for _ in range(10):
            await asyncio.sleep(1)
            st = await pve.get(f"{base}/exec-status", pid=pid)
            if st.get("exited"):
                return " | ".join((st.get("out-data") or "").split("\n"))[:500]
    except Exception as e:  # diagnostics must never break the run
        return f"could not ask the desktop: {e}"[:200]
    return ""


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
        # The workload runs as its own systemd unit. Started plainly it would belong to the guest
        # agent's service, and a package upgrade that restarts that service kills it mid-run.
        args = (f"--spec {spec} --duration {r['duration']} --report {url} --base {r['base']} "
                f"--size-gb {r.get('size_gb', 1)}")
        log = "/tmp/vdi-workload.log"
        script = ("systemctl stop 'vdi-workload-*.service' 2>/dev/null; "
                  "pkill -f '^(/usr/bin/)?python3 (-u )?/tmp/vdi-workload' ; pkill -x fio; "
                  f"curl -fsS -m 30 {r['base']}/agent/workload.py -o /tmp/vdi-workload.py && "
                  f"{{ systemd-run --quiet --collect --unit vdi-workload-{r['id']} -p StandardOutput=file:{log} "
                  f"-p StandardError=file:{log} /usr/bin/python3 -u /tmp/vdi-workload.py {args} 2>/dev/null || "
                  f"nohup python3 /tmp/vdi-workload.py {args} >{log} 2>&1 & }}")
        d = "C:\\vdi-workload"
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*vdi-workload\\run.ps1*-Spec*' "
              "-and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
              "Get-Process diskspd -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; "
              f"New-Item -ItemType Directory -Force -Path {d} | Out-Null; $ProgressPreference='SilentlyContinue'; "
              f"Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri {r['base']}/agent/workload.ps1 -OutFile {d}\\run.ps1; "
              f"Start-Process powershell.exe -WindowStyle Hidden -RedirectStandardOutput {d}\\out.log -RedirectStandardError {d}\\err.log "
              f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','{d}\\run.ps1','-Spec','{spec}',"
              f"'-Duration','{r['duration']}','-Report','{url}','-Base','{r['base']}','-SizeGb','{r.get('size_gb', 1)}'")
        try:
            # A freshly deployed desktop may still be booting; give its guest agent time to answer.
            for _ in range(60):
                if await _agent_up(pve, vm):
                    break
                await asyncio.sleep(5)
            if _is_windows(r):
                await _exec(pve, vm, ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
            else:
                await _exec(pve, vm, ["bash", "-c", script])
            if vm["status"] == "queued":  # a fast desktop may already have reported; never step backwards
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

    try:
        await _timed(r, vms, wait)
    finally:
        for vm in [v for v in vms if v["status"] == "failed" and "could not start" not in v.get("error", "")][:10]:
            why = await _why(pve, vm, _is_windows(r))
            if why:
                vm["error"] += " Desktop says: " + why


async def _timed(r: dict, vms: list[dict], wait) -> None:
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
        r["go_at"] = time.time()
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
    done = [vm["result"] for vm in r["vms"].values() if vm["status"] == "done" and vm.get("result") and not vm.get("late")]
    out: dict[str, Any] = {"desktops": len(r["vms"]), "completed": len(done),
                           "failed": sum(vm["status"] == "failed" for vm in r["vms"].values())}
    late = sum(1 for vm in r["vms"].values() if vm.get("late"))
    if late:
        out["late"] = late
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
        if all("p95_ms" in d["read"] for d in done):
            # Average of the per-desktop percentiles, taking the busier side of each desktop.
            side = lambda d, k: max(d["read"][k], d["write"][k])  # noqa: E731
            out.update(p50_ms=round(sum(side(d, "p50_ms") for d in done) / len(done), 2),
                       p95_ms=round(sum(side(d, "p95_ms") for d in done) / len(done), 2),
                       max_lat_ms=round(max(side(d, "max_ms") for d in done), 2))
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
    stores = sorted({vm["storage"] for vm in r["vms"].values() if vm.get("storage")})
    counted = io_totals(r) if p == "fio" else {}
    if counted:
        # Disk counters added up second by second: what the storage did while everyone was
        # loading it together. This replaces the sum of per-desktop averages.
        out.update(fio_sum_mbps=out["total_mbps"], total_mbps=counted["all"]["mbps"],
                   total_gbit=round(counted["all"]["mbps"] * 8 / 1000, 2), peak_mbps=counted["all"]["peak"])
        out["counted_by"] = "drives" if r.get("drives") else "desktops"
    if p == "fio" and len(stores) > 1:
        # A run across several storages: the same totals, split per storage.
        by = {}
        for st in stores:
            mine = [vm for vm in r["vms"].values() if vm.get("storage") == st]
            res = [vm["result"] for vm in mine if vm["status"] == "done" and vm.get("result") and not vm.get("late")]
            mb = sum(d.get("mbps", d["read"]["mbps"] + d["write"]["mbps"]) for d in res)
            by[st] = {"desktops": len(mine), "completed": len(res), "total_iops": sum(d["iops"] for d in res),
                      "total_mbps": round(mb), "total_gbit": round(mb * 8 / 1000, 2),
                      "p95_ms": round(sum(max(d["read"].get("p95_ms", 0), d["write"].get("p95_ms", 0)) for d in res) / len(res), 2) if res else 0}
        for st, v in by.items():
            if st in counted:
                v.update(total_mbps=counted[st]["mbps"], total_gbit=round(counted[st]["mbps"] * 8 / 1000, 2), peak_mbps=counted[st]["peak"])
        out["by_storage"] = by
    return out


def io_series(r: dict) -> dict[str, list[list[float]]]:
    """Per storage and for all of them: [second, read MB/s, write MB/s]. Taken from the physical drives'
    own counters when the storage is local, otherwise from the counters inside the desktops."""
    io = r.get("drives") or r.get("io") or {}
    if not io:
        return {}
    secs = sorted({int(k) for per in io.values() for k in per})
    out = {st: [[t, *per.get(str(t), [0.0, 0.0])] for t in range(secs[0], secs[-1] + 1)] for st, per in io.items()}
    out["all"] = [[t, round(sum(io[st].get(str(t), [0, 0])[0] for st in io), 1), round(sum(io[st].get(str(t), [0, 0])[1] for st in io), 1)]
                  for t in range(secs[0], secs[-1] + 1)]
    return out


def io_totals(r: dict) -> dict[str, dict]:
    """Average over the shared timed window (5 s trimmed at each end) and the best 10 s."""
    start, dur = r.get("measure_from"), r.get("duration", 0)
    if start is None or dur < 20:
        return {}
    res = {}
    for st, rows in io_series(r).items():
        win = [rd + wr for t, rd, wr in rows if start + 5 <= t < start + dur - 5]
        if len(win) < 10:
            return {}
        peak = max(sum(win[i:i + 10]) / 10 for i in range(len(win) - 9))
        res[st] = {"mbps": round(sum(win) / (dur - 10)), "peak": round(peak)}
    return res


async def execute(pve: PVE, r: dict) -> None:
    spawn(_sampler(pve, r))
    if r["kind"] == "fio":
        spawn(_drives(pve, r))
    try:
        await (_bootstorm if r["kind"] == "bootstorm" else _workload)(pve, r)
        await asyncio.sleep(SAMPLE_EVERY)  # one last host sample
        r["summary"] = summarize(r)
        r.update(status="done" if r["summary"].get("completed") else "failed", finished=time.time())
    except Exception as e:  # noqa: BLE001
        r.update(status="failed", error=str(e), finished=time.time())
    save(r)


def _timed_samples(r: dict) -> list[dict]:
    start = r.get("measure_from", 0)
    return [s for s in r["samples"] if start <= s["t"] <= start + r.get("duration", 0) + SAMPLE_EVERY] or r["samples"]


def validity(r: dict) -> dict:
    """Decide whether a run's numbers can be trusted, with the reasons in plain words."""
    s, problems, notes = r.get("summary") or {}, [], []
    total = len(r["vms"]) or 1
    failed = sum(vm["status"] == "failed" for vm in r["vms"].values())
    if r["status"] == "running":
        return {"level": "running", "problems": [], "notes": []}
    if r["status"] == "failed" or not s.get("completed"):
        problems.append(r.get("error") or "No desktop completed the run.")
    elif failed / total > 0.10:
        problems.append(f"{failed} of {total} desktops failed, so the totals understate what the full batch would do.")
    elif failed:
        notes.append(f"{failed} of {total} desktops failed; totals cover the other {total - failed}.")
    if s.get("late"):
        notes.append(f"{s['late']} of {total} desktops started late and ran partly alone; they are left out of the totals.")
    if r["kind"] == "fio" and s.get("completed"):
        if "measure_from" not in r:
            problems.append("This run predates the fix that writes real test data. Reads may have returned zeros "
                            "without touching the storage, so throughput is not trustworthy.")
        # Cross-check what the desktops claim against what Proxmox itself saw on their disks.
        seen = sorted(x.get("diskread", 0) + x.get("diskwrite", 0) for x in _timed_samples(r))
        host = seen[len(seen) // 2] if seen else 0
        claimed = s.get("fio_sum_mbps", s.get("total_mbps", 0))
        if s.get("fio_sum_mbps") and claimed > 1.15 * s["total_mbps"]:
            notes.append(f"The desktops' own averages add up to {claimed:,} MB/s, but they did not all run at the same moment. "
                         f"The throughput shown is what the {'drives' if s.get('counted_by') == 'drives' else 'desktops'} "
                         "moved second by second during the shared window.")
        if claimed > 50 and host and claimed > 1.5 * host and not s.get("fio_sum_mbps"):
            problems.append(f"Desktops reported {s['total_mbps']:,} MB/s but Proxmox saw only about {round(host):,} MB/s "
                            "on their disks, so part of the I/O never reached the storage.")
    return {"level": "invalid" if problems else "warning" if notes else "valid", "problems": problems, "notes": notes}


def verdict(r: dict) -> str:
    """One plain sentence on what limited the run."""
    s = r.get("summary") or {}
    if not s.get("completed"):
        return ""
    cpu = s.get("peak_host_cpu", 0)
    if r["kind"] == "cpu":
        return f"CPU test: the host peaked at {cpu}% CPU."
    if cpu >= 85:
        return f"The host CPU was the limit (peak {cpu}%). The storage could likely deliver more with fewer or lighter desktops per core."
    if r["kind"] == "fio":
        rate = (r.get("spec", {}).get("fio") or {}).get("rate_iops")
        if rate:
            return f"Load was capped at {rate} IOPS per desktop, so this shows latency at a fixed load, not the maximum (host CPU peak {cpu}%)."
        return f"The storage path was the limit: desktops ran flat out while the host CPU peaked at only {cpu}%."
    if r["kind"] == "bootstorm":
        return f"All desktops booted at once; the host CPU peaked at {cpu}%."
    return f"The host CPU peaked at {cpu}%, so the host had headroom."


def desktop_metric(kind: str) -> tuple[str, str, bool]:
    """(result key, label, higher is better) used to rank desktops inside one run."""
    return {"office": ("mean_ms", "Avg action time (ms)", False), "fio": ("iops", "IOPS", True),
            "cpu": ("mb_per_s", "Hashed (MB/s)", True), "fill": ("mb_per_s", "Write speed (MB/s)", True),
            "bootstorm": ("boot_s", "Boot time (s)", False)}.get(kind, ("iops", "IOPS", True))


def spread(r: dict) -> dict:
    key, label, higher = desktop_metric(r["kind"])
    vals = sorted(vm["result"][key] for vm in r["vms"].values() if vm.get("result") and key in vm["result"])
    if not vals:
        return {}
    return {"key": key, "label": label, "higher_better": higher, "min": vals[0], "p50": pctl(vals, 50), "p95": pctl(vals, 95),
            "max": vals[-1]}


def report_data(r: dict) -> dict:
    """Everything a report page needs for one run."""
    b = state.data["batches"].get(r["batch"])
    setup = r.get("setup") or (setup_of(b) if b else {})
    return {**{k: v for k, v in r.items() if k not in ("io", "drives")}, "io_series": io_series(r), "setup": setup, "validity": validity(r), "verdict": verdict(r), "spread": spread(r),
            "env": r.get("env") or {}, "note": r.get("note", ""), "tags": r.get("tags", [])}


def desktops_csv(r: dict) -> str:
    import csv
    import io
    rows = []
    for vm in r["vms"].values():
        res, flat = vm.get("result") or {}, {}
        for k, v in res.items():
            if isinstance(v, dict):
                flat.update({f"{k}_{k2}": v2 for k2, v2 in v.items()})
            else:
                flat[k] = v
        io_ = vm.get("io") or {}
        rows.append({"vmid": vm["vmid"], "name": vm["name"], "node": vm["node"], "status": vm["status"], **flat,
                     "disk_read_bytes": io_.get("diskread"), "disk_written_bytes": io_.get("diskwrite"),
                     "net_in_bytes": io_.get("netin"), "net_out_bytes": io_.get("netout"), "error": vm.get("error", "")})
    cols: list[str] = []
    for row in rows:
        cols += [c for c in row if c not in cols]
    cols = [c for c in cols if c != "error"] + ["error"]
    out = io.StringIO()
    w = csv.DictWriter(out, cols)
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def runs_csv() -> str:
    import csv
    import io
    rows = []
    for r in sorted(runs.values(), key=lambda r: r["created"]):
        setup = report_data(r)["setup"]
        rows.append({"run": r["id"], "started_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["created"])),
                     "workload": r["profile_name"], "kind": r["kind"], "status": r["status"], "validity": validity(r)["level"],
                     "storage": r["storage"], "storage_type": (r.get("env") or {}).get("storage_type", ""),
                     "clone": setup.get("clone", ""), "batch": r["batch_name"], "duration_s": r["duration"], "ramp_s": r["ramp"],
                     "size_gb": r.get("size_gb", ""), "tags": " ".join(r.get("tags", [])), "note": r.get("note", ""),
                     **(r.get("summary") or {})})
    cols: list[str] = []
    for row in rows:
        cols += [c for c in row if c not in cols]
    out = io.StringIO()
    w = csv.DictWriter(out, cols)
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


async def execute_series(pve: PVE, sid: str, steps: list[int], make) -> None:
    """Density test: the same workload on a growing number of desktops, one step after another."""
    series_busy.add(sid)
    try:
        for n in steps:
            r = make(n)
            if not r["vms"]:
                delete(r["id"])
                break
            await execute(pve, r)
            await asyncio.sleep(15)  # let the storage settle between steps
    finally:
        series_busy.discard(sid)


def brief(r: dict) -> dict:
    counts: dict[str, int] = {}
    for vm in r["vms"].values():
        counts[vm["status"]] = counts.get(vm["status"], 0) + 1
    return {k: r.get(k) for k in ("id", "batch", "batch_name", "profile", "profile_name", "kind", "duration", "ramp", "size_gb", "status",
                                  "created", "finished", "summary", "error", "storage", "note", "tags", "series")} | {
        "counts": counts, "validity": validity(r)["level"], "clone": (r.get("setup") or {}).get("clone")}


def go(rid: str) -> dict:
    """Desktops poll this before the timed part of a disk run, so they all start together."""
    r = runs.get(rid)
    if not r or r["status"] != "running":
        return {"go": False, "over": True}
    return {"go": bool(r.get("go")), "over": False}


def _label(v: Any) -> str:
    return str(v if v is not None else "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def prometheus() -> str:
    """Run results in the Prometheus text format, so Grafana can chart runs next to host load."""
    out = ["# HELP vdi_run_active 1 while a benchmark run is in progress.", "# TYPE vdi_run_active gauge",
           "# HELP vdi_run_desktops Desktops in a run by state.", "# TYPE vdi_run_desktops gauge",
           "# HELP vdi_run_result Summary results of finished runs (one series per measure).", "# TYPE vdi_run_result gauge",
           "# HELP vdi_run_valid 1 if the run passed the validity checks, 0.5 with a caveat, 0 if not.", "# TYPE vdi_run_valid gauge",
           "# HELP vdi_run_finished_timestamp_seconds When the run finished.", "# TYPE vdi_run_finished_timestamp_seconds gauge"]
    recent = sorted(runs.values(), key=lambda r: r["created"])[-60:]
    for r in recent:
        setup = r.get("setup") or {}
        base = (f'run="{_label(r["id"])}",workload="{_label(r["profile_name"])}",kind="{_label(r["kind"])}",storage="{_label(r["storage"])}",'
                f'clone="{_label(setup.get("clone"))}",batch="{_label(r["batch_name"])}",series="{_label(r.get("series"))}"')
        out.append(f'vdi_run_active{{{base}}} {1 if r["status"] == "running" else 0}')
        if r["status"] == "running":
            counts: dict[str, int] = {}
            for vm in r["vms"].values():
                counts[vm["status"]] = counts.get(vm["status"], 0) + 1
            out += [f'vdi_run_desktops{{{base},state="{_label(k)}"}} {v}' for k, v in counts.items()]
            continue
        for k, v in (r.get("summary") or {}).items():
            if isinstance(v, (int, float)):
                out.append(f'vdi_run_result{{{base},measure="{_label(k)}"}} {v}')
        out.append(f'vdi_run_valid{{{base}}} {dict(valid=1, warning=0.5).get(validity(r)["level"], 0)}')
        if r.get("finished"):
            out.append(f'vdi_run_finished_timestamp_seconds{{{base}}} {round(r["finished"])}')
    return "\n".join(out) + "\n"
