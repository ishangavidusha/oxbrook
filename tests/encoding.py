#!/usr/bin/env python3
"""One value, one JSON encoding, on every surface that sends one.

Demonstrated against a running server before the fix: a handler returning a
datetime answered 500, the same dict inside a `Reply` answered
`"2026-09-23 10:00:00"`, and inside a pydantic model it was ISO 8601. An enum
went out as `"Color.red"`, bytes as `"b'hi'"`, a set as `"{1}"`, NaN as a bare
`NaN` no JSON parser accepts, and a database row not at all. A value's encoding
depended on which way out it took.

Every surface here must produce the same bytes for the same value, and those
bytes must be what the value's pydantic encoding is.
"""
import asyncio
import datetime
import decimal
import enum
import json
import logging
import sys
import uuid

from oxbrook import SSE, App, Event, HTTPError, Reply, Request
from oxbrook.testing import TestClient
from pydantic import BaseModel

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


class Color(enum.Enum):
    red = "red"


class Stamp(BaseModel):
    at: datetime.datetime


class Row:
    """Mapping-like without being a Mapping, which is what asyncpg's Record is."""

    def __init__(self, **fields):
        self._fields = fields

    def keys(self):
        return self._fields.keys()

    def __getitem__(self, key):
        return self._fields[key]


class Opaque:
    def __repr__(self):
        return "<Opaque secret=hunter2>"


AT = datetime.datetime(2026, 9, 23, 10, 0, 0, 123000, tzinfo=datetime.UTC)

VALUE = {
    "datetime": AT,
    "naive": datetime.datetime(2026, 9, 23, 10, 0),
    "date": datetime.date(2026, 9, 23),
    "time": datetime.time(10, 0),
    "timedelta": datetime.timedelta(seconds=90),
    "uuid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    "decimal": decimal.Decimal("12.50"),
    "enum": Color.red,
    "set": {1},
    "tuple": (1, 2),
    "bytes": b"hi",
    "int_key": {1: "a"},
    "models": [Stamp(at=AT)],
    "row": Row(id=7, created_at=AT),
    "big": 2**70,
    "nan": float("nan"),
}

EXPECTED = {
    "datetime": "2026-09-23T10:00:00.123000Z",
    "naive": "2026-09-23T10:00:00",
    "date": "2026-09-23",
    "time": "10:00:00",
    "timedelta": "PT1M30S",
    "uuid": "12345678-1234-5678-1234-567812345678",
    "decimal": "12.50",
    "enum": "red",
    "set": [1],
    "tuple": [1, 2],
    "bytes": "hi",
    "int_key": {"1": "a"},
    "models": [{"at": "2026-09-23T10:00:00.123000Z"}],
    "row": {"id": 7, "created_at": "2026-09-23T10:00:00.123000Z"},
    "big": 2**70,
    "nan": None,
}


app = App()


@app.get("/plain")
async def plain(_: Request):
    return VALUE


@app.get("/native")
async def native(_: Request):
    return {"a": [1, 2.5, "x", None, True]}


@app.get("/reply")
async def reply(_: Request):
    return Reply(VALUE, status=200, headers={"x-via": "reply"})


@app.get("/error")
async def error(_: Request):
    raise HTTPError(409, "conflict", extensions={"value": VALUE})


@app.get("/events")
async def events(_: Request):
    async def source():
        yield Event(data=VALUE)

    return SSE(source(), ping=None)


@app.websocket("/ws")
async def ws(_: Request, sock):
    await sock.send(VALUE)


@app.get("/tool", tool=True)
async def tool(_: Request):
    """A tool returning the value."""
    return VALUE


@app.get("/opaque")
async def opaque(_: Request):
    return {"thing": Opaque()}


@app.get("/opaque-reply")
async def opaque_reply(_: Request):
    return Reply({"thing": Opaque()}, status=200)


def agrees(surface: str, got) -> None:
    for key, want in EXPECTED.items():
        if got.get(key) != want:
            failures.append(f"{surface}: {key} encoded as {got.get(key)!r}, expected {want!r}")


def every_surface_agrees(c: TestClient) -> None:
    r = c.get("/plain")
    check(r.status_code == 200, f"a plain return answered {r.status_code}")
    if r.status_code == 200:
        agrees("plain return", r.json())

    r = c.get("/reply")
    check(r.status_code == 200, f"a Reply answered {r.status_code}")
    if r.status_code == 200:
        agrees("Reply", r.json())
        check(r.content == c.get("/plain").content, "a Reply and a plain return differ in bytes")

    r = c.get("/error")
    check(r.status_code == 409, f"an HTTPError with extensions answered {r.status_code}")
    if r.status_code == 409:
        agrees("HTTPError extensions", r.json()["value"])

    with c.http.stream("GET", "/events") as response:
        for line in response.iter_lines():
            if line.startswith("data:"):
                agrees("SSE", json.loads(line[5:]))
                break

    async def over_socket():
        async with c.websocket("/ws") as sock:
            return await asyncio.wait_for(sock.recv(), 5)

    agrees("WebSocket", json.loads(asyncio.run(over_socket())))

    result = c.mcp("tools/call", {"name": "tool", "arguments": {}})
    text = "".join(part.get("text", "") for part in result["content"])
    agrees("MCP tool result", json.loads(text))


def json_native_values_keep_the_fast_path(c: TestClient) -> None:
    # Plain JSON is still encoded in Rust, and must come out as it always did.
    r = c.get("/native")
    check(r.content == b'{"a":[1,2.5,"x",null,true]}', f"native values encoded as {r.content!r}")


