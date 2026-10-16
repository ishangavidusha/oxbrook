#!/usr/bin/env python3
"""What happens when something is wrong: bad registration, and bad responses.

Two areas the suites did not cover until 2026-09-09, both probed against a
running server first.

**Route conflicts (I-034).** The radix tree refuses two routes of the same
shape, but it is built when the server starts serving — so a duplicate route
raised on whatever thread called `serve`. Through the test client that thread
is a background one: the exception vanished and the caller saw a connection
refused with no explanation. Conflicts are now refused at registration, where
the traceback points at the decorator that caused it.

**Failure paths (I-035).** A handler that raises has always answered 500 and
logged. A *response* that cannot be built did neither: the exception escaped
into the asyncio task, the responder was dropped without a reply, and the
client got the connection-level fallback while the traceback went to asyncio's
default handler.

Trailing slashes are asserted here too, because "undefined" was the finding:
`/x` and `/x/` are different routes, and neither redirects to the other.
"""
import socket
import sys
import time

import httpx
from oxbrook import App, Depends, Request
from oxbrook.testing import TestClient, free_port

failures: list[str] = []

SECRET = "postgres://user:hunter2@internal/prod"


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


# --------------------------------------------------------------------------
# I-034: registration
# --------------------------------------------------------------------------
def refuses(build) -> str | None:
    """Return the error message, or None if registration was accepted."""
    app = App(openapi_url=None, docs_url=None, mcp_url=None)
    try:
        build(app)
    except ValueError as exc:
        return str(exc)
    return None


def conflicting_routes_are_refused() -> None:
    def duplicate(app):
        @app.get("/dup")
        async def first(_: Request):
            return {}

        @app.get("/dup")
        async def second(_: Request):
            return {}

    def same_shape(app):
        @app.get("/a/{x}")
        async def first(_: Request, x: str):
            return {}

        @app.get("/a/{y}")
        async def second(_: Request, y: str):
            return {}

    def catch_all_against_param(app):
        @app.get("/f/{*rest}")
        async def first(_: Request, rest: str):
            return {}

        @app.get("/f/{name}")
        async def second(_: Request, name: str):
            return {}

    def socket_against_get(app):
        @app.get("/ws")
        async def plain(_: Request):
            return {}

        @app.websocket("/ws")
        async def socket(request, ws):
            return None

    for label, build in [
        ("the same route twice", duplicate),
        ("paths differing only in parameter name", same_shape),
        ("a catch-all against a parameter", catch_all_against_param),
        ("a socket against a GET on the same path", socket_against_get),
    ]:
        message = refuses(build)
        check(message is not None, f"{label} was accepted; the router cannot hold both")
        if message:
            check(
                "conflicts with" in message,
                f"{label} raised an unhelpful message: {message!r}",
            )


def distinguishable_routes_are_accepted() -> None:
    """The check must not be so eager that it refuses what the router accepts."""

    def static_and_dynamic(app):
        @app.get("/users/{user_id}")
        async def one(_: Request, user_id: str):
            return {}

        @app.get("/users/me")
        async def me(_: Request):
            return {}

    def catch_all_and_static(app):
        @app.get("/f/{*rest}")
        async def rest(_: Request, rest: str):
            return {}

        @app.get("/f/readme")
        async def readme(_: Request):
            return {}

    def different_methods(app):
        @app.get("/thing")
        async def read(_: Request):
            return {}

        @app.post("/thing")
        async def write(_: Request):
            return {}

    def diverging_after_a_parameter(app):
        @app.get("/p/{a}/q")
        async def first(_: Request, a: str):
            return {}

        @app.get("/p/{b}/r")
        async def second(_: Request, b: str):
            return {}

    for label, build in [
        ("a static route beside a dynamic one", static_and_dynamic),
        ("a static route beside a catch-all", catch_all_and_static),
        ("two methods on one path", different_methods),
        ("paths that diverge after a parameter", diverging_after_a_parameter),
    ]:
        message = refuses(build)
        check(message is None, f"{label} was refused: {message}")


# --------------------------------------------------------------------------
# I-035: failure paths
# --------------------------------------------------------------------------
app = App(openapi_url=None, docs_url=None, mcp_url=None)


@app.get("/ok")
async def ok(_: Request):
    return {"ok": True}


@app.get("/x")
async def x(_: Request):
    return {"path": "/x"}


@app.get("/y/")
async def y(_: Request):
    return {"path": "/y/"}


@app.get("/before")
async def before(_: Request):
    return {"never": True}


@app.get("/after")
async def after(_: Request):
    return {"reached": True}


@app.middleware
async def sometimes_explodes(request, call_next):
    if request.path == "/before":
        raise RuntimeError(f"middleware failed: {SECRET}")
    reply = await call_next(request)
    if request.path == "/after":
        raise RuntimeError(f"middleware failed after the handler: {SECRET}")
    return reply


@app.get("/unserializable")
async def unserializable(_: Request):
    """A value with no JSON representation, produced after the handler returns."""
    return {"opaque": object()}


async def broken_dependency(request):
    raise RuntimeError(f"dependency failed: {SECRET}")


@app.get("/dependency")
async def dependency(_: Request, value=Depends(broken_dependency)):
    return {"value": value}


@app.get("/slow")
async def slow(_: Request):
    import asyncio

    await asyncio.sleep(1.0)
    return {"late": True}


