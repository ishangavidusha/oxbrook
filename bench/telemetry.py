#!/usr/bin/env python3
"""What OpenTelemetry tracing costs per request, measured with `oha`.

    bench/telemetry.py --python .venv/bin/python
    bench/telemetry.py --python .venv/bin/python --baseline /tmp/main-venv/bin/python

One server per mode of bench/telemetry_app.py: off, the adapter over the
API alone, the full SDK exporting nowhere, and the SDK sampling nothing.
Modes alternate over several rounds, so a drift in the machine's state lands
on every mode rather than on whichever ran last, and the median round is
reported.

`--baseline` adds `off` under another interpreter, one with an earlier build
of Oxbrook installed: the check that an app without telemetry pays nothing
for the adapter existing.

The cost is microseconds of added wall time per request against `off`:
(1/rps - 1/rps_off) multiplied by the number of worker loops, since the
tracing runs on the worker loops.
"""
import argparse
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from machine import Session
from run import oha, wait_port

ROOT = Path(__file__).resolve().parent.parent
PORT = 8768
URL = f"http://127.0.0.1:{PORT}/items/7"


def serve(py: str, workers: int, mode: str) -> subprocess.Popen:
    cmd = [py, "bench/telemetry_app.py", "--port", str(PORT), "--workers", str(workers),
           "--mode", mode]
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if not wait_port(PORT, proc):
        raise SystemExit(f"telemetry_app ({mode}) failed to start:\n{proc.stderr.read()[-800:]}")
    return proc


def stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=10)
    except Exception:
        os.killpg(proc.pid, signal.SIGKILL)
    time.sleep(0.5)


def measure(py: str, mode: str, args) -> dict:
    proc = serve(py, args.workers, mode)
    try:
        oha(URL, args.warmup, args.conns, [])
        r = oha(URL, args.duration, args.conns, [])
    finally:
        stop(proc)
    s, p = r["summary"], r["latencyPercentiles"]
    if s["successRate"] < 1.0:
        raise SystemExit(f"{mode}: success rate {s['successRate']}")
    return {"rps": s["requestsPerSec"], "p50_ms": p["p50"] * 1000, "p99_ms": p["p99"] * 1000}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--baseline", default=None,
                    help="an interpreter with an earlier Oxbrook, to measure `off` against")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--duration", type=int, default=8)
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

    session = Session("telemetry", executable=args.python, conns=args.conns, strict=args.strict)
    session.announce()
    runs: list[tuple[str, str]] = []
    if args.baseline:
        runs.append(("off (baseline build)", args.baseline))
    runs += [(mode, args.python) for mode in ("off", "api", "sdk", "sampled-out")]

    measured: dict[str, list[dict]] = {label: [] for label, _ in runs}
    for round_ in range(args.rounds):
        for label, py in runs:
            mode = "off" if label.startswith("off") else label
            r = measure(py, mode, args)
            measured[label].append(r)
            print(f"  round {round_ + 1} {label:<22}{r['rps']:>10.0f} req/s", flush=True)

    def median(label: str) -> dict:
        rows = sorted(measured[label], key=lambda r: r["rps"])
        return rows[len(rows) // 2]

    base = median("off")["rps"]
    print(f"\n{'mode':<24}{'req/s':>10}{'p50 ms':>9}{'p99 ms':>9}{'+us/req':>10}{'spread':>9}")
    results = []
    for label, _ in runs:
        r = median(label)
        rates = [x["rps"] for x in measured[label]]
        spread = (max(rates) - min(rates)) / statistics.mean(rates) * 100
        added = (1 / r["rps"] - 1 / base) * args.workers * 1e6
        print(f"{label:<24}{r['rps']:>10.0f}{r['p50_ms']:>9.2f}{r['p99_ms']:>9.2f}"
              f"{added:>10.1f}{spread:>8.1f}%")
        results.append({"mode": label, **r, "added_us": added, "spread_pct": spread,
                        "rounds": measured[label]})
    path = session.finish({"workers": args.workers, "conns": args.conns, "results": results})
    print(f"\nsaved {path}")


if __name__ == "__main__":
    main()
