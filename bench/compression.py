#!/usr/bin/env python3
"""What response compression costs per request, measured with `oha`.

    bench/compression.py --python .venv/bin/python

Two servers of the same app, one with `Compression()` and one without. The
off server gives the baseline for each route; the on server is measured with
no `Accept-Encoding` (eligible, sent plain), with gzip and with brotli. A
small reply under `min_size` on both servers is the cost of the check alone.

The cost is reported as microseconds of added wall time per request at the
measured concurrency, from the change in throughput against the same route
on the off server: (1/rps - 1/rps_off) multiplied by the number of worker
loops. Compression runs on tokio threads (or the blocking pool, for
`/large`), not the worker loops, so this is an approximation of where the
time went rather than a direct measurement of it; bytes per reply are
reported alongside, because they are the reason to pay it.
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from machine import Session
from run import oha, wait_port

ROOT = Path(__file__).resolve().parent.parent
PORT = 8767


def serve(py: str, workers: int, compression: bool) -> subprocess.Popen:
    cmd = [py, "bench/compression_app.py", "--port", str(PORT), "--workers", str(workers)]
    if compression:
        cmd.append("--compression")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if not wait_port(PORT, proc):
        raise SystemExit(f"compression_app failed to start:\n{proc.stderr.read()[-800:]}")
    return proc


def stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=10)
    except Exception:
        os.killpg(proc.pid, signal.SIGKILL)
    time.sleep(0.5)


def measure(path: str, accept: str | None, args) -> dict:
    url = f"http://127.0.0.1:{PORT}{path}"
    # oha sends an Accept-Encoding of its own unless given one, so "plain"
    # asks for identity outright. It does not decode: the size it reports is
    # the size on the wire.
    extra = ["-H", f"accept-encoding: {accept or 'identity'}"]
    oha(url, args.warmup, args.conns, extra)
    r = oha(url, args.duration, args.conns, extra)
    s, p = r["summary"], r["latencyPercentiles"]
    if s["successRate"] < 1.0:
        raise SystemExit(f"{path}: success rate {s['successRate']}")
    return {"rps": s["requestsPerSec"], "p50_ms": p["p50"] * 1000, "p99_ms": p["p99"] * 1000,
            "bytes": s["sizePerRequest"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--duration", type=int, default=10)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--conns", type=int, default=64)
    ap.add_argument("--workers", type=int, default=None,
                    help="worker loops; default is what the app would choose")
    ap.add_argument("--strict", action="store_true",
                    help="refuse to run on a busy host rather than warn")
    args = ap.parse_args()

    if args.workers is None:
        args.workers = int(subprocess.run(
            [args.python, "-c", "from oxbrook._workers import default_workers; "
             "print(default_workers())"],
            capture_output=True, text=True, check=True, cwd=ROOT,
        ).stdout)

    session = Session("compression", executable=args.python, conns=args.conns,
                      strict=args.strict)
    session.announce()
    rows: list[tuple[str, str, dict]] = []
    proc = serve(args.python, args.workers, compression=False)
    try:
        for path in ("/small", "/items", "/large"):
            rows.append((path, "off", measure(path, "br, gzip", args)))
    finally:
        stop(proc)
    proc = serve(args.python, args.workers, compression=True)
    try:
        rows.append(("/small", "on, under min_size", measure("/small", "br, gzip", args)))
        for path in ("/items", "/large"):
            rows.append((path, "on, plain", measure(path, None, args)))
            rows.append((path, "on, gzip", measure(path, "gzip", args)))
            rows.append((path, "on, br", measure(path, "br", args)))
    finally:
        stop(proc)

    base = {path: r["rps"] for path, label, r in rows if label == "off"}
    print(f"\n{'route':<8}{'server':<22}{'req/s':>10}{'p50 ms':>9}{'p99 ms':>9}"
          f"{'bytes':>9}{'+us/req':>10}")
    results = []
    for path, label, r in rows:
        added = (1 / r["rps"] - 1 / base[path]) * args.workers * 1e6
        print(f"{path:<8}{label:<22}{r['rps']:>10.0f}{r['p50_ms']:>9.2f}{r['p99_ms']:>9.2f}"
              f"{r['bytes']:>9.0f}{added:>10.1f}")
        results.append({"route": path, "server": label, **r, "added_us": added})
    path = session.finish({"workers": args.workers, "conns": args.conns, "results": results})
    print(f"\nsaved {path}")


if __name__ == "__main__":
    main()