def middleware_failures_answer_500(client: httpx.Client) -> None:
    for path in ("/before", "/after"):
        response = client.get(path)
        check(response.status_code == 500, f"{path} returned {response.status_code}")
        check(SECRET not in response.text, f"{path} leaked the exception text")


def unsendable_responses_answer_500(client: httpx.Client) -> None:
    """The defect: this used to escape the error handling entirely.

    The client got the connection-level fallback, which says the handler never
    responded — it did, and the response could not be encoded — and the
    traceback never reached the log.
    """
    response = client.get("/unserializable")
    check(
        response.status_code == 500,
        f"an unserializable response returned {response.status_code}",
    )
    check(
        "finished without responding" not in response.text,
        f"an unserializable response blamed the handler: {response.text!r}",
    )


def dependency_failures_answer_500(client: httpx.Client) -> None:
    response = client.get("/dependency")
    check(response.status_code == 500, f"/dependency returned {response.status_code}")
    check(SECRET not in response.text, "a failing dependency leaked its exception text")


def trailing_slashes_are_distinct(client: httpx.Client) -> None:
    """Neither redirects to the other. Strict, and now written down."""
    check(client.get("/x").status_code == 200, "/x should exist")
    check(client.get("/x/").status_code == 404, "/x/ should not exist")
    check(client.get("/y/").status_code == 200, "/y/ should exist")
    check(client.get("/y").status_code == 404, "/y should not exist")


def a_vanishing_client_leaves_the_server_healthy(port: int, client: httpx.Client) -> None:
    """Disconnecting mid-request must not take anything else down."""
    for _ in range(5):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n")
        time.sleep(0.05)
        sock.close()

    time.sleep(1.2)
    response = client.get("/ok")
    check(
        response.status_code == 200,
        f"after five abandoned requests the server returned {response.status_code}",
    )


def refused_statuses_and_headers_are_named() -> None:
    """I-039 and I-040: a status outside 100-599, or a header HTTP cannot
    carry, is a 500 whose log names the status or header, never its value.

    Used to be a bare 500 with nothing logged (the header was refused on a
    tokio thread, which cannot log), and `HTTP/1.1 999 <none>` on the wire.
    An app with no middleware, so `HTTPError` takes the runtime's own path.
    """
    import logging

    from oxbrook import HTTPError, Reply, Response

    plain = App(openapi_url=None, docs_url=None, mcp_url=None)

    @plain.get("/status")
    async def status(_: Request):
        return Response(b"", status=999)

    @plain.get("/name")
    async def name(_: Request):
        return Response(b"{}", headers={"bad name": "x"})

    @plain.get("/value")
    async def value(_: Request):
        return Reply({}, headers={"x-token": f"{SECRET}\r\nset-cookie: stolen=1"})

    @plain.get("/content-type")
    async def content_type(_: Request):
        return Response(b"x", content_type="text/plain\nx-injected: 1")

    @plain.get("/http-error")
    async def http_error(_: Request):
        raise HTTPError(401, headers={"www-authenticate": "Bearer\nx-injected: 1"})

    @plain.get("/fine")
    async def fine(_: Request):
        return Response(b"{}", status=299, headers={"x-ok": "yes"})

    class Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    capture = Capture()
    logger = logging.getLogger("oxbrook")
    logger.addHandler(capture)
    try:
        with TestClient(plain, workers=1) as client:
            for path, named in (("/status", "999"), ("/name", "bad name"),
                                ("/value", "x-token"), ("/content-type", "content type"),
                                ("/http-error", "www-authenticate")):
                before = len(capture.records)
                r = client.get(path)
                check(r.status_code == 500, f"{path} answered {r.status_code}, not 500")
                check("stolen" not in r.headers.get("set-cookie", "")
                      and "x-injected" not in r.headers,
                      f"{path}: a header value split into a second header")
                logged = [rec for rec in capture.records[before:]
                          if rec.getMessage() == "response could not be sent"]
                text = "".join(str(rec.exc_info[1]) for rec in logged if rec.exc_info)
                check(named in text, f"{path}: the log did not name {named!r}: {text!r}")
                check(SECRET not in text, f"{path}: the log carried the header's value")
            r = client.get("/fine")
            check(r.status_code == 299 and r.headers.get("x-ok") == "yes",
                  f"a valid unusual status and header were refused: {r.status_code}")
    finally:
        logger.removeHandler(capture)


def main() -> None:
    try:
        refused_statuses_and_headers_are_named()
        print("  refused_statuses_and_headers_are_named: ok")
    except Exception as exc:
        failures.append("refused_statuses_and_headers_are_named raised "
                        f"{type(exc).__name__}: {exc}")
        print("  refused_statuses_and_headers_are_named: ERROR")
    for step in (conflicting_routes_are_refused, distinguishable_routes_are_accepted):
        try:
            step()
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR")

    port = free_port()
    with TestClient(app, port=port, workers=1, timeout=20) as client:
        http = client.http
        for step, args in [
            (middleware_failures_answer_500, (http,)),
            (unsendable_responses_answer_500, (http,)),
            (dependency_failures_answer_500, (http,)),
            (trailing_slashes_are_distinct, (http,)),
            (a_vanishing_client_leaves_the_server_healthy, (port, http)),
        ]:
            try:
                step(*args)
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
