#!/usr/bin/env python3
"""Which origins may open a WebSocket.

Browsers do not apply CORS to WebSockets, and they attach the user's cookies to
the handshake. Demonstrated against a running server before the fix: with CORS
configured for one frontend, a page on `https://evil.example` opened the socket,
got `101`, and the handler ran with the victim's session cookie — cross-site
WebSocket hijacking. Every socket authenticated by cookie was usable by any site
the user visited.

The upgrade now checks `Origin` in Rust, before an authorizer or handler runs.
These cases assert what is accepted — no `Origin`, the server's own origin by
`Host` or `X-Forwarded-Host`, listed origins — and that everything else is a
`403` that never reaches application code.

Raw sockets throughout: a WebSocket client library sets `Host` and `Origin`
itself, which are exactly the headers under test.
"""
import socket
import sys
import threading
import time

from oxbrook import CORS, App, HTTPError
from oxbrook.testing import TestClient

FRONTEND = "https://app.example.com"
EVIL = "https://evil.example"
failures: list[str] = []
reached: list[str] = []
lock = threading.Lock()


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def make_app(**options) -> App:
    app = App(openapi_url=None, docs_url=None, mcp_url=None, **options)

    async def authorize(request):
        with lock:
            reached.append(f"authorizer {request.header('origin')}")

    @app.websocket("/live", authorize=authorize)
    async def live(request, ws):
        with lock:
            reached.append(f"handler {request.header('origin')}")
        await ws.close()

    @app.websocket("/open")
    async def open_socket(request, ws):
        with lock:
            reached.append(f"handler {request.header('origin')}")
        await ws.close()

    return app


def handshake(port: int, path: str = "/live", origin: str | None = None,
              host: str | None = None, forwarded: str | None = None) -> tuple[int, str]:
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host or f'127.0.0.1:{port}'}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version: 13",
        "Cookie: session=victim",
    ]
    if origin is not None:
        lines.append(f"Origin: {origin}")
    if forwarded is not None:
        lines.append(f"X-Forwarded-Host: {forwarded}")
    sock = socket.create_connection(("127.0.0.1", port))
    sock.settimeout(3)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    data = sock.recv(1024)
    sock.close()
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, body.decode("utf-8", "replace")


def expect(port: int, label: str, want: int, **kwargs) -> None:
    status, body = handshake(port, **kwargs)
    check(status == want, f"{label}: expected {want}, got {status} {body!r}")


def refused_origins_reach_nothing(client: TestClient) -> None:
    """The demonstrated defect."""
    reached.clear()
    for path in ("/live", "/open"):
        status, body = handshake(client.port, path=path, origin=EVIL)
        check(status == 403, f"a foreign page opening {path} got {status}, expected 403")
        check("websocket_origins" in body, f"the refusal does not say how to allow it: {body!r}")
    time.sleep(0.2)
    check(not reached, f"a refused handshake reached application code: {reached}")

    expect(client.port, "an origin of null", 403, origin="null")
    expect(client.port, "a lookalike subdomain", 403, origin="https://app.example.com.evil.example")
    expect(client.port, "the frontend over plain http", 403, origin="http://app.example.com")


def accepted_origins(client: TestClient) -> None:
    port = client.port
    expect(port, "no Origin header (not a browser)", 101)
    expect(port, "the server's own origin", 101, origin=f"http://127.0.0.1:{port}")
    expect(port, "a CORS origin", 101, origin=FRONTEND)
    expect(port, "a CORS origin in another case", 101, origin="HTTPS://APP.EXAMPLE.COM")
    expect(port, "same origin behind a proxy that rewrote Host", 101,
           origin="https://api.example.com", host="127.0.0.1:8000",
           forwarded="api.example.com")
    expect(port, "same origin with the default port spelled out in Host", 101,
           origin="https://api.example.com", host="api.example.com:443")
    expect(port, "a proxy header naming a different host", 403,
           origin=EVIL, host="127.0.0.1:8000", forwarded="api.example.com")


def cors_wildcard_does_not_open_sockets(client: TestClient) -> None:
    """CORS `*` forbids credentials; a handshake always carries them."""
    expect(client.port, "a foreign page under CORS *", 403, origin=EVIL)
    expect(client.port, "the server's own origin under CORS *", 101,
           origin=f"http://127.0.0.1:{client.port}")


def explicit_list_replaces_cors(client: TestClient) -> None:
    expect(client.port, "a listed websocket origin", 101, origin="https://sockets.example.com")
    expect(client.port, "a CORS origin not in the explicit list", 403, origin=FRONTEND)
    expect(client.port, "same origin with an explicit list", 101,
           origin=f"http://127.0.0.1:{client.port}")


def wildcard_opts_out(client: TestClient) -> None:
    expect(client.port, "any origin with websocket_origins=['*']", 101, origin=EVIL)


def no_cors_means_same_origin_only(client: TestClient) -> None:
    expect(client.port, "a foreign page with no CORS configured", 403, origin=EVIL)
    expect(client.port, "no Origin with no CORS configured", 101)


def authorizers_still_run_for_accepted_origins() -> None:
    async def refuse(request):
        raise HTTPError(401)

    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.websocket("/guarded", authorize=refuse)
    async def guarded(request, ws):
        await ws.close()

    with TestClient(app, workers=1) as client:
        status, _ = handshake(client.port, path="/guarded",
                              origin=f"http://127.0.0.1:{client.port}")
        check(status == 401, f"an accepted origin skipped the authorizer: {status}")


def configuration_is_checked() -> None:
    for label, value, kind in [
        ("a single string", "https://a.com", TypeError),
        ("a trailing slash", ["https://a.com/"], ValueError),
        ("no scheme", ["a.com"], ValueError),
    ]:
        try:
            App(websocket_origins=value)
            failures.append(f"websocket_origins accepted {label}")
        except kind:
            pass


def patterns_apply_to_sockets(client: TestClient) -> None:
    """I-057: the same origin patterns as CORS, matched the same way."""
    from cors import PATTERNED

    for origin, allowed in PATTERNED:
        expect(client.port, f"a handshake from {origin}", 101 if allowed else 403,
               path="/open", origin=origin)


def main() -> None:
    for step in (configuration_is_checked, authorizers_still_run_for_accepted_origins):
        try:
            step()
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR")

    for options, steps in [
        ({"cors": CORS(allow_origins=[FRONTEND])},
         (refused_origins_reach_nothing, accepted_origins)),
        ({"cors": CORS(allow_origins=["*"])}, (cors_wildcard_does_not_open_sockets,)),
        ({"cors": CORS(allow_origins=[FRONTEND]),
          "websocket_origins": ["https://sockets.example.com"]}, (explicit_list_replaces_cors,)),
        ({"websocket_origins": ["*"]}, (wildcard_opts_out,)),
        ({}, (no_cors_means_same_origin_only,)),
        ({"websocket_origins": ["https://*.preview.example.dev",
                                "https://app-*-team.example.com"]},
         (patterns_apply_to_sockets,)),
    ]:
        with TestClient(make_app(**options), workers=1) as client:
            for step in steps:
                try:
                    step(client)
                    print(f"  {step.__name__}: ok")
                except Exception as exc:
                    failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
                    print(f"  {step.__name__}: ERROR")

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
