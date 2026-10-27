#!/usr/bin/env python3
"""SSE fan-out and WebSocket echo throughput. (I-016)

    bench/streams.py --python .venv/bin/python

**SSE.** S clients subscribe to one topic, then a single publish emits N events.
The topic blocks rather than drops, so the run ends only when every client has
every event, and the rate is lossless deliveries per second. Anything missing is
reported, not averaged away.

**WebSocket.** C connections each send a small message and wait for the echo, as
fast as they can, for a fixed time. The rate is round trips per second.

**Who ran out first.** The load generator is Python, on the same machine as the
server, and could easily be the slower half. Two numbers guard against reading
a client limit as a server one:

* *server CPU per 1000* — CPU the server process spent per thousand deliveries
  or round trips, read from inside it. This is the efficiency number, and it
  holds whatever the client's speed.
* *client cores* — CPU the client processes used over wall time. When that
  approaches the number of client processes, the client was saturated and the
  rate is a floor for the server, not its ceiling.
"""
import argparse
import asyncio
import json
import multiprocessing as mp
import os
import resource
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

# bench/ is a directory of scripts rather than a package, so a sibling
# import needs the directory on the path first.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from machine import Session

ROOT = Path(__file__).resolve().parent.parent
PORT = 8773
BASE = f"http://127.0.0.1:{PORT}"


