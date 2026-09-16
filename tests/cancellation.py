#!/usr/bin/env python3
"""Handlers are cancelled when their client is gone.

Found on 2026-09-16: a client that sent a request, waited for its handler to
start and closed the connection left the handler running with its worker slot
held. 600 such requests in 0.64 s filled both workers of a test server, 200
handlers still running, and every other client got 503. HTTP/2 had the same
hole per stream, capped separately at 200 per connection.

What is asserted:

* a disconnect cancels the handler at its next `await`: `finally` runs,
  dependency teardown runs, middleware sees no exception, nothing is logged
  as an error, and the worker's capacity comes back;
* a request timeout cancels the handler as well as answering 504;
* an HTTP/2 stream reset cancels its handler;
* a request still queued when its client leaves never runs;
* `cancel_on_disconnect=False`, on the app or on a router, lets a handler
  finish after its client has left or its request has timed out;
* `asyncio.shield` protects a step inside a cancelled handler;
* a handler that has answered is not cancelled by a client leaving later;
* thousands of abandoned requests leave nothing behind.
"""
import asyncio
import contextlib
import logging
import socket
import sys
import threading
import time

import h2.config
import h2.connection
import httpx
from oxbrook import App, Depends, Request, Router
from oxbrook.testing import TestClient

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


events: list[str] = []
lock = threading.Lock()


def note(event: str) -> None:
    with lock:
        events.append(event)


