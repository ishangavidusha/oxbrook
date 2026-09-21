#!/usr/bin/env python3
"""Regression tests for defects found against a running server.

Each of these was demonstrated before being fixed:

* `HEAD` returned 405 on every route, which violates HTTP.
* A crashing handler returned its exception text to the client. The probe used
  a fake database URL with a password in it and the client received it.
* A request body was buffered without limit. One 200MB POST took the server
  from 44MB to 836MB resident.
* A refusal decided before the body was read — no such route, wrong method, a
  path parameter that will not coerce, no capacity — left the body in the
  socket and closed on top of it. That is a reset, and on Windows a reset
  discards what the client had already buffered, so the answer was replaced by
  `[WinError 10053] An established connection was aborted`. Linux and macOS
  won the race often enough to hide it, but paid for it in a connection that
  could not be reused. Asserted here through that second cost, which is
  visible on every platform: the connection stays alive.
"""
import socket
import sys
import threading

import httpx
from oxbrook import App, Request

PORT = 8807
DEBUG_PORT = 8808
BASE = f"http://127.0.0.1:{PORT}"
LIMIT = 64 * 1024

SECRET = "db://user:hunter2@internal-host/prod"

app = App(openapi_url=None, docs_url=None)


@app.get("/hello")
async def hello(_: Request):
    return {"hello": "world"}


@app.post("/only-post")
async def only_post(_: Request):
    return {"ok": True}


@app.post("/items/{item_id}")
async def item(_: Request, item_id: int):
    return {"id": item_id}


@app.post("/echo")
async def echo(req: Request):
    return {"len": len(req.body)}


@app.get("/boom")
async def boom(_: Request):
    raise ValueError(f"connection failed for {SECRET}")


@app.websocket("/ws")
async def ws(_: Request, socket):
    async for message in socket:
        await socket.send(message)


debug_app = App(openapi_url=None, docs_url=None, debug=True)


@debug_app.get("/boom")
async def debug_boom(_: Request):
    raise ValueError(f"connection failed for {SECRET}")


def check(failures, cond, msg):
    if not cond:
        failures.append(msg)


def main() -> None:
    failures: list[str] = []
    threading.Thread(
        target=lambda: app.run(port=PORT, max_body=LIMIT), daemon=True
    ).start()
    threading.Thread(target=lambda: debug_app.run(port=DEBUG_PORT), daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/hello", timeout=0.3)
            httpx.get(f"http://127.0.0.1:{DEBUG_PORT}/boom", timeout=0.3)
            break
        except Exception:
            threading.Event().wait(0.1)

    with httpx.Client(base_url=BASE, timeout=30) as c:
        # --- HEAD ---
        body = c.get("/hello").content
        r = c.head("/hello")
        check(failures, r.status_code == 200, f"HEAD on a GET route returned {r.status_code}")
        check(failures, r.content == b"", "HEAD returned a body")
        check(
            failures,
            r.headers.get("content-length") == str(len(body)),
            f"HEAD content-length was {r.headers.get('content-length')!r}, "
            f"expected {len(body)}",
        )
        check(
            failures,
            r.headers.get("content-type") == "application/json",
            f"HEAD content-type was {r.headers.get('content-type')!r}",
        )
        check(
            failures,
            c.head("/only-post").status_code == 405,
            "HEAD on a POST-only route should be 405",
        )
        check(failures, c.head("/nope").status_code == 404, "HEAD on an unknown path")
        check(
            failures,
            c.head("/ws").status_code in (405, 426),
            f"HEAD on a socket route returned {c.head('/ws').status_code}",
        )

        # --- a refusal before the body is read keeps the connection ---
        for label, request, status in (
            ("no such route", b"POST /nope HTTP/1.1", b"404"),
            ("wrong method", b"PUT /only-post HTTP/1.1", b"405"),
            ("bad path parameter", b"POST /items/xyz HTTP/1.1", b"422"),
        ):
            body = b"a" * 200_000
            sock = socket.create_connection(("127.0.0.1", PORT), timeout=10)
            try:
                sock.sendall(request + b"\r\nHost: x\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
                first = sock.recv(4096)
                check(failures, status in first.split(b"\r\n")[0],
                      f"{label}: answered {first[:40]!r}, expected {status!r}")
                # The refusal drained the body, so this connection is still
                # good. Unread, hyper closes it, and this is a broken pipe or
                # an empty read.
                try:
                    sock.sendall(b"GET /hello HTTP/1.1\r\nHost: x\r\n\r\n")
                    second = sock.recv(4096)
                except OSError as exc:
                    second = f"<{type(exc).__name__}: {exc}>".encode()
                check(failures, b"200" in second.split(b"\r\n")[0],
                      f"{label}: the connection did not survive the refusal, "
                      f"so the body was left unread: {second[:60]!r}")
            finally:
                sock.close()

        # --- error responses must not leak ---
        r = c.get("/boom")
        check(failures, r.status_code == 500, f"crashing handler returned {r.status_code}")
        check(failures, SECRET not in r.text, "500 response leaked the exception text")
        check(
            failures,
            "ValueError" not in r.text,
            "500 response leaked the exception type",
        )

    with httpx.Client(base_url=f"http://127.0.0.1:{DEBUG_PORT}", timeout=10) as c:
        r = c.get("/boom")
        check(
            failures,
            SECRET in r.text,
            "debug=True should include the exception detail, for development",
        )

    with httpx.Client(base_url=BASE, timeout=60) as c:
        # --- body limit ---
        ok = c.post("/echo", content=b"x" * (LIMIT - 1024))
        check(failures, ok.status_code == 200, f"body under the limit returned {ok.status_code}")
        big = c.post("/echo", content=b"x" * (LIMIT * 4))
        check(failures, big.status_code == 413, f"oversized body returned {big.status_code}")
        # The server must still be healthy afterwards.
        check(failures, c.get("/hello").status_code == 200, "server unhealthy after a 413")

    print(f"checks: {'PASS' if not failures else 'FAIL'}")
    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
