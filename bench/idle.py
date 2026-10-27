#!/usr/bin/env python3
"""Server memory per idle connection. (I-047)

    bench/idle.py --python .venv/bin/python

Opens idle connections in steps and reads the server's resident set after each
step settles. Three kinds, each against a fresh server so one kind's leftovers
are not counted against the next:

* *keep-alive* — a request answered, the connection left open. Nothing reaches
  Python; this is what hyper and the connection task cost. The server closes an
  idle keep-alive connection after 15 seconds, so this kind is sampled quickly
  and kept smaller.
* *sse* — a subscription held open on a topic: a handler coroutine, its
  subscription and the streaming response, on top of the connection.
* *websocket* — an upgraded socket whose handler is waiting in `receive`.

The figure is the slope of RSS between steps, divided by connections added:
the fixed cost of a server is excluded, and a step that is not linear with the
others shows. RSS is a coarse instrument (the allocator keeps what it freed),
so the figure is an upper bound per connection rather than an exact one.
"""
import argparse
import asyncio
import base64
import os
import resource
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from machine import Session

ROOT = Path(__file__).resolve().parent.parent
# One kind per port, each its own server process.
PORTS = {"keep-alive": 8781, "sse": 8782, "websocket": 8783}
REQUESTS = {
    "keep-alive": b"GET /subscribers HTTP/1.1\r\nhost: x\r\n\r\n",
    "sse": b"GET /sse HTTP/1.1\r\nhost: x\r\naccept: text/event-stream\r\n\r\n",
    "websocket": (b"GET /echo HTTP/1.1\r\nhost: x\r\nupgrade: websocket\r\n"
                  b"connection: Upgrade\r\nsec-websocket-version: 13\r\n"
                  b"sec-websocket-key: " + base64.b64encode(os.urandom(16)) + b"\r\n\r\n"),
}
EXPECT = {"keep-alive": b"HTTP/1.1 200", "sse": b"HTTP/1.1 200", "websocket": b"HTTP/1.1 101"}


def rss_kib(pid: int) -> int:
    return int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                              capture_output=True, text=True, check=True).stdout)


async def open_one(kind: str, port: int, held: list) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(REQUESTS[kind])
    head = await reader.readuntil(b"\r\n\r\n")
    if not head.startswith(EXPECT[kind]):
        raise SystemExit(f"{kind}: {head[:80]!r}")
    if kind == "keep-alive":
        # Drain the body so the connection is idle, not mid-response.
        length = int(head.lower().split(b"content-length:")[1].split(b"\r\n")[0])
        await reader.readexactly(length)
    held.append(writer)


async def measure(kind: str, steps: list[int], pid: int, settle: float) -> list[dict]:
    port, held, rows = PORTS[kind], [], []
    gate = asyncio.Semaphore(100)  # under the listen backlog (somaxconn is 128 on macOS)

    async def guarded():
        async with gate:
            await open_one(kind, port, held)

    await asyncio.sleep(settle)
    rows.append({"connections": 0, "rss_kib": rss_kib(pid)})
    for target in steps:
        started = time.perf_counter()
        await asyncio.gather(*(guarded() for _ in range(target - len(held))))
        opened = time.perf_counter() - started
        await asyncio.sleep(settle)
        rows.append({"connections": len(held), "rss_kib": rss_kib(pid), "open_s": opened})
        prev = rows[-2]
        per = (rows[-1]["rss_kib"] - prev["rss_kib"]) * 1024 / (len(held) - prev["connections"])
        rows[-1]["bytes_per_conn"] = per
        print(f"  {kind:10} {len(held):>6} conns  rss {rows[-1]['rss_kib'] / 1024:7.1f} MiB  "
              f"+{per / 1024:6.1f} KiB/conn over the step  (opened in {opened:.1f}s)", flush=True)
    for w in held:
        # Reset rather than close: a graceful close leaves the client's end in
        # TIME_WAIT, and ten thousand of those exhaust the ephemeral ports
        # before the next kind has opened its connections.
        w.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                               struct.pack("ii", 1, 0))
        w.close()
    return rows


def serve(python: str, port: int, workers: int | None) -> subprocess.Popen:
    cmd = [python, "bench/streams_app.py", "--port", str(port), "--max-concurrency", "16384"]
    if workers:
        cmd += ["--workers", str(workers)]
    proc = subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                            start_new_session=True, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    for _ in range(200):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc
        except OSError:
            time.sleep(0.05)
    raise SystemExit(f"server on {port} did not start")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--steps", default="1000,5000,10000")
    ap.add_argument("--keepalive-steps", default="1000,4000")
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()
    steps = [int(s) for s in args.steps.split(",")]
    _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 65536), hard))

    session = Session("idle", executable=args.python, conns=max(steps))
    session.announce()
    results = {}
    for kind in PORTS:
        proc = serve(args.python, PORTS[kind], args.workers)
        try:
            kind_steps = ([int(s) for s in args.keepalive_steps.split(",")]
                          if kind == "keep-alive" else steps)
            settle = min(args.settle, 1.0) if kind == "keep-alive" else args.settle
            results[kind] = asyncio.run(measure(kind, kind_steps, proc.pid, settle))
        finally:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=30)
    print(f"\nsaved {session.finish({'kinds': results, 'workers': args.workers})}")


if __name__ == "__main__":
    main()
