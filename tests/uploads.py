#!/usr/bin/env python3
"""Form bodies and streaming request bodies.

**Forms.** `request.form()` parses URL-encoded and multipart bodies in Rust, on
demand; `Form()` binds them to a pydantic model with the same `422` a JSON body
gets. Wrong content types, malformed multipart, a missing boundary and too many
parts each have their own status, and none of them is a `500`.

**Streaming.** A `BodyStream` argument makes a route take its body
incrementally. Each promise is asserted against a running server:

* a large upload arrives intact and is not held in memory, even when the handler
  reads slower than the client sends — the server stops reading, and TCP pushes
  back on the client;
* nothing is read until the handler asks, so a handler that answers `401` first
  never receives the body — no `100 Continue` is sent;
* the size limit holds when no length is declared, and a declared length over
  it is refused without reading;
* `request_timeout` follows progress, so an upload that keeps moving outlives
  it, while a stalled client gets `408` and a stalled handler `504`.

The last one was a defect found while building this: the body pump's idle
timeout and the reply timeout were the same length and raced, and on the GIL
build a client that stopped sending got `504`, blaming the server for the
client's stall. The reply timeout now stands aside while the pump is waiting on
the client.
"""
import asyncio
import ctypes
import hashlib
import socket
import sys
import time

from openapi_spec_validator import validate
from oxbrook import App, BodyStream, Form, HTTPError, Request, UploadFile
from oxbrook.testing import TestClient
from pydantic import BaseModel

MB = 1024 * 1024
failures: list[str] = []
seen: dict[str, bool] = {}


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


class Signup(BaseModel):
    email: str
    age: int
    interests: list[str] = []
    avatar: UploadFile | None = None


app = App(docs_url=None, mcp_url=None)


@app.post("/raw")
async def raw(request: Request):
    form = request.form()
    return {
        "keys": list(form),
        "name": form.get("name"),
        "tags": form.getlist("tag"),
        "files": [(f.name, f.filename, f.content_type, f.size, f.text()) for f in form.files],
    }


@app.post("/signup")
async def signup(_: Request, data: Signup = Form()):
    return {
        "email": data.email,
        "age": data.age,
        "interests": data.interests,
        "avatar": None if data.avatar is None else [data.avatar.filename, data.avatar.size],
    }


@app.post("/limited")
async def limited(request: Request):
    return len(request.form(max_parts=2))


@app.put("/upload")
async def upload(_: Request, body: BodyStream):
    digest, size = hashlib.sha256(), 0
    async for chunk in body:
        digest.update(chunk)
        size += len(chunk)
    return {"bytes": size, "sha": digest.hexdigest()}


@app.put("/slow")
async def slow(_: Request, body: BodyStream):
    size = 0
    async for chunk in body:
        size += len(chunk)
        await asyncio.sleep(0.001)
    return {"bytes": size}


@app.put("/guarded")
async def guarded(request: Request, body: BodyStream):
    if request.header("authorization") != "yes":
        raise HTTPError(401)
    seen["guarded read"] = True
    return {"bytes": len(await body.read())}


@app.put("/never-reads")
async def never_reads(_: Request, body: BodyStream):
    await asyncio.sleep(10)
    return {}


@app.put("/reads-then-hangs")
async def reads_then_hangs(_: Request, body: BodyStream):
    await body.read()
    await asyncio.sleep(10)
    return {}


@app.post("/buffered")
async def buffered(request: Request):
    return [len(chunk) async for chunk in request.stream()]


def chunks(total: int, size: int = 64 * 1024, pause: float = 0.0):
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        sent += n
        if pause:
            time.sleep(pause)
        yield b"a" * n


def raw_request(port: int, head: bytes, body: bytes = b"", wait: float = 5.0) -> tuple[str, float]:
    sock = socket.create_connection(("127.0.0.1", port))
    sock.settimeout(wait)
    started = time.monotonic()
    sock.sendall(head + body)
    try:
        line = sock.recv(512).split(b"\r\n")[0].decode()
    except TimeoutError:
        line = "(no response)"
    finally:
        sock.close()
    return line, time.monotonic() - started


# ---------------------------------------------------------------------------
# forms
# ---------------------------------------------------------------------------
def forms_parse(client: TestClient) -> None:
    body = client.post("/raw", data={"name": "ada lovelace", "tag": ["a", "b"]}).json()
    check(body["name"] == "ada lovelace" and body["tags"] == ["a", "b"],
          f"URL-encoded form parsed as {body}")

    body = client.post("/raw", data={"name": "ada"},
                       files={"doc": ("notes.txt", b"hello", "text/plain")}).json()
    check(body["files"] == [["doc", "notes.txt", "text/plain", 5, "hello"]],
          f"multipart file parsed as {body['files']}")
    check(body["keys"] == ["name", "doc"], f"multipart field order was {body['keys']}")

    response = client.post(
        "/raw",
        content=b"--b\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\n"
        b"\xff\xfe\r\n--b--\r\n",
        headers={"content-type": "multipart/form-data; boundary=b"},
    )
    check(response.status_code == 200 and "�" in response.json()["name"],
          f"undecodable form text gave {response.status_code} {response.text[:80]}")


