#!/usr/bin/env python3
"""Form bodies and streaming request bodies.

**Forms.** `request.form()` parses URL-encoded and multipart bodies in Rust, on
demand; `Form()` binds them to a pydantic model with the same `422` a JSON body
gets. Wrong content types, malformed multipart, a missing boundary and too many
parts each have their own status, and none of them is a `500`. A form route
without files can be an agent tool: its fields are the tool's arguments, and
the handler and the router's middleware get a real URL-encoded body.

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
from oxbrook import App, BodyStream, Form, HTTPError, Request, Router, UploadFile
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


@app.post("/twice")
async def twice(request: Request, body: BodyStream):
    first, second = await asyncio.gather(request.read(), request.read())
    return {"first": len(first), "second": len(second), "same": first == second}


@app.post("/parts")
async def parts(request: Request, body: BodyStream):
    seen_parts, slow = [], request.header("x-slow") is not None
    async for part in body.parts(max_parts=int(request.header("x-max-parts") or 1000)):
        entry = {"name": part.name, "filename": part.filename,
                 "content_type": part.content_type, "headers": part.headers}
        if request.header("x-skip"):
            seen_parts.append(entry)
            continue
        if part.filename is None:
            entry["text"] = await part.text()
        else:
            digest, size, count = hashlib.sha256(), 0, 0
            async for chunk in part:
                digest.update(chunk)
                size += len(chunk)
                count += 1
                if slow:
                    await asyncio.sleep(0.001)
            entry.update(bytes=size, sha=digest.hexdigest(), chunks=count)
        seen_parts.append(entry)
    return seen_parts


@app.post("/parts-stale")
async def parts_stale(_: Request, body: BodyStream):
    stream = body.parts()
    first = await stream.__anext__()
    await stream.__anext__()
    try:
        await first.read()
    except RuntimeError:
        return {"stale": "refused"}
    return {"stale": "read"}


@app.post("/parts-buffered")
async def parts_buffered(request: Request):
    return [[p.name, await p.text()] async for p in request.stream().parts()]


@app.post("/form-of")
async def form_of(request: Request):
    form = request.form()
    out = []
    for name, value in form.multi_items():
        if isinstance(value, UploadFile):
            out.append({"name": name, "filename": value.filename,
                        "content_type": value.content_type,
                        "sha": hashlib.sha256(value.data).hexdigest()})
        else:
            out.append({"name": name, "text": value})
    return out


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
    check(response.json()["errors"][0]["loc"] == ["age"], f"422 errors were {response.text}")


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
        ("a form route with files as a tool", form_tool),
        ("a streaming route as a tool", stream_tool),
        ("a BodyStream on a websocket", socket_stream),
    ]:
        check(build(fn), f"{label} was accepted")


class Subscribe(BaseModel):
    email: str
    age: int
    topics: list[str] = []
    weekly: bool = True
    note: str | None = None


def form_routes_are_tools() -> None:
    seen: list[dict] = []
    router = Router()

    @router.middleware
    async def peek(request: Request, call_next):
        form = request.form()
        seen.append({k: form.getlist(k) for k in form})
        return await call_next(request)

    @router.post("/subscribe", tool=True)
    async def subscribe(_: Request, data: Subscribe = Form()):
        """Subscribe an address to the newsletter."""
        return data.model_dump()

    app = App(docs_url=None)
    app.include(router)

    with TestClient(app) as client:
        tool = next(t for t in client.mcp("tools/list")["tools"] if t["name"] == "subscribe")
        schema = tool["inputSchema"]
        check(set(schema["properties"]) == {"email", "age", "topics", "weekly", "note"}
              and set(schema["required"]) == {"email", "age"},
              f"the form tool's schema was {schema}")

        got = client.call_tool("subscribe", {"email": "a@example.com", "age": 30,
                                             "topics": ["rust", "python"], "weekly": False,
                                             "note": None})
        check(got == {"email": "a@example.com", "age": 30, "topics": ["rust", "python"],
                      "weekly": False, "note": None}, f"the form tool returned {got}")
        check(seen[-1] == {"email": ["a@example.com"], "age": ["30"],
                           "topics": ["rust", "python"], "weekly": ["false"]},
              f"the router's middleware read the form as {seen[-1]}")

        # The same form over HTTP gives the same answer.
        r = client.post("/subscribe", data={"email": "a@example.com", "age": "30",
                                            "topics": ["rust", "python"], "weekly": "false"})
        check(r.status_code == 200 and r.json() == got,
              f"the route over HTTP answered {r.status_code} {r.text}")

        result = client.mcp("tools/call", {"name": "subscribe",
                                           "arguments": {"email": "b@example.com",
                                                         "age": "old"}})
        text = "".join(p.get("text", "") for p in result.get("content", []))
        check(result.get("isError") is True and "422" in text and "age" in text,
              f"a form field that does not validate was not a tool error: {result}")


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
def multipart(fields, files, boundary="b0und4ry", chunk=64 * 1024):
    """A multipart body as a generator: fields as (name, text), files as
    (name, filename, content_type, size), each file `size` bytes of a pattern."""
    def gen():
        for name, text in fields:
            yield (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                   .encode() + text + b"\r\n")
        for name, filename, content_type, size in files:
            yield (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                   f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n"
                   f"X-Extra: yes\r\n\r\n").encode()
            sent = 0
            while sent < size:
                n = min(chunk, size - sent)
                yield pattern(sent, n)
                sent += n
            yield b"\r\n"
        yield f"--{boundary}--\r\n".encode()
    return gen, {"content-type": f"multipart/form-data; boundary={boundary}"}


def pattern(offset: int, n: int) -> bytes:
    # Not all one byte, so a part cut or shifted anywhere changes the digest.
    block = bytes(range(251)) * (n // 251 + 2)
    start = offset % 251
    return block[start:start + n]


def parts_stream(client: TestClient) -> None:
    """I-055: a multipart upload read a part at a time, never held whole."""
    size = 48 * MB
    gen, headers = multipart([("title", b"holiday"), ("note", b"caf\xc3\xa9 \xff")],
                             [("photo", "a.jpg", "image/jpeg", size)])
    before = peak_rss()
    r = client.post("/parts", content=gen(), headers={**headers, "x-slow": "1"}, timeout=120)
    grew = peak_rss() - before
    got = r.json() if r.status_code == 200 else r.text
    expected_sha = hashlib.sha256(pattern(0, size)).hexdigest()
    check(r.status_code == 200 and [p["name"] for p in got] == ["title", "note", "photo"],
          f"parts answered {r.status_code} {str(got)[:300]}")
    if r.status_code != 200:
        return
    check(got[0]["text"] == "holiday" and got[1]["text"] == "café \ufffd",
          f"fields read as {got[0].get('text')!r}, {got[1].get('text')!r}")
    photo = got[2]
    check(photo["bytes"] == size and photo["sha"] == expected_sha,
          f"a {size // MB} MB part arrived as {photo['bytes']} bytes, digest match "
          f"{photo['sha'] == expected_sha}")
    check(photo["chunks"] > 1, "a 48 MB part arrived in one chunk: it was collected first")
    check(photo["filename"] == "a.jpg" and photo["content_type"] == "image/jpeg"
          and photo["headers"].get("x-extra") == "yes",
          f"the part's metadata read as {photo}")
    check(grew < 32 * MB,
          f"a {size // MB} MB multipart upload to a slow reader grew the process by "
          f"{grew // MB} MB; the body is being held instead of streamed")

    # Same parser as request.form(): the same body reads the same both ways,
    # cut into pieces small enough to split every boundary and header.
    gen, headers = multipart([("a", b"1"), ("a", b"2"), ("bad", b"\xff\xfe")],
                             [("f", "x.bin", "application/octet-stream", 5000)], chunk=7)
    whole = b"".join(gen())
    streamed = client.post("/parts", content=(whole[i:i + 7] for i in range(0, len(whole), 7)),
                           headers=headers).json()
    formed = client.post("/form-of", content=whole, headers=headers).json()
    as_form = [{"name": p["name"], "text": p["text"]} if p["filename"] is None else
               {"name": p["name"], "filename": p["filename"],
                "content_type": p["content_type"], "sha": p["sha"]} for p in streamed]
    check(as_form == formed, f"streamed parts {as_form} differ from request.form() {formed}")

    skipped = client.post("/parts", content=whole, headers={**headers, "x-skip": "1"})
    names = [p["name"] for p in skipped.json()] if skipped.status_code == 200 else None
    check(names == ["a", "a", "bad", "f"],
          f"parts left unread were not skipped: {skipped.status_code} {skipped.text[:200]}")
    stale = client.post("/parts-stale", content=whole, headers=headers).json()
    check(stale == {"stale": "refused"}, f"reading a part after moving on gave {stale}")
    # The fields alone, so the body is small enough for an ordinary route.
    fields_only = whole[:whole.index(b'--b0und4ry\r\nContent-Disposition: form-data; name="f"')]
    buffered = client.post("/parts-buffered", content=fields_only + b"--b0und4ry--\r\n",
                           headers=headers)
    expected = [["a", "1"], ["a", "2"], ["bad", "\ufffd\ufffd"]]
    check(buffered.status_code == 200 and buffered.json() == expected,
          f"parts of a buffered body read as {buffered.status_code} {buffered.text[:200]}")


def parts_refuse_what_they_cannot_read(client: TestClient) -> None:
    gen, headers = multipart([("a", b"1"), ("b", b"2"), ("c", b"3")], [])
    whole = b"".join(gen())
    for label, content, sent, status in (
        ("not multipart", b"a=1", {"content-type": "application/x-www-form-urlencoded"}, 415),
        ("no content type", b"x", {}, 415),
        ("no boundary", whole, {"content-type": "multipart/form-data"}, 400),
        ("cut short", whole[:-30], headers, 400),
        ("no parts at all", b"just bytes", headers, 400),
        ("too many parts", whole, {**headers, "x-max-parts": "2"}, 413),
    ):
        r = client.post("/parts", content=content, headers=sent)
        check(r.status_code == status, f"{label}: answered {r.status_code}, expected {status}")


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


def two_reads_at_once_both_finish(client: TestClient) -> None:
    """I-115: two `request.read()` awaited together on one request. The second
    used to replace the first's wakeup, and the first waited forever."""
    total = 4 * 64 * 1024
    r = client.post("/twice", content=chunks(total, pause=0.05), timeout=10)
    check(r.status_code == 200 and r.json() == {"first": total, "second": total, "same": True},
          f"two reads at once answered {r.status_code} {r.text[:200]}")


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
    for step in (registration_checks, form_routes_are_tools):
        try:
            step()
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR")

    with TestClient(app, workers=2, max_body=256 * MB, timeout=60) as client:
        for step in (forms_parse, forms_bind, forms_refuse_what_they_cannot_read,
                     openapi_describes_bodies, streams_arrive_intact,
                     slow_reader_is_backpressured, nothing_is_read_before_asked,
                     two_reads_at_once_both_finish, parts_stream,
                     parts_refuse_what_they_cannot_read):
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
