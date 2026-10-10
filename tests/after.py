#!/usr/bin/env python3
"""Work set aside with `request.after_response`, run once the response is out.

Each promise is checked against a server doing it, with timing where timing is
the promise: the client has its answer before the work starts; the work runs
in order, on the handler's own loop when it is async and off it when it is
not; it counts against `max_concurrency` while it runs; a graceful stop waits
for it; the client leaving does not cancel it. And the other side: a handler
that raises — whether the failure reaches the client as a `500` or is mapped
by an exception handler — runs none of what it set aside, and one piece of
work that raises does not stop the next.
"""
import asyncio
import logging
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager

import websockets
from oxbrook import SSE, App, HTTPError, Reply, Request
from oxbrook._core import Request as CoreRequest
from oxbrook.testing import TestClient

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class Captured(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def runs_after_the_response() -> None:
    events: list[tuple] = []

    @asynccontextmanager
    async def per_loop(_app):
        yield {"pool": object()}

    app = App(worker_lifespan=per_loop)

    @app.get("/")
    async def root(request: Request):
        handler_thread = threading.current_thread().name
        pool = request.state.pool

        async def first(tag, *, delay):
            events.append(("first start", time.monotonic()))
            await asyncio.sleep(delay)
            events.append(("first", tag, threading.current_thread().name == handler_thread,
                           request.state.pool is pool, time.monotonic()))

        def second(tag):
            time.sleep(0.05)
            events.append(("second", tag, threading.current_thread().name, time.monotonic()))

        request.after_response(first, "a", delay=0.3)
        request.after_response(second, "b")
        return {"ok": True}

    with TestClient(app) as client:
        started = time.monotonic()
        response = client.get("/")
        answered = time.monotonic()
        check(response.status_code == 200 and response.json() == {"ok": True},
              f"the response was {response.status_code} {response.text}")
        check(answered - started < 0.25, f"the client waited {answered - started:.2f}s "
                                          "for work meant for after the response")
        check(wait_until(lambda: any(e[0] == "second" for e in events)),
              f"the work did not all run: {events}")
    by_name = {e[0]: e for e in events}
    first, second = by_name.get("first"), by_name.get("second")
    if first and second:
        check(first[1] == "a" and second[1] == "b", f"arguments were lost: {events}")
        check(first[2], "async work did not run on the handler's loop")
        check(first[3], "async work did not see the loop's own request.state")
        check(second[2].startswith("oxbrook-blocking"),
              f"plain work ran on {second[2]!r}, not the blocking threadpool")
        check(second[3] >= first[4], "the work did not run in the order it was added")


def failures_drop_or_continue() -> None:
    ran: list[str] = []
    captured = Captured()
    logging.getLogger("oxbrook.after").addHandler(captured)

    class Mapped(Exception):
        pass

    def make(handlers: bool) -> App:
        app = App()
        if handlers:
            @app.exception_handler(Mapped)
            async def mapped(_request, _exc):
                return Reply({"mapped": True}, status=409)

        async def note(tag):
            ran.append(tag)

        @app.get("/crash")
        async def crash(request: Request):
            request.after_response(note, "crash")
            raise RuntimeError("boom")

        @app.get("/refuse")
        async def refuse(request: Request):
            request.after_response(note, "refuse")
            raise HTTPError(403)

        @app.get("/mapped")
        async def mapped_route(request: Request):
            request.after_response(note, "mapped")
            raise Mapped()

        @app.get("/chosen")
        async def chosen(request: Request):
            request.after_response(note, "chosen")
            return Reply({"no": True}, status=404)

        @app.get("/partial")
        async def partial(request: Request):
            async def broken():
                raise ValueError("broken work")

            def broken_sync():
                raise ValueError("broken sync work")

            request.after_response(broken)
            request.after_response(broken_sync)
            request.after_response(note, "after the broken ones")
            return {}

        return app

    for handlers in (False, True):
        ran.clear()
        captured.records.clear()
        with TestClient(make(handlers)) as client:
            statuses = {path: client.get(path).status_code
                        for path in ("/crash", "/refuse", "/mapped", "/chosen", "/partial")}
            wait_until(lambda: "after the broken ones" in ran)
            time.sleep(0.1)
        label = "with exception handlers" if handlers else "without"
        expected_mapped = 409 if handlers else 500
        check(statuses == {"/crash": 500, "/refuse": 403, "/mapped": expected_mapped,
                           "/chosen": 404, "/partial": 200},
              f"{label}: statuses were {statuses}")
        check(sorted(ran) == ["after the broken ones", "chosen"],
              f"{label}: the work that ran was {ran}; a raising handler's must not")
        logged = [r for r in captured.records if r.exc_info]
        check(len(logged) == 2, f"{label}: {len(logged)} failures were logged, not 2")
    logging.getLogger("oxbrook.after").removeHandler(captured)

    for bad in (5, "send_email", None):
        try:
            CoreRequest().after_response(bad)
            check(False, f"after_response({bad!r}) was accepted")
        except TypeError:
            pass


def counted_while_it_runs() -> None:
    release = threading.Event()
    app = App()

    @app.get("/slow-after")
    async def slow_after(request: Request):
        async def hold():
            while not release.is_set():
                await asyncio.sleep(0.01)

        request.after_response(hold)
        return {}

    with TestClient(app, workers=1, max_concurrency=2) as client:
        first = [client.get("/slow-after").status_code for _ in range(2)]
        third = client.get("/slow-after")
        check(first == [200, 200], f"the first two answered {first}")
        check(third.status_code == 503,
              f"with two requests' work running, a third got {third.status_code}, not 503: "
              "the work did not count against max_concurrency")
        release.set()
        check(wait_until(lambda: client.get("/slow-after").status_code == 200),
              "slots were not released when the work finished")


def a_cancelled_handler_frees_its_slot() -> None:
    ran: list[str] = []
    app = App()

    @app.get("/abandoned")
    async def abandoned(request: Request):
        async def note():
            ran.append("ran")

        request.after_response(note)
        await asyncio.sleep(5)
        return {}

    @app.get("/")
    async def root(_: Request):
        return {}

    with TestClient(app, workers=1, max_concurrency=1) as client:
        with socket.create_connection((client.host, client.port)) as raw:
            raw.sendall(b"GET /abandoned HTTP/1.1\r\nhost: x\r\n\r\n")
            time.sleep(0.2)
        # The client left before the answer: the handler is cancelled, its
        # work dropped, and the only slot on the only loop must come back.
        check(wait_until(lambda: client.get("/").status_code == 200, 3),
              "the slot of a cancelled handler with work set aside was never freed")
        time.sleep(0.2)
    check(ran == [], "a cancelled handler's work ran")


def a_stop_waits_for_it() -> None:
    done: list[float] = []
    app = App()

    @app.get("/")
    async def root(request: Request):
        async def finish():
            await asyncio.sleep(0.5)
            done.append(time.monotonic())

        request.after_response(finish)
        return {}

    client = TestClient(app, shutdown_grace=5).start()
    client.get("/")
    client.stop()
    check(len(done) == 1, "a graceful stop did not wait for after-response work")


def the_client_leaving_does_not_cancel_it() -> None:
    done: list[str] = []
    app = App()

    @app.get("/")
    async def root(request: Request):
        async def finish():
            await asyncio.sleep(0.3)
            done.append("done")

        request.after_response(finish)
        return {}

    with TestClient(app) as client:
        with socket.create_connection((client.host, client.port)) as raw:
            raw.sendall(b"GET / HTTP/1.1\r\nhost: x\r\n\r\n")
            check(raw.recv(4096).startswith(b"HTTP/1.1 200"), "no response on the raw socket")
        check(wait_until(lambda: done == ["done"], 3), "the client leaving cancelled the work")


def other_ways_in() -> None:
    ran: list[str] = []
    app = App()

    async def note(tag):
        ran.append(tag)

    @app.middleware
    async def audit(request, call_next):
        request.after_response(note, "middleware")
        return await call_next(request)

    @app.get("/stream")
    async def stream(request: Request):
        async def source():
            for i in range(3):
                await asyncio.sleep(0.05)
                yield i
            ran.append("stream ended")

        request.after_response(note, "after the stream")
        return SSE(source(), ping=None)

    @app.get("/search", tool=True)
    async def search(request: Request, q: str = ""):
        request.after_response(note, f"tool {q}")
        return {"q": q}

    @app.get("/broken-tool", tool=True)
    async def broken_tool(request: Request):
        request.after_response(note, "broken tool")
        raise RuntimeError("tool failed")

    @app.websocket("/ws")
    async def ws(request: Request, socket):
        request.after_response(note, "socket")
        await socket.send("hi")

    @app.websocket("/ws-broken")
    async def ws_broken(request: Request, socket):
        request.after_response(note, "broken socket")
        await socket.send("hi")
        raise RuntimeError("socket failed")

    async def open_socket(client: TestClient, path: str) -> None:
        async with client.websocket(path) as socket:
            await socket.recv()
            try:
                await socket.recv()
            except websockets.ConnectionClosed:
                pass

    with TestClient(app) as client:
        with client.stream("GET", "/stream") as response:
            for _ in response.iter_lines():
                pass
        check(wait_until(lambda: "after the stream" in ran),
              f"work after a stream did not run: {ran}")
        if "after the stream" in ran and "stream ended" in ran:
            check(ran.index("stream ended") < ran.index("after the stream"),
                  f"work ran before the stream ended: {ran}")

        check(client.call_tool("search", {"q": "x"}) == {"q": "x"}, "the tool call failed")
        check(wait_until(lambda: "tool x" in ran), f"a tool's work did not run: {ran}")
        result = client.mcp("tools/call", {"name": "broken_tool", "arguments": {}})
        check(result.get("isError") is True, "the broken tool did not report an error")

        asyncio.run(open_socket(client, "/ws"))
        check(wait_until(lambda: "socket" in ran), f"a socket's work did not run: {ran}")
        asyncio.run(open_socket(client, "/ws-broken"))
        time.sleep(0.2)
    check("middleware" in ran, f"work added by middleware did not run: {ran}")
    check("broken tool" not in ran, "a tool that raised had its work run")
    check("broken socket" not in ran, "a socket handler that raised had its work run")


def the_guide_example_works() -> None:
    sent: list[str] = []
    app = App()

    async def send_welcome_email(address: str) -> None:
        sent.append(address)

    @app.post("/users")
    async def create_user(request: Request):
        user = {"email": "ada@example.com"}
        request.after_response(send_welcome_email, user["email"])
        return Reply(user, status=201)

    with TestClient(app) as client:
        check(client.post("/users").status_code == 201, "the guide's route did not answer 201")
        check(wait_until(lambda: sent == ["ada@example.com"]), f"the email was not sent: {sent}")


def main() -> None:
    for step in (runs_after_the_response, failures_drop_or_continue, counted_while_it_runs,
                 a_cancelled_handler_frees_its_slot, a_stop_waits_for_it,
                 the_client_leaving_does_not_cancel_it, other_ways_in,
                 the_guide_example_works):
        try:
            step()
            print(f"  {step.__name__}: ok", flush=True)
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR", flush=True)

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