def forms_bind(client: TestClient) -> None:
    body = client.post("/signup", data={"email": "a@b.c", "age": "36",
                                        "interests": ["x", "y"]}).json()
    check(body == {"email": "a@b.c", "age": 36, "interests": ["x", "y"], "avatar": None},
          f"form bound as {body}")
    body = client.post("/signup", data={"email": "a@b.c", "age": "36", "interests": "solo"},
                       files={"avatar": ("me.png", b"\x89PNG", "image/png")}).json()
    check(body["interests"] == ["solo"] and body["avatar"] == ["me.png", 4],
          f"multipart form bound as {body}")

    response = client.post("/signup", data={"email": "a@b.c", "age": "old"})
    check(response.status_code == 422, f"an invalid form field returned {response.status_code}")
    check(response.json()["detail"][0]["loc"] == ["age"], f"422 detail was {response.text}")


def forms_refuse_what_they_cannot_read(client: TestClient) -> None:
    for label, kwargs, status in [
        ("a JSON body", {"json": {"a": 1}}, 415),
        ("no content type", {"content": b"a=1"}, 415),
        ("broken multipart", {"content": b"--x\r\nbroken",
                              "headers": {"content-type": "multipart/form-data; boundary=x"}}, 400),
        ("multipart with no boundary", {"content": b"x",
                                        "headers": {"content-type": "multipart/form-data"}}, 400),
    ]:
        response = client.post("/raw", **kwargs)
        check(response.status_code == status,
              f"{label} returned {response.status_code}, expected {status}: {response.text}")
    response = client.post("/limited", data={"a": "1", "b": "2", "c": "3"})
    check(response.status_code == 413, f"too many form parts returned {response.status_code}")


def registration_checks() -> None:
    def build(fn):
        target = App(openapi_url=None, docs_url=None, mcp_url=None)
        try:
            fn(target)
        except TypeError:
            return True
        return False

    def form_not_a_model(target):
        @target.post("/x")
        async def x(_: Request, data: dict = Form()):
            return 1

    def two_bodies(target):
        @target.post("/x")
        async def x(_: Request, body: BodyStream, data: Signup = Form()):
            return 1

    def form_tool(target):
        @target.post("/x", tool=True)
        async def x(_: Request, data: Signup = Form()):
            return 1

    def stream_tool(target):
        @target.put("/x", tool=True)
        async def x(_: Request, body: BodyStream):
            return 1

    def socket_stream(target):
        @target.websocket("/x")
        async def x(_request, ws, body: BodyStream):
            return None

    for label, fn in [
        ("Form() on a non-model", form_not_a_model),
        ("a stream and a form on one handler", two_bodies),
        ("a form route as a tool", form_tool),
        ("a streaming route as a tool", stream_tool),
        ("a BodyStream on a websocket", socket_stream),
    ]:
        check(build(fn), f"{label} was accepted")


def openapi_describes_bodies(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    check(list(document["paths"]["/signup"]["post"]["requestBody"]["content"])
          == ["multipart/form-data"], "a form with a file is not documented as multipart")
    check(list(document["paths"]["/upload"]["put"]["requestBody"]["content"])
          == ["application/octet-stream"], "a streaming body is not documented as binary")
    validate(document)


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------
def streams_arrive_intact(client: TestClient) -> None:
    payload = 24 * MB
    body = client.put("/upload", content=chunks(payload)).json()
    check(body["bytes"] == payload, f"streamed {body['bytes']} of {payload} bytes")
    check(body["sha"] == hashlib.sha256(b"a" * payload).hexdigest(), "streamed body was corrupted")

    check(client.post("/buffered", content=b"xyz").json() == [3],
          "request.stream() on an ordinary route did not yield its body once")
    check(client.post("/buffered").json() == [], "an empty body yielded a chunk")


class _MemoryCounters(ctypes.Structure):
    """`PROCESS_MEMORY_COUNTERS`, for the Windows half of `peak_rss`."""

    _fields_ = [("cb", ctypes.c_uint32), ("page_faults", ctypes.c_uint32)] + [
        (name, ctypes.c_size_t) for name in (
            "peak_working_set", "working_set", "peak_paged_pool", "paged_pool",
            "peak_nonpaged_pool", "nonpaged_pool", "pagefile", "peak_pagefile",
        )
    ]


def peak_rss() -> int:
    """The most memory this process has held resident, in bytes.

    A high-water mark rather than the current figure on both platforms, so the
    difference across a call is what the call cost at its worst.
    """
    if sys.platform == "win32":
        counters = _MemoryCounters()
        counters.cb = ctypes.sizeof(_MemoryCounters)
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_MemoryCounters), ctypes.c_uint32,
        ]
        # `GetCurrentProcess()` is the pseudo-handle -1, written out rather
        # than called so that it is passed as a whole pointer.
        if not psapi.GetProcessMemoryInfo(ctypes.c_void_p(-1), ctypes.byref(counters),
                                          counters.cb):
            raise ctypes.WinError()
        return counters.peak_working_set
    import resource

    unit = 1 if sys.platform == "darwin" else 1024  # ru_maxrss is bytes on macOS, KiB elsewhere
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit


