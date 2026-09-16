#!/usr/bin/env python3
"""WebSocket: handshake, framing, close, parameters, and cross-loop broadcast."""
import asyncio
import sys
import threading

import httpx
import websockets
from oxbrook import App, Request, Response
from pydantic import BaseModel

PORT = 8805
BASE = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}"
WORKERS = 4
CLIENTS = 8

app = App(openapi_url="/openapi.json", docs_url=None)


class Note(BaseModel):
    id: int
    text: str


@app.websocket("/echo")
async def echo(_: Request, ws):
    async for message in ws:
        await ws.send(message)


@app.websocket("/rooms/{room}")
async def room(_: Request, ws, room: str, verbose: bool = False):
    await ws.send({"room": room, "verbose": verbose})
    async for message in ws:
        await ws.send(f"{room}: {message}")


@app.websocket("/shapes")
async def shapes(_: Request, ws):
    await ws.send("text")
    await ws.send(b"bytes")
    await ws.send({"a": 1})
    await ws.send(Note(id=7, text="model"))
    await ws.close()


@app.websocket("/feed")
async def feed(_: Request, ws):
    async with app.topic("bus").subscribe() as sub:
        async for item in sub:
            await ws.send(item)


@app.websocket("/counted")
async def counted(_: Request, ws):
    async for _message in ws:
        pass
    # Reached only after the client closes, which is what makes it a useful
    # check that a peer close ends iteration.
    app.topic("closed").emit_nowait("done")


async def members_only(request: Request):
    """Refuses before the handshake, which the handler cannot do: by the time
    it runs the 101 has been sent and the client believes it is connected."""
    if request.header("authorization") != "Bearer good":
        return Response(b'{"error":"unauthorized"}', status=401)


@app.websocket("/private", authorize=members_only)
async def private(_: Request, ws):
    await ws.send("welcome")


@app.websocket("/private/{room}", authorize=members_only)
async def private_room(_: Request, ws, room: str, level: int = 1):
    """An authorizer *and* path parameters.

    Rust coverage showed this combination had never run: the authorizer is
    dispatched as a separate queue item, and its parameters are copied for it,
    so a gated route with no parameters never exercises the copy. Both routes
    existed; only the pairing was missing.
    """
    await ws.send(f"{room}:{level}")


@app.post("/publish/{text}")
async def publish(_: Request, text: str):
    return {"delivered": await app.topic("bus").emit({"text": text})}


@app.get("/plain")
async def plain(_: Request):
    return {"ok": True}


