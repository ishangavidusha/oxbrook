#!/usr/bin/env python3
"""A throughput sanity check that needs no load generator.

    python bench/smoke.py --seconds 5 --conns 32

**This is not a benchmark.** `bench/run.py` is, and it refuses to measure on a
host it cannot trust — a shared CI virtual machine is exactly such a host, and
`oha` is not installed there anyway. The question here is the cruder one a port
has to answer: does dispatch work under concurrency at all, and is the rate
within sight of what the same code does elsewhere? A number from here belongs
in a session note, never in the documentation.

The load generator is asyncio holding a fixed number of keep-alive connections,
each sending the next request as soon as the last reply is complete. It is a
Python client, so on a fast machine it — not the server — is what the figure
measures. That is fine for the question being asked.
"""
import argparse
import asyncio
import statistics
import sys
import time

from oxbrook import App, Request
from oxbrook._workers import default_workers, describe, gil_enabled
from oxbrook.testing import TestClient

app = App(openapi_url=None, docs_url=None, mcp_url=None)


@app.get("/")
async def hello(_: Request):
    return {"hello": "world"}


REQUEST = b"GET / HTTP/1.1\r\nHost: bench\r\n\r\n"


async def one_connection(port: int, deadline: float) -> int:
    """Keep one connection busy until the deadline, and say how many it served."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    served = 0
    try:
        while time.monotonic() < deadline:
            writer.write(REQUEST)
            await writer.drain()
            # One request in flight per connection, so the reply ends at the
            # first blank line plus its content length. Read the head once,
            # then exactly the body.
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            if length:
                await reader.readexactly(length)
            served += 1
    finally:
        writer.close()
    return served


async def measure(port: int, conns: int, seconds: float) -> float:
    deadline = time.monotonic() + seconds
    started = time.monotonic()
    counts = await asyncio.gather(*(one_connection(port, deadline) for _ in range(conns)))
    return sum(counts) / (time.monotonic() - started)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=5.0, help="per round")
    parser.add_argument("--conns", type=int, default=32)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args()

    gil = "GIL" if gil_enabled() else "free-threaded"
    workers = args.workers or default_workers()
    print(f"python     : {sys.version.split()[0]}, {gil}, {sys.platform}")
    print(f"detection  : {describe()}")
    print(f"serving    : {workers} worker loop(s), {args.conns} connections, "
          f"{args.rounds} x {args.seconds:g}s")

    rates = []
    with TestClient(app, workers=workers) as client:
        # One short round first: the first requests on a fresh process pay for
        # imports and the first task on each loop.
        asyncio.run(measure(client.port, args.conns, 1.0))
        for round_number in range(1, args.rounds + 1):
            rate = asyncio.run(measure(client.port, args.conns, args.seconds))
            rates.append(rate)
            print(f"round {round_number}    : {rate:,.0f} req/s")

    print(f"median     : {statistics.median(rates):,.0f} req/s")
    # A rate this low means dispatch is not working, not that the host is slow:
    # a single-threaded Python client alone manages thousands per second.
    if statistics.median(rates) < 1000:
        print("\nRESULT: FAIL - dispatch is not serving at a plausible rate")
        return 1
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