def slow_reader_is_backpressured(client: TestClient) -> None:
    payload = 48 * MB
    before = peak_rss()
    body = client.put("/slow", content=chunks(payload)).json()
    grew = peak_rss() - before
    check(body["bytes"] == payload, f"slow reader received {body['bytes']} of {payload}")
    check(grew < 32 * MB,
          f"a {payload // MB} MB upload to a slow reader grew the process by {grew // MB} MB; "
          f"the body is being buffered instead of back-pressured")


def nothing_is_read_before_asked(client: TestClient) -> None:
    seen.clear()
    line, _ = raw_request(
        client.port,
        b"PUT /guarded HTTP/1.1\r\nHost: x\r\nContent-Length: 10000000\r\n"
        b"Expect: 100-continue\r\n\r\n",
    )
    check(" 401 " in line + " ", f"refusing before reading answered {line!r}")
    check("100" not in line, "the server sent 100 Continue for a body the handler never read")
    check(not seen.get("guarded read"), "the handler read a body it refused")


def limits_hold_while_streaming() -> None:
    with TestClient(app, workers=1, max_body=1 * MB, timeout=30) as client:
        response = client.put("/upload", content=chunks(4 * MB))
        check(response.status_code == 413,
              f"a chunked body over max_body returned {response.status_code}")
        line, _ = raw_request(client.port,
                              b"PUT /upload HTTP/1.1\r\nHost: x\r\n"
                              b"Content-Length: 99999999\r\n\r\n")
        check(" 413 " in line + " ", f"a declared length over max_body answered {line!r}")

        sock = socket.create_connection(("127.0.0.1", client.port))
        sock.sendall(b"PUT /upload HTTP/1.1\r\nHost: x\r\nContent-Length: 500000\r\n\r\n"
                     + b"a" * 100)
        sock.close()
        time.sleep(0.3)
        check(client.put("/upload", content=b"ok").status_code == 200,
              "the server was unhealthy after a client abandoned an upload")


def timeouts_follow_progress() -> None:
    with TestClient(app, workers=1, request_timeout=1.0, timeout=30) as client:
        response = client.put("/upload", content=chunks(2 * MB, size=32 * 1024, pause=0.03))
        check(response.status_code == 200,
              f"an upload taking ~2s with a 1s timeout, making progress, returned "
              f"{response.status_code}")

        line, took = raw_request(client.port,
                                 b"PUT /upload HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n",
                                 b"a" * 10)
        check(" 408 " in line + " ", f"a client that stopped sending got {line!r}, expected 408")
        check(took < 3.0, f"a stalled client waited {took:.1f}s for its answer")

        line, took = raw_request(client.port,
                                 b"PUT /never-reads HTTP/1.1\r\nHost: x\r\n"
                                 b"Content-Length: 5\r\n\r\n",
                                 b"hello")
        check(" 504 " in line + " ", f"a handler that never reads or answers got {line!r}")
        check(took < 3.0, f"a stalled handler held the request {took:.1f}s past a 1s timeout")

        line, took = raw_request(client.port,
                                 b"PUT /reads-then-hangs HTTP/1.1\r\nHost: x\r\n"
                                 b"Content-Length: 5\r\n\r\n", b"hello")
        check(" 504 " in line + " ", f"a handler that read and then hung got {line!r}")


def main() -> None:
    try:
        registration_checks()
        print("  registration_checks: ok")
    except Exception as exc:
        failures.append(f"registration_checks raised {type(exc).__name__}: {exc}")

    with TestClient(app, workers=2, max_body=256 * MB, timeout=60) as client:
        for step in (forms_parse, forms_bind, forms_refuse_what_they_cannot_read,
                     openapi_describes_bodies, streams_arrive_intact,
                     slow_reader_is_backpressured, nothing_is_read_before_asked):
            try:
                step(client)
                print(f"  {step.__name__}: ok")
            except Exception as exc:
                failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
                print(f"  {step.__name__}: ERROR")

    for step in (limits_hold_while_streaming, timeouts_follow_progress):
        try:
            step()
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
