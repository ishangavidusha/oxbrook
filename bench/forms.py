#!/usr/bin/env python3
"""Form parsing and upload throughput. (I-059)

    bench/forms.py --python .venv/bin/python

Each body is sent with `oha` at 64 connections and read four ways where it
applies: whole and untouched (`request.body`, the baseline), parsed by
`request.form()`, as a raw stream (`BodyStream`), and part by part
(`body.parts()`). The difference from the baseline is what parsing costs;
the bodies are small forms, where per-field cost shows, and one-megabyte
uploads, where per-byte cost does.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from machine import Session

ROOT = Path(__file__).resolve().parent.parent
PORT = 8774
BOUNDARY = "b0und4ry"


def urlencoded(fields: int) -> bytes:
    return "&".join(f"field{i}=value-{i}-abcdefgh" for i in range(fields)).encode()


def multipart(fields: int, file_bytes: int) -> bytes:
    out = bytearray()
    for i in range(fields):
        out += (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"field{i}\"\r\n\r\n"
                f"value-{i}-abcdefgh\r\n").encode()
    if file_bytes:
        out += (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"a.bin\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        out += bytes(range(256)) * (file_bytes // 256)
        out += b"\r\n"
    out += f"--{BOUNDARY}--\r\n".encode()
    return bytes(out)


CASES = [
    # name, body, content type, paths
    ("urlencoded, 10 fields", urlencoded(10), "application/x-www-form-urlencoded",
     ["/raw", "/form"]),
    ("urlencoded, 100 fields", urlencoded(100), "application/x-www-form-urlencoded",
     ["/raw", "/form"]),
    ("multipart, 10 fields", multipart(10, 0), f"multipart/form-data; boundary={BOUNDARY}",
     ["/raw", "/form", "/parts"]),
    ("multipart, 1 MiB file", multipart(0, 1 << 20), f"multipart/form-data; boundary={BOUNDARY}",
     ["/raw", "/form", "/stream", "/parts"]),
]


def oha(url: str, seconds: int, conns: int, body: Path, content_type: str) -> dict:
    out = subprocess.run(
        ["oha", "--no-tui", "--output-format", "json", "-z", f"{seconds}s", "-c", str(conns),
         "-m", "POST", "-H", f"content-type: {content_type}", "-D", str(body), url],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--duration", type=int, default=6)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--conns", type=int, default=64)
    args = ap.parse_args()

    session = Session("forms", executable=args.python, conns=args.conns)
    session.announce()
    proc = subprocess.Popen([args.python, "bench/forms_app.py", "--port", str(PORT)], cwd=ROOT,
                            env={**os.environ, "PYTHONPATH": str(ROOT)}, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    rows = []
    try:
        for _ in range(100):
            try:
                httpx.post(f"http://127.0.0.1:{PORT}/raw", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        with tempfile.TemporaryDirectory() as tmp:
            for name, body, content_type, paths in CASES:
                path = Path(tmp, "body")
                path.write_bytes(body)
                for route in paths:
                    url = f"http://127.0.0.1:{PORT}{route}"
                    check = httpx.post(url, content=body, headers={"content-type": content_type})
                    if check.status_code != 200:
                        raise SystemExit(f"{name} {route}: {check.status_code} {check.text[:200]}")
                    oha(url, args.warmup, args.conns, path, content_type)
                    r = oha(url, args.duration, args.conns, path, content_type)
                    s = r["summary"]
                    codes = r.get("statusCodeDistribution", {})
                    rps = s["requestsPerSec"]
                    rows.append({
                        "case": name, "route": route, "rps": rps,
                        "mb_per_s": rps * len(body) / 1e6,
                        "p99_ms": r["latencyPercentiles"]["p99"] * 1000,
                        "non_200": sum(n for code, n in codes.items() if code != "200"),
                    })
                    print(f"  {name:24} {route:8} {rps:>10,.0f} req/s "
                          f"{rows[-1]['mb_per_s']:>9,.1f} MB/s  non-200 {rows[-1]['non_200']}",
                          flush=True)
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
    print(f"\nsaved {session.finish({'rows': rows})}")


if __name__ == "__main__":
    main()