def wait_for(event: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with lock:
            if event in events:
                return True
        time.sleep(0.01)
    return False


def seen(event: str) -> bool:
    with lock:
        return event in events


class Errors(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self.records.append(record.getMessage())


errors = Errors()
logging.getLogger("oxbrook").addHandler(errors)

app = App()


@app.middleware
async def watch(request, call_next):
    try:
        reply = await call_next(request)
    except Exception as exc:
        note(f"middleware saw {type(exc).__name__}")
        raise
    return reply


async def resource():
    note("dependency opened")
    try:
        yield "db"
    finally:
        # Awaits during teardown, as a pool release would.
        await asyncio.sleep(0.01)
        note("dependency closed")


@app.get("/slow/{tag}")
async def slow(_: Request, tag: str, db=Depends(resource)):
    note(f"{tag} started")
    try:
        await asyncio.sleep(3)
        note(f"{tag} finished")
    except asyncio.CancelledError:
        note(f"{tag} cancelled")
        raise
    finally:
        note(f"{tag} finally")
    return {"tag": tag}


@app.get("/kept/{tag}", cancel_on_disconnect=False)
async def kept(_: Request, tag: str):
    note(f"{tag} started")
    await asyncio.sleep(0.5)
    note(f"{tag} finished")
    return {"tag": tag}


@app.get("/shielded/{tag}")
async def shielded(_: Request, tag: str):
    async def must_finish():
        await asyncio.sleep(0.5)
        note(f"{tag} saved")

    note(f"{tag} started")
    await asyncio.shield(must_finish())
    note(f"{tag} after shield")
    return {}


@app.get("/answered/{tag}")
async def answered(_: Request, tag: str):
    note(f"{tag} started")
    return {"tag": tag}


@app.get("/block")
async def block(_: Request):
    note("block started")
    time.sleep(1.0)  # holds the loop on purpose
    return {}


@app.get("/counted/{tag}")
async def counted(_: Request, tag: str):
    note(f"{tag} ran")
    return {}


@app.get("/")
async def root(_: Request):
    return {}


routed = Router(prefix="/r")


@routed.get("/kept/{tag}", cancel_on_disconnect=False)
async def routed_kept(_: Request, tag: str):
    note(f"{tag} started")
    await asyncio.sleep(0.5)
    note(f"{tag} finished")
    return {}


app.include(routed)


def send_and_close(port: int, path: str, started: str | None) -> None:
    """Send a request, wait until its handler starts, and hang up."""
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    if started is not None:
        check(wait_for(started), f"{path}: handler never started")
    sock.close()


def disconnect(client: TestClient) -> None:
    send_and_close(client.port, "/slow/a", "a started")
    check(wait_for("a cancelled", 2), "a disconnect did not cancel the handler")
    check(wait_for("a finally"), "finally did not run")
    check(wait_for("dependency closed"), "dependency teardown did not run")
    time.sleep(0.1)
    check(not seen("a finished"), "the handler finished anyway")
    check(not any(e.startswith("middleware saw") for e in events),
          f"middleware saw an exception: {events}")
    check(not errors.records, f"cancellation was logged as an error: {errors.records}")


def timeout(client: TestClient) -> None:
    with TestClient(app, request_timeout=0.3) as short:
        reply = short.get("/slow/t")
        check(reply.status_code == 504, f"timeout answered {reply.status_code}")
        check(wait_for("t cancelled", 2), "a timed-out handler was not cancelled")
        kept_reply = short.get("/kept/tk")
        check(kept_reply.status_code == 504, f"kept timeout answered {kept_reply.status_code}")
        check(wait_for("tk finished", 2), "a kept handler did not finish after its timeout")


def reset(client: TestClient) -> None:
    conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    sock = socket.create_connection(("127.0.0.1", client.port))
    conn.initiate_connection()
    stream = conn.get_next_available_stream_id()
    conn.send_headers(stream, [(":method", "GET"), (":path", "/slow/h"), (":scheme", "http"),
                               (":authority", f"127.0.0.1:{client.port}")], end_stream=True)
    sock.sendall(conn.data_to_send())
    check(wait_for("h started"), "h2 handler never started")
    conn.reset_stream(stream)
    sock.sendall(conn.data_to_send())
    check(wait_for("h cancelled", 2), "an HTTP/2 reset did not cancel the handler")
    sock.close()


def opt_out(client: TestClient) -> None:
    send_and_close(client.port, "/kept/k", "k started")
    check(wait_for("k finished", 2), "cancel_on_disconnect=False was cancelled anyway")
    send_and_close(client.port, "/r/kept/rk", "rk started")
    check(wait_for("rk finished", 2), "a router's cancel_on_disconnect=False was ignored")


def shield(client: TestClient) -> None:
    send_and_close(client.port, "/shielded/s", "s started")
    check(wait_for("s saved", 2), "a shielded step did not finish")
    time.sleep(0.1)
    check(not seen("s after shield"), "the handler carried on past the shield")


def after_answer(client: TestClient) -> None:
    # Answered, then the client leaves: nothing left to cancel, nothing breaks.
    send_and_close(client.port, "/answered/x", "x started")
    check(client.get("/").status_code == 200, "server unhealthy after a late disconnect")


def queued(client: TestClient) -> None:
    # One worker, held by a handler that blocks its loop. Requests that arrive
    # meanwhile wait in the queue; their clients leave before the loop frees.
    blocker = threading.Thread(target=lambda: httpx.get(f"{client.base_url}/block", timeout=10))
    blocker.start()
    check(wait_for("block started"), "the blocking handler never started")
    socks = []
    for i in range(20):
        sock = socket.create_connection(("127.0.0.1", client.port))
        sock.sendall(f"GET /counted/q{i} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        socks.append(sock)
    # Long enough to be queued: closed at once, hyper drops a request before
    # it is ever dispatched, and the case would prove nothing.
    time.sleep(0.1)
    for sock in socks:
        sock.close()
    # Only the tokio side can see these closes, and it is not blocked.
    time.sleep(0.2)
    blocker.join()
    time.sleep(0.3)
    ran = [e for e in events if e.startswith("q") and e.endswith(" ran")]
    check(not ran, f"{len(ran)} abandoned queued requests still ran")
    check(client.get("/counted/after").status_code == 200, "queue unusable afterwards")


def capacity() -> None:
    # Two slots. Abandon many more requests than that; capacity must return.
    with TestClient(app, workers=1, max_concurrency=2) as small:
        for i in range(10):
            send_and_close(small.port, f"/slow/c{i}", None)
            time.sleep(0.05)
        time.sleep(0.3)
        check(small.get("/").status_code == 200, "slots were not released by cancellation")

    async def flood(port: int) -> None:
        async def one(i: int) -> None:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET /slow/f{i} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        for start in range(0, 3000, 300):
            await asyncio.gather(*(one(i) for i in range(start, start + 300)))

    with TestClient(app, workers=2, max_concurrency=500) as busy:
        asyncio.run(flood(busy.port))
        time.sleep(0.5)
        with lock:
            started = sum(1 for e in events if e.startswith("f") and e.endswith(" started"))
            cancelled = sum(1 for e in events if e.startswith("f") and e.endswith(" cancelled"))
            finished = sum(1 for e in events if e.startswith("f") and e.endswith(" finished"))
        check(started > 1000, f"flood reached only {started} handlers; test is void")
        check(cancelled == started and finished == 0,
              f"flood: {started} started, {cancelled} cancelled, {finished} finished")
        replies = [busy.get("/").status_code for _ in range(20)]
        check(replies == [200] * 20, f"server after the flood: {replies}")


def main() -> None:
    with TestClient(app, workers=1) as client:
        steps = [disconnect, timeout, reset, opt_out, shield, after_answer, queued]
        for step in steps:
            try:
                step(client)
                print(f"  {step.__name__}: ok")
            except Exception as exc:
                failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
                print(f"  {step.__name__}: ERROR")
    try:
        capacity()
        print("  capacity: ok")
    except Exception as exc:
        failures.append(f"capacity raised {type(exc).__name__}: {exc}")

    check(not errors.records, f"errors logged: {errors.records[:3]}")
    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
