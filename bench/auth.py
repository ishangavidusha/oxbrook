#!/usr/bin/env python3
"""What authentication costs per request, measured with `oha`.

    bench/auth.py --python .venv/bin/python

Each protected route is measured against the same route unprotected, on one
server, so the difference is the scheme and nothing else: an API key looked
up in a dict, an HS256 JWT, an RS256 JWT. Then a protected POST with a small
JSON body, whose body is read after authentication — against the same route
with its body collected first, which is what `--eager` starts.

The cost is reported as microseconds of added wall time per request at the
measured concurrency, from the change in throughput: (1/rps - 1/rps_public)
multiplied by the number of worker loops, which is how much longer each loop
spent per request.
"""
import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from machine import Session
from run import oha, wait_port

ROOT = Path(__file__).resolve().parent.parent
PORT = 8766
BODY = '{"name":"widget","size":3}'


def serve(py: str, key_file: str, workers: int, eager: bool) -> subprocess.Popen:
    cmd = [py, "bench/auth_app.py", "--port", str(PORT), "--workers", str(workers),
           "--public-key", key_file]
    if eager:
        cmd.append("--eager")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if not wait_port(PORT, proc):
        raise SystemExit(f"auth_app failed to start:\n{proc.stderr.read()[-800:]}")
    return proc


def stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=10)
    except Exception:
        os.killpg(proc.pid, signal.SIGKILL)
    time.sleep(0.5)


def measure(path: str, extra: list[str], args) -> dict:
    url = f"http://127.0.0.1:{PORT}{path}"
    oha(url, args.warmup, args.conns, extra)
    r = oha(url, args.duration, args.conns, extra)
    s, p = r["summary"], r["latencyPercentiles"]
    if s["successRate"] < 1.0:
        raise SystemExit(f"{path}: success rate {s['successRate']}")
    return {"rps": s["requestsPerSec"], "p50_ms": p["p50"] * 1000, "p99_ms": p["p99"] * 1000}


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

    sys.path.insert(0, str(ROOT / "bench"))
    import auth_app

    if args.workers is None:
        # The loop count the app would pick on this machine, asked of the
        # interpreter being measured: the figure per request depends on it.
        args.workers = int(subprocess.run(
            [args.python, "-c", "from oxbrook._workers import default_workers; "
             "print(default_workers())"],
            capture_output=True, text=True, check=True, cwd=ROOT,
        ).stdout)

    session = Session("auth", executable=args.python, conns=args.conns, strict=args.strict)
    session.announce()
    rows: list[tuple[str, dict]] = []
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as key_file:
        key_file.write(auth_app.PUBLIC)
    headers = {
        "key": ["-H", f"x-api-key: {auth_app.KEY}"],
        "hs256": ["-H", f"authorization: Bearer {auth_app.hs256_token()}"],
        "rs256": ["-H", f"authorization: Bearer {auth_app.rs256_token(auth_app.PRIVATE)}"],
    }
    post = ["-m", "POST", "-H", "content-type: application/json", "-d", BODY]
    try:
        proc = serve(args.python, key_file.name, args.workers, eager=False)
        try:
            rows.append(("GET public", measure("/public", [], args)))
            rows.append(("GET api key", measure("/key", headers["key"], args)))
            rows.append(("GET jwt hs256", measure("/hs256", headers["hs256"], args)))
            rows.append(("GET jwt rs256", measure("/rs256", headers["rs256"], args)))
            rows.append(("POST public", measure("/public-body", post, args)))
            rows.append(("POST api key, deferred", measure("/key-body", post + headers["key"],
                                                           args)))
        finally:
            stop(proc)
        proc = serve(args.python, key_file.name, args.workers, eager=True)
        try:
            rows.append(("POST api key, eager", measure("/key-body", post + headers["key"],
                                                        args)))
        finally:
            stop(proc)
    finally:
        os.unlink(key_file.name)

    base = {"GET": rows[0][1]["rps"], "POST": rows[4][1]["rps"]}
    print(f"\n{'route':<26}{'req/s':>10}{'p50 ms':>9}{'p99 ms':>9}{'+us/req':>10}")
    results = []
    for label, r in rows:
        added = (1 / r["rps"] - 1 / base[label.split()[0]]) * args.workers * 1e6
        print(f"{label:<26}{r['rps']:>10.0f}{r['p50_ms']:>9.2f}{r['p99_ms']:>9.2f}"
              f"{added:>10.1f}")
        results.append({"route": label, **r, "added_us": added})
    path = session.finish({"workers": args.workers, "conns": args.conns, "results": results})
    print(f"\nsaved {path}")


if __name__ == "__main__":
    main()