def split(total: int, parts: int) -> list[int]:
    parts = max(1, min(parts, total))
    return [total // parts + (1 if i < total % parts else 0) for i in range(parts)]


def cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


# ---------------------------------------------------------------------------
# SSE clients
# ---------------------------------------------------------------------------
async def _subscriber(expected: int) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    writer.write(b"GET /sse HTTP/1.1\r\nHost: bench\r\n\r\n")
    await writer.drain()
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        block = await reader.read(65536)
        if not block:
            return 0
        buffer += block
    rest = buffer.partition(b"\r\n\r\n")[2]
    # Events end in a blank line. Chunk framing only ever writes \r\n, so a
    # bare \n\n is always an event boundary; one byte of carry catches a
    # boundary split across two reads.
    count, carry = rest.count(b"\n\n"), rest[-1:]
    while count < expected:
        block = await reader.read(65536)
        if not block:
            break
        count += (carry + block).count(b"\n\n")
        carry = block[-1:]
    writer.close()
    return count


def sse_client(conns: int, expected: int, results) -> None:
    async def run():
        return await asyncio.gather(*(_subscriber(expected) for _ in range(conns)))

    counts = asyncio.run(run())
    results.put({"counts": counts, "done": time.monotonic(), "cpu": cpu_seconds()})


def measure_sse(subscribers: int, events: int, procs: int, size: int) -> dict:
    results = mp.Queue()
    clients = [mp.Process(target=sse_client, args=(n, events, results))
               for n in split(subscribers, procs)]
    for c in clients:
        c.start()

    with httpx.Client(base_url=BASE, timeout=600) as http:
        deadline = time.monotonic() + 60
        while http.get("/subscribers").json()["count"] < subscribers:
            if time.monotonic() > deadline:
                raise RuntimeError(f"only {http.get('/subscribers').json()['count']} "
                                   f"of {subscribers} subscribers connected")
            time.sleep(0.05)
        cpu_before = http.get("/cpu").json()["cpu"]
        started = time.monotonic()
        published = http.post("/publish", params={"n": events, "size": size}).json()
        reports = [results.get(timeout=600) for _ in clients]
        finished = max(r["done"] for r in reports)
        cpu_after = http.get("/cpu").json()["cpu"]

    for c in clients:
        c.join(timeout=10)

    wall = finished - started
    delivered = sum(sum(r["counts"]) for r in reports)
    expected = subscribers * events
    return {
        "subscribers": subscribers,
        "events": events,
        "payload_bytes": size,
        "delivered": delivered,
        "missing": expected - delivered,
        "wall_s": wall,
        "publish_s": published["elapsed"],
        "deliveries_per_s": delivered / wall if wall > 0 else 0,
        "server_cpu_ms_per_1000": (cpu_after - cpu_before) * 1000 / max(delivered, 1) * 1000,
        "client_procs": len(clients),
        "client_cores": sum(r["cpu"] for r in reports) / wall if wall > 0 else 0,
    }


# ---------------------------------------------------------------------------
# WebSocket clients
# ---------------------------------------------------------------------------
async def _echoer(start_at: float, seconds: float, message: bytes) -> int:
    import websockets

    async with websockets.connect(f"ws://127.0.0.1:{PORT}/echo", max_size=None) as ws:
        await asyncio.sleep(max(0.0, start_at - time.monotonic()))
        stop = start_at + seconds
        count = 0
        while time.monotonic() < stop:
            await ws.send(message)
            await ws.recv()
            count += 1
        return count


def ws_client(conns: int, start_at: float, seconds: float, size: int, results) -> None:
    message = b"x" * size

    async def run():
        return await asyncio.gather(*(_echoer(start_at, seconds, message) for _ in range(conns)))

    cpu_before = cpu_seconds()
    counts = asyncio.run(run())
    results.put({"counts": counts, "cpu": cpu_seconds() - cpu_before})


def measure_ws(connections: int, seconds: float, procs: int, size: int) -> dict:
    results = mp.Queue()
    # Everyone starts at the same instant, after all connections are open, so
    # handshakes are not counted as throughput. Generous for a thousand sockets.
    start_at = time.monotonic() + 3.0 + connections / 200
    clients = [mp.Process(target=ws_client, args=(n, start_at, seconds, size, results))
               for n in split(connections, procs)]
    for c in clients:
        c.start()

    with httpx.Client(base_url=BASE, timeout=60) as http:
        time.sleep(max(0.0, start_at - time.monotonic()))
        cpu_before = http.get("/cpu").json()["cpu"]
        reports = [results.get(timeout=seconds + 120) for _ in clients]
        cpu_after = http.get("/cpu").json()["cpu"]

    for c in clients:
        c.join(timeout=10)

    trips = sum(sum(r["counts"]) for r in reports)
    return {
        "connections": connections,
        "seconds": seconds,
        "payload_bytes": size,
        "round_trips": trips,
        "round_trips_per_s": trips / seconds,
        # Includes the handshake-free tail after the window closes; negligible
        # against a multi-second window.
        "server_cpu_ms_per_1000": (cpu_after - cpu_before) * 1000 / max(trips, 1) * 1000,
        "client_procs": len(clients),
        "client_cores": sum(r["cpu"] for r in reports) / seconds,
    }


def wsload() -> Path:
    """Build the Rust echo client (bench/wsload) and return its path."""
    target = ROOT / "target" / "wsload"
    subprocess.run(["cargo", "build", "--release", "--quiet", "--manifest-path",
                    str(ROOT / "bench" / "wsload" / "Cargo.toml"), "--target-dir", str(target)],
                   check=True)
    return target / "release" / "wsload"


def measure_ws_rust(binary: Path, connections: int, seconds: float, threads: int,
                    size: int) -> dict:
    """The same echo, from a Rust client: one process, `threads` runtime threads.

    A Python client spends most of a core per ~40k round trips, so above a
    hundred connections it measured itself. This one costs a fraction of that,
    which leaves the server as the side that runs out.
    """
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    proc = subprocess.Popen([str(binary), f"ws://127.0.0.1:{PORT}/echo", str(connections),
                             str(seconds), str(size), str(threads)],
                            stdout=subprocess.PIPE, text=True)
    with httpx.Client(base_url=BASE, timeout=60) as http:
        if proc.stdout.readline().strip() != "measuring":
            raise SystemExit("wsload did not start")
        cpu_before = http.get("/cpu").json()["cpu"]
        report = json.loads(proc.stdout.readline())
        cpu_after = http.get("/cpu").json()["cpu"]
    proc.wait(timeout=60)
    wall = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    client_cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    trips = report["round_trips"]
    return {
        "connections": connections,
        "seconds": report["seconds"],
        "payload_bytes": size,
        "round_trips": trips,
        "round_trips_per_s": trips / report["seconds"],
        "server_cpu_ms_per_1000": (cpu_after - cpu_before) * 1000 / max(trips, 1) * 1000,
        "client": "rust",
        "client_procs": threads,
        # Over the client's whole life, connecting included, so slightly low.
        "client_cores": client_cpu / wall,
    }


# ---------------------------------------------------------------------------
def wait_port(proc, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def saturated(row: dict) -> str:
    return " client-bound" if row["client_cores"] > 0.85 * row["client_procs"] else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--client-procs", type=int, default=4)
    ap.add_argument("--subscribers", type=int, nargs="*", default=[10, 100, 1000])
    ap.add_argument("--deliveries", type=int, default=200_000,
                    help="target total deliveries per SSE scenario")
    ap.add_argument("--connections", type=int, nargs="*", default=[10, 100, 1000])
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--size", type=int, default=64)
    ap.add_argument("--ws-client", choices=["python", "rust"], default="python",
                    help="rust: bench/wsload, for the server's ceiling rather than the client's")
    ap.add_argument("--ws-threads", type=int, default=2,
                    help="runtime threads for the rust client")
    ap.add_argument("--strict", action="store_true",
                    help="refuse to measure when the host fails preflight")
    args = ap.parse_args()

    py = str(Path(args.python).absolute())
    most = max(args.subscribers + args.connections)
    session = Session("streams", executable=py, conns=most, strict=args.strict)
    session.announce()

    cmd = [py, "bench/streams_app.py", "--port", str(PORT)]
    if args.workers:
        cmd += ["--workers", str(args.workers)]
    server = subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                              start_new_session=True, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True)
    sse_rows, ws_rows = [], []
    try:
        if not wait_port(server):
            print(f"server failed to start: {(server.stderr.read() or '')[-600:]}", file=sys.stderr)
            sys.exit(1)

        print(f"SSE fan-out, {args.size}-byte payload, {args.client_procs} client processes")
        print(f"{'subs':>6}{'events':>8}{'deliv/s':>11}{'missing':>9}{'srv cpu ms/1k':>15}"
              f"{'client cores':>14}")
        for subs in args.subscribers:
            events = max(100, args.deliveries // subs)
            row = measure_sse(subs, events, args.client_procs, args.size)
            sse_rows.append(row)
            print(f"{subs:>6}{events:>8}{row['deliveries_per_s']:>11.0f}{row['missing']:>9}"
                  f"{row['server_cpu_ms_per_1000']:>15.1f}{row['client_cores']:>14.2f}"
                  f"{saturated(row)}", flush=True)
            time.sleep(1.0)

        print(f"\nWebSocket echo, {args.size}-byte message, {args.seconds:.0f}s per scenario")
        print(f"{'conns':>6}{'trips/s':>11}{'srv cpu ms/1k':>15}{'client cores':>14}")
        binary = wsload() if args.ws_client == "rust" else None
        for conns in args.connections:
            if binary:
                row = measure_ws_rust(binary, conns, args.seconds, args.ws_threads, args.size)
            else:
                row = measure_ws(conns, args.seconds, args.client_procs, args.size)
            ws_rows.append(row)
            print(f"{conns:>6}{row['round_trips_per_s']:>11.0f}"
                  f"{row['server_cpu_ms_per_1000']:>15.1f}{row['client_cores']:>14.2f}"
                  f"{saturated(row)}", flush=True)
            time.sleep(1.0)
    finally:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGINT)
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)

    out = session.finish({"args": vars(args), "sse": sse_rows, "websocket": ws_rows})
    print(f"\nsaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    mp.set_start_method("spawn")
    main()