async def run() -> list[str]:
    bad: list[str] = []

    # A gated route that also has parameters: refused without the header,
    # and given its coerced parameters once accepted.
    try:
        async with websockets.connect(f"{WS}/private/lounge?level=7") as ws:
            bad.append("a gated socket with parameters accepted an unauthorized client")
    except Exception:
        pass
    async with websockets.connect(
        f"{WS}/private/lounge?level=7", additional_headers={"authorization": "Bearer good"}
    ) as ws:
        got = await asyncio.wait_for(ws.recv(), 5)
        if got != "lounge:7":
            bad.append(f"a gated socket with parameters received {got!r}")

    async with websockets.connect(f"{WS}/echo") as ws:
        await ws.send("hello")
        got = await asyncio.wait_for(ws.recv(), 5)
        if got != "hello":
            bad.append(f"text echo returned {got!r}")
        await ws.send(b"\x00\x01\x02")
        got = await asyncio.wait_for(ws.recv(), 5)
        if got != b"\x00\x01\x02":
            bad.append(f"binary echo returned {got!r}")
        big = "x" * 200_000
        await ws.send(big)
        got = await asyncio.wait_for(ws.recv(), 10)
        if got != big:
            bad.append(f"large frame round-trip failed ({len(got)} bytes back)")

    # Path and query parameters reach a socket handler.
    async with websockets.connect(f"{WS}/rooms/lobby?verbose=true") as ws:
        first = await asyncio.wait_for(ws.recv(), 5)
        if first != '{"room":"lobby","verbose":true}':
            bad.append(f"socket params gave {first!r}")
        await ws.send("hi")
        got = await asyncio.wait_for(ws.recv(), 5)
        if got != "lobby: hi":
            bad.append(f"room echo returned {got!r}")

    # Payload shapes, then a server-initiated close.
    async with websockets.connect(f"{WS}/shapes") as ws:
        want = ["text", b"bytes", '{"a":1}', '{"id":7,"text":"model"}']
        for expected in want:
            got = await asyncio.wait_for(ws.recv(), 5)
            if got != expected:
                bad.append(f"shape mismatch: got {got!r}, expected {expected!r}")
        try:
            await asyncio.wait_for(ws.recv(), 5)
            bad.append("server close did not end the stream")
        except websockets.ConnectionClosed:
            pass

    # A client close must end handler iteration rather than hang.
    async with websockets.connect(f"{WS}/counted") as ws:
        await ws.send("one")
    await asyncio.sleep(0.3)

    # Cross-loop broadcast: more sockets than worker loops, one publish.
    sockets = [await websockets.connect(f"{WS}/feed") for _ in range(CLIENTS)]
    try:
        for _ in range(100):
            if app.topic("bus").subscribers == CLIENTS:
                break
            await asyncio.sleep(0.05)
        else:
            bad.append(f"only {app.topic('bus').subscribers} of {CLIENTS} subscribed")

        async with httpx.AsyncClient(base_url=BASE, timeout=10) as client:
            result = (await client.post("/publish/broadcast")).json()
        if result["delivered"] != CLIENTS:
            bad.append(f"publish reached {result['delivered']} of {CLIENTS}")

        for i, ws in enumerate(sockets):
            try:
                got = await asyncio.wait_for(ws.recv(), 5)
            except Exception as e:
                bad.append(f"socket {i} received nothing: {type(e).__name__}")
                continue
            if got != '{"text":"broadcast"}':
                bad.append(f"socket {i} received {got!r}")
    finally:
        for ws in sockets:
            await ws.close()

    # An authorizer must be able to refuse an upgrade outright.
    try:
        async with websockets.connect(f"{WS}/private"):
            bad.append("an unauthorized socket was accepted")
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status != 401:
            bad.append(f"refused socket gave {type(exc).__name__} {status}, expected 401")

    async with websockets.connect(
        f"{WS}/private", additional_headers={"Authorization": "Bearer good"}
    ) as ws:
        greeting = await asyncio.wait_for(ws.recv(), 5)
        if greeting != "welcome":
            bad.append(f"authorized socket received {greeting!r}")

    # Subscriptions must be released when sockets go away.
    for _ in range(100):
        if app.topic("bus").subscribers == 0:
            break
        await asyncio.sleep(0.05)
    else:
        bad.append(f"{app.topic('bus').subscribers} socket subscriptions leaked")

    return bad


def http_checks() -> list[str]:
    bad = []
    with httpx.Client(base_url=BASE, timeout=5) as c:
        r = c.get("/echo")
        if r.status_code != 426:
            bad.append(f"plain GET to a socket route returned {r.status_code}, expected 426")
        r = c.get("/nope-ws")
        if r.status_code != 404:
            bad.append(f"unknown path returned {r.status_code}")
        doc = c.get("/openapi.json").json()
        for path in ("/echo", "/rooms/{room}", "/feed"):
            if path in doc["paths"]:
                bad.append(f"websocket route {path} leaked into the OpenAPI document")
        if "/plain" not in doc["paths"]:
            bad.append("ordinary routes missing from the OpenAPI document")
    return bad