def nan_is_valid_json(c: TestClient) -> None:
    for path in ("/plain", "/reply"):
        raw = c.get(path).text
        try:
            json.loads(raw, parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
        except ValueError as exc:
            failures.append(f"{path} sent {exc}, which is not JSON")


def unknown_objects_are_refused(c: TestClient) -> None:
    # Refused, not str()'d: a repr in a response is a bug that reaches the
    # client looking like data, and a repr can carry what it should not.
    for path in ("/opaque", "/opaque-reply"):
        r = c.get(path)
        check(r.status_code == 500, f"{path}: an object with no JSON form answered {r.status_code}")
        check("hunter2" not in r.text, f"{path}: an object's repr reached the client")


def every_error_is_problem_details() -> None:
    """One error format, RFC 9457, whichever side answered.

    Before, a missing route was `text/plain` "not found", a 405 and 413 were
    plain text too, a validation failure was `{"detail": [...]}`, an HTTPError
    `{"detail": "..."}`, and a handler crash `text/plain` "internal server
    error". Rust's titles also disagreed with Python's for 413 and 422: the
    `http` crate predates RFC 9110's names.
    """
    import http
    import pathlib
    import tempfile

    import httpx

    class Model(BaseModel):
        n: int

    folder = pathlib.Path(tempfile.mkdtemp())
    errors = App(openapi_url=None, docs_url=None, mcp_url=None)
    errors.static("/files", folder)

    class Taken(Exception):
        pass

    @errors.exception_handler(Taken)
    async def taken(_request, exc):
        raise HTTPError(409, "taken")

    async def refuse(request):
        raise HTTPError(401, "members only")

    @errors.get("/num/{n}")
    async def num(_: Request, n: int):
        return {}

    @errors.post("/body")
    async def body(_: Request, m: Model):
        return {}

    @errors.get("/conflict")
    async def conflict(_: Request):
        raise HTTPError(
            409, "a duplicate", type="https://example.com/problems/dup",
            extensions={"existing": 7},
        )

    @errors.get("/bare")
    async def bare(_: Request):
        raise HTTPError(403)

    @errors.get("/crash")
    async def crash(_: Request):
        raise RuntimeError("password=hunter2")

    @errors.get("/mapped")
    async def mapped(_: Request):
        raise Taken()

    @errors.get("/slow")
    async def slow(_: Request):
        await asyncio.sleep(5)

    @errors.websocket("/ws")
    async def ws(_: Request, sock):
        pass

    @errors.websocket("/members", authorize=refuse)
    async def members(_: Request, sock):
        pass

    upgrade = {
        "connection": "upgrade", "upgrade": "websocket", "sec-websocket-version": "13",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
    }
    with TestClient(errors, max_body=100, request_timeout=0.5) as c:
        cases = [
            ("no such route", c.get("/nowhere"), 404),
            ("wrong method", c.delete("/bare"), 405),
            ("parameter", c.get("/num/x"), 422),
            ("body", c.post("/body", json={"n": "x"}), 422),
            ("too large", c.post("/body", content=b"x" * 500), 413),
            ("HTTPError", c.get("/conflict"), 409),
            ("bare HTTPError", c.get("/bare"), 403),
            ("crash", c.get("/crash"), 500),
            ("exception handler", c.get("/mapped"), 409),
            ("timeout", c.get("/slow"), 504),
            ("not an upgrade", c.get("/ws"), 426),
            ("authorizer", httpx.get(c.base_url + "/members", headers=upgrade), 401),
            ("static file", c.get("/files/missing.txt"), 404),
        ]
        for label, r, status in cases:
            if r.status_code != status:
                failures.append(f"{label}: answered {r.status_code}, expected {status}")
                continue
            kind = r.headers.get("content-type", "")
            if kind != "application/problem+json":
                failures.append(f"{label}: content type was {kind!r}")
                continue
            got = r.json()
            want_title = http.HTTPStatus(status).phrase
            if got.get("status") != status:
                failures.append(f"{label}: status member was {got.get('status')!r}")
            if label == "HTTPError":
                continue
            if got.get("type") != "about:blank" or got.get("title") != want_title:
                failures.append(
                    f"{label}: type/title were {got.get('type')!r}/{got.get('title')!r}, "
                    f"expected about:blank/{want_title!r}"
                )

        got = c.get("/conflict").json()
        check(
            got == {
                "type": "https://example.com/problems/dup", "title": "Conflict", "status": 409,
                "detail": "a duplicate", "existing": 7,
            },
            f"an HTTPError with a type and extensions gave {got}",
        )
        check(list(got)[:3] == ["type", "title", "status"], f"member order was {list(got)}")
        check("hunter2" not in c.get("/crash").text, "a crash's exception text reached the client")
        check(
            c.get("/num/x").json()["errors"][0]["loc"] == ["path", "n"],
            "a parameter failure lost its location",
        )

    for build, fix in (
        (lambda: HTTPError(409, {"id": 7}), "extensions"),
        (lambda: HTTPError(409, "x", extensions={"status": 200}), "standard members"),
    ):
        try:
            build()
            failures.append(f"an HTTPError that should be refused was accepted ({fix})")
        except (TypeError, ValueError) as exc:
            check(fix in str(exc), f"the refusal did not name the fix: {exc}")


def main() -> None:
    logging.getLogger("oxbrook").setLevel(logging.CRITICAL)
    every_error_is_problem_details()
    print("  every_error_is_problem_details: ok")
    with TestClient(app) as c:
        for step in (
            every_surface_agrees,
            json_native_values_keep_the_fast_path,
            nan_is_valid_json,
            unknown_objects_are_refused,
        ):
            try:
                step(c)
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
