#!/usr/bin/env python3
"""vdi-tool workload, run inside a desktop. Python 3 standard library only.

Kinds:
  office  synthetic knowledge worker: save/open documents, compress, compute,
          download, with think time between actions. Reports action latency.
  fio     any fio pattern the harness describes. Reports IOPS, throughput and latency.
  cpu     all-core hashing. Reports throughput.
  fill    write incompressible data and keep it, to test storage at capacity.
"""
import argparse
import base64
import hashlib
import json
import multiprocessing
import os
import random
import subprocess
import time
import urllib.request
import zlib

WORKDIR = "/var/tmp/vdi-workload"


def post(url, payload):
    try:
        req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass  # the run controller treats a missing report as a failure


def pctl(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def office(duration, report, base):
    rnd = random.Random()
    blob = os.urandom(1 << 20)
    doc = os.path.join(WORKDIR, "doc.bin")

    def save():
        with open(doc, "wb") as f:
            for _ in range(rnd.randint(2, 8)):
                f.write(blob)
            f.flush()
            os.fsync(f.fileno())

    def open_doc():
        if not os.path.exists(doc):
            save()
        fd = os.open(doc, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)  # read from disk, not cache
            while os.read(fd, 1 << 20):
                pass
        finally:
            os.close(fd)

    def compress():
        zlib.compress(blob * 4, 6)

    def compute():
        h = hashlib.sha256()
        for _ in range(48):
            h.update(blob)

    def download():
        urllib.request.urlopen(base + "/agent/blob", timeout=30).read()

    actions = {"save": save, "open": open_doc, "compress": compress, "compute": compute, "download": download}
    weights = {"save": 3, "open": 4, "compress": 2, "compute": 2, "download": 3}
    names = [n for n, w in weights.items() for _ in range(w)]
    all_ms = {n: [] for n in actions}
    window, errors, start, last = [], 0, time.time(), time.time()
    while time.time() - start < duration:
        name = rnd.choice(names)
        t0 = time.perf_counter()
        try:
            actions[name]()
            ms = (time.perf_counter() - t0) * 1000
            all_ms[name].append(ms)
            window.append(ms)
        except Exception:
            errors += 1
        time.sleep(rnd.uniform(0.5, 2.0))  # think time
        if time.time() - last >= 5 and window:
            post(report, {"type": "sample", "sum_ms": sum(window), "n": len(window)})
            window, last = [], time.time()
    flat = [v for vals in all_ms.values() for v in vals]
    return {
        "actions": len(flat), "errors": errors,
        "mean_ms": round(sum(flat) / len(flat), 1) if flat else 0, "p95_ms": round(pctl(flat, 95), 1),
        "by_action": {n: {"n": len(v), "mean_ms": round(sum(v) / len(v), 1), "p95_ms": round(pctl(v, 95), 1)}
                      for n, v in all_ms.items() if v},
    }


SIZE_GB = 1
SPEC = {}


def fill(duration, report, base):
    """Write SIZE_GB of unique random data and leave it in place."""
    target_dir = "/var/tmp/vdi-fill"
    os.makedirs(target_dir, exist_ok=True)
    existing = sum(os.path.getsize(os.path.join(target_dir, f)) for f in os.listdir(target_dir))
    goal, written, start, stopped = SIZE_GB << 30, 0, time.time(), ""
    block = 4 << 20
    path = os.path.join(target_dir, "fill-%d.bin" % int(start))
    with open(path, "wb", buffering=0) as f:
        while existing + written < goal:
            if time.time() - start > duration:
                stopped = "time limit reached"
                break
            st = os.statvfs(target_dir)
            if st.f_bavail * st.f_frsize < (2 << 30):
                stopped = "desktop disk nearly full (2 GB kept free)"
                break
            f.write(os.urandom(block))  # fresh random data: cannot be compressed or deduplicated
            written += block
            if written % (256 << 20) == 0:
                os.fsync(f.fileno())
        os.fsync(f.fileno())
    secs = max(0.001, time.time() - start)
    return {"written_gb": round(written / (1 << 30), 1), "total_gb": round((existing + written) / (1 << 30), 1),
            "mb_per_s": round(written / 1e6 / secs, 1), "seconds": round(secs), "stopped": stopped}


def disk(duration, report, base):
    """Run fio with the settings in SPEC["fio"]; all jobs share one file of SIZE_GB."""
    f = SPEC.get("fio", {})
    target = os.path.join(WORKDIR, "fio.dat")
    rw = f.get("rw", "randrw")
    cmd = ["fio", "--name=vdi", f"--filename={target}", f"--size={SIZE_GB}G", f"--rw={rw}",
           f"--iodepth={int(f.get('iodepth', 16))}", f"--numjobs={int(f.get('numjobs', 1))}", "--group_reporting",
           "--ioengine=libaio", "--direct=1", "--time_based", f"--runtime={duration}", "--output-format=json"]
    cmd.append(f"--bssplit={f['bssplit']}" if f.get("bssplit") else f"--bs={f.get('bs', '4k')}")
    mix = int(f.get("rwmixread", 70))
    if rw in ("randrw", "rw"):
        cmd.append(f"--rwmixread={mix}")
    if f.get("rate_iops"):
        rate = int(f["rate_iops"])
        if rw in ("randrw", "rw"):
            r = max(1, round(rate * mix / 100))
            cmd.append(f"--rate_iops={r},{max(1, rate - r)}")
        else:
            cmd.append(f"--rate_iops={rate}")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 300 + SIZE_GB * 20)
    try:
        os.remove(target)
    except OSError:
        pass
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-300:] or "fio failed")
    job = json.loads(out.stdout)["jobs"][0]

    def side(name):
        j = job[name]
        return {"iops": round(j["iops"]), "mbps": round(j["bw_bytes"] / 1e6, 1),
                "lat_ms": round(j["clat_ns"]["mean"] / 1e6, 2),
                "p99_ms": round(j["clat_ns"].get("percentile", {}).get("99.000000", 0) / 1e6, 2)}
    r, w = side("read"), side("write")
    return {"read": r, "write": w, "iops": r["iops"] + w["iops"], "mbps": round(r["mbps"] + w["mbps"], 1)}


def _burn(seconds):
    buf, n, end = os.urandom(1 << 20), 0, time.time() + seconds
    while time.time() < end:
        hashlib.sha256(buf).digest()
        n += 1
    return n


def cpu(duration, report, base):
    cores = os.cpu_count() or 1
    with multiprocessing.Pool(cores) as pool:
        counts = pool.map(_burn, [duration] * cores)
    return {"cores": cores, "mb_per_s": round(sum(counts) / duration, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="base64 JSON: {kind, fio?}")
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--report", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--size-gb", type=int, default=1)
    a = ap.parse_args()
    global SIZE_GB, SPEC
    SIZE_GB = max(1, a.size_gb)
    SPEC = json.loads(base64.b64decode(a.spec))
    os.makedirs(WORKDIR, exist_ok=True)
    post(a.report, {"type": "started"})
    try:
        fn = {"office": office, "fio": disk, "cpu": cpu, "fill": fill}[SPEC["kind"]]
        post(a.report, {"type": "result", "result": fn(a.duration, a.report, a.base)})
    except Exception as e:  # report every failure back to the harness
        post(a.report, {"type": "error", "error": str(e)[:300]})


if __name__ == "__main__":
    main()