def registration_checks() -> list[str]:
    bad = []

    def one_arg():
        bad_app = App()

        @bad_app.websocket("/x")
        async def h(_: Request):
            pass

    def with_body():
        bad_app = App()

        @bad_app.websocket("/x")
        async def h(_: Request, ws, body: Note):
            pass

    for label, fn in [("socket handler with no ws argument", one_arg),
                      ("socket handler with a body model", with_body)]:
        try:
            fn()
            bad.append(f"{label}: no error raised")
        except TypeError:
            pass
        except Exception as e:
            bad.append(f"{label}: raised {type(e).__name__}: {e}")
    return bad


async def message_size_cap() -> list[str]:
    """A single message larger than the limit closes the connection.

    Before this was configurable the limit was tungstenite's own 64 MiB —
    four times what the same server accepts as a request body, and nothing the
    application could change. Its own server, because the cap is set per
    server and the shared one deliberately runs with the default.
    """
    from oxbrook.testing import free_port

    bad: list[str] = []
    capped = App(openapi_url=None, docs_url=None, mcp_url=None)

    @capped.websocket("/echo")
    async def echo_capped(_: Request, ws):
        async for message in ws:
            await ws.send(f"got {len(message)}")

    port = free_port()
    server = capped.build_server("127.0.0.1", port, workers=1, max_message=4096)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    await asyncio.sleep(0.4)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/echo", max_size=None) as ws:
            await ws.send("x" * 1000)
            got = await asyncio.wait_for(ws.recv(), 5)
            if got != "got 1000":
                bad.append(f"a message under the cap returned {got!r}")

        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}/echo", max_size=None) as ws:
                await ws.send("x" * 20000)
                got = await asyncio.wait_for(ws.recv(), 5)
                bad.append(f"a message over the cap was accepted and returned {got!r}")
        except Exception:
            pass  # Refused, which is the point.
    finally:
        server.shutdown()
        thread.join(timeout=10)
    return bad


async def sockets_hold_connection_slots() -> list[str]:
    """An open socket counts against `max_connections` until it closes.

    The slot belonged to the connection task, and hyper ends that task when it
    hands the connection over to the upgrade, so every socket released its slot
    at the handshake: with `max_connections=2`, five sockets stayed open and
    plain requests were still accepted beside them.
    """
    from oxbrook.testing import TestClient

    bad: list[str] = []
    limited = App(openapi_url=None, docs_url=None, mcp_url=None)

    @limited.get("/plain")
    async def limited_plain(_: Request):
        return {"ok": True}

    @limited.websocket("/echo")
    async def limited_echo(_: Request, ws):
        async for message in ws:
            await ws.send(message)

    with TestClient(limited, workers=1, max_connections=2) as client:
        url = f"{client.ws_url}/echo"
        first = await websockets.connect(url)
        second = await websockets.connect(url)
        for sock in (first, second):
            await sock.send("hi")
            await asyncio.wait_for(sock.recv(), 5)

        async with httpx.AsyncClient(timeout=10) as http:
            waiting = asyncio.ensure_future(http.get(f"{client.base_url}/plain"))
            await asyncio.sleep(0.5)
            if waiting.done():
                bad.append("a request was served while two sockets held both slots")
            await first.close()
            try:
                reply = await asyncio.wait_for(waiting, 5)
                if reply.status_code != 200:
                    bad.append(f"the waiting request got {reply.status_code}")
            except Exception as e:
                bad.append(f"closing a socket did not free its slot: {type(e).__name__}")
        await second.close()
    return bad


def main() -> None:
    failures = registration_checks()
    print(f"registration checks: {'PASS' if not failures else 'FAIL'}")

    threading.Thread(target=lambda: app.run(port=PORT, workers=WORKERS), daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/plain", timeout=0.3)
            break
        except Exception:
            threading.Event().wait(0.1)

    http = http_checks()
    print(f"http checks:         {'PASS' if not http else 'FAIL'}")
    failures += http

    ws = asyncio.run(run())
    ws += asyncio.run(message_size_cap())
    ws += asyncio.run(sockets_hold_connection_slots())
    print(f"socket checks:       {'PASS' if not ws else 'FAIL'} "
          f"({WORKERS} loops, {CLIENTS} sockets)")
    failures += ws

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
