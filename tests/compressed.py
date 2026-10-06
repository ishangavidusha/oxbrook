#!/usr/bin/env python3
"""Response compression, negotiated from Accept-Encoding and applied in Rust.

Each case reads the body as it came off the wire, before any decoding, and
decodes it with an independent library: `gzip` from the standard library and
`brotlicffi`. A server whose header says `br` over a gzip body, or over the
plain bytes, would pass a check that only looked at the header, and httpx's
own decoding would hide it.

Also held to account: what must be left alone — streams, static files, small
bodies, images, a body the handler already encoded, a reply marked
`no-transform`, a `206` or `304` — because compressing any of them either
breaks the client or wastes the time; `Vary: Accept-Encoding` on every reply
that could have gone either way, so a shared cache does not hand gzip to a
client that cannot read it; and the blocking-pool path for large bodies.

The `brotlicffi` package, not `brotli`: `brotli` re-enables the GIL when
imported on a free-threaded build, and httpx imports it whenever it is
installed, which would quietly turn every free-threaded suite into a GIL one.
"""
import gzip
import json
import sys
import tempfile
from pathlib import Path

import brotlicffi
from oxbrook import CORS, SSE, App, Compression, Request, Response
from oxbrook.testing import TestClient

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


#: JSON that compresses the way an API's does: repeated keys, similar values.
ITEMS = [
    {"id": i, "title": f"Note {i} about the plan", "owner": f"user-{i % 7}", "done": i % 2 == 0}
    for i in range(200)
]
BIG = [{"id": i, "text": f"line {i} of a long export " * 4} for i in range(20_000)]

STATIC = Path(tempfile.mkdtemp(prefix="oxbrook-compression-"))
(STATIC / "app.js").write_text("console.log('a long script');\n" * 400)


def make(compression: Compression | None, cors: CORS | None = None) -> App:
    app = App(openapi_url=None, docs_url=None, mcp_url=None, compression=compression, cors=cors)

    @app.get("/items")
    async def items(_: Request):
        return ITEMS

    @app.get("/big")
    async def big(_: Request):
        return BIG

    @app.get("/small")
    async def small(_: Request):
        return {"ok": True}

    @app.get("/html")
    async def html(_: Request):
        return Response("<p>hello</p>\n" * 300, content_type="text/html; charset=utf-8")

    @app.get("/png")
    async def png(_: Request):
        return Response(b"\x89PNG" + bytes(4000), content_type="image/png")

    @app.get("/encoded")
    async def encoded(_: Request):
        body = gzip.compress(json.dumps(ITEMS).encode())
        return Response(body, headers={"content-encoding": "gzip"})

    @app.get("/kept")
    async def kept(_: Request):
        return Response(json.dumps(ITEMS), headers={"cache-control": "private, no-transform"})

    @app.get("/tagged")
    async def tagged(_: Request):
        return Response(json.dumps(ITEMS), headers={"etag": '"v1"', "vary": "Cookie"})

    @app.get("/varies")
    async def varies(_: Request):
        return Response(json.dumps(ITEMS), headers={"vary": "accept-encoding"})

    @app.get("/partial")
    async def partial(_: Request):
        return Response(json.dumps(ITEMS), status=206)

    @app.get("/missing")
    async def missing(_: Request):
        return Response(json.dumps({"error": "x" * 2000}), status=404)

    @app.get("/events")
    async def events(_: Request):
        async def source():
            for i in range(3):
                yield {"n": i, "pad": "x" * 2000}

        return SSE(source(), ping=None)

    app.static("/static", STATIC)
    return app


def raw(client: TestClient, path: str, accept: str | None, method: str = "GET"):
    """Status, headers and the body exactly as sent."""
    headers = {} if accept is None else {"accept-encoding": accept}
    # httpx adds its own Accept-Encoding unless told otherwise; None removes it.
    if accept is None:
        headers["accept-encoding"] = ""
    with client.stream(method, path, headers=headers) as response:
        body = b"".join(response.iter_raw())
    return response.status_code, response.headers, body


def decode(encoding: str | None, body: bytes) -> bytes:
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "br":
        return brotlicffi.decompress(body)
    return body


def vary_values(headers) -> list[str]:
    return [v.strip().lower() for value in headers.get_list("vary") for v in value.split(",")]


# ---- cases --------------------------------------------------------------------


def configuration_is_checked() -> None:
    for label, kwargs, kind in [
        ("an unknown coding", {"encodings": ["zstd"]}, ValueError),
        ("no codings", {"encodings": []}, ValueError),
        ("a single string", {"encodings": "br"}, TypeError),
        ("gzip level 0", {"gzip_level": 0}, ValueError),
        ("gzip level 10", {"gzip_level": 10}, ValueError),
        ("brotli quality 12", {"brotli_quality": 12}, ValueError),
        ("a negative min_size", {"min_size": -1}, ValueError),
        ("min_size as a string", {"min_size": "1k"}, TypeError),
    ]:
        try:
            Compression(**kwargs)
            failures.append(f"Compression accepted {label}")
        except kind:
            pass
    check(Compression(encodings=["GZIP", "gzip"]).encodings == ("gzip",),
          "encodings were not normalised and deduplicated")
    try:
        App(compression=True)
        failures.append("App accepted compression=True instead of Compression()")
    except TypeError:
        pass


def negotiation(client: TestClient) -> None:
    expected = json.dumps(ITEMS, separators=(",", ":")).encode()
    for accept, coding in [
        ("gzip, deflate, br, zstd", "br"),  # what a browser sends
        ("gzip", "gzip"),
        ("x-gzip", "gzip"),
        ("GZip", "gzip"),
        ("br;q=0.5, gzip", "gzip"),
        ("br;q=0, gzip;q=0", None),
        ("identity", None),
        ("deflate, zstd", None),
        ("*", "br"),
        ("*;q=0", None),
        ("br;q=0, *", "gzip"),
        ("gzip;q=1.0, br;q=1.000", "br"),
        ("br;q=2, gzip", "gzip"),  # an invalid weight drops that entry only
        ("br;q=0.1234, gzip;q=0.1", "gzip"),
        ("br ; q=0.9 , gzip;q=0.8", "br"),  # whitespace RFC 9110 allows
        (None, None),
    ]:
        status, headers, body = raw(client, "/items", accept)
        got = headers.get("content-encoding")
        check(status == 200, f"Accept-Encoding {accept!r}: status {status}")
        check(got == coding, f"Accept-Encoding {accept!r} chose {got!r}, not {coding!r}")
        try:
            plain = decode(got, body)
        except Exception as exc:
            failures.append(f"Accept-Encoding {accept!r}: {got} body did not decode: {exc}")
            continue
        check(json.loads(plain) == ITEMS, f"Accept-Encoding {accept!r}: body changed")
        check(int(headers["content-length"]) == len(body),
              f"Accept-Encoding {accept!r}: content-length {headers['content-length']} "
              f"for {len(body)} bytes")
        if got is not None:
            check(len(body) < len(expected) / 4,
                  f"{got} made {len(expected)} bytes into {len(body)}")
        check("accept-encoding" in vary_values(headers),
              f"Accept-Encoding {accept!r}: no Vary: Accept-Encoding")


def a_real_client_reads_it(client: TestClient) -> None:
    # httpx's own Accept-Encoding and decoding: what a caller of the API sees.
    response = client.get("/items")
    check(response.headers.get("content-encoding") == "br",
          f"httpx was answered with {response.headers.get('content-encoding')!r}")
    check(response.json() == ITEMS, "httpx could not read a compressed reply")
    html = client.get("/html")
    check(html.headers.get("content-encoding") == "br" and html.text.startswith("<p>hello"),
          "an HTML reply was not compressed or did not decode")


def large_bodies_round_trip(client: TestClient) -> None:
    # Over the inline limit, so this is the blocking-pool path.
    for coding in ("br", "gzip"):
        _status, headers, body = raw(client, "/big", coding)
        check(headers.get("content-encoding") == coding,
              f"a large body was sent as {headers.get('content-encoding')!r}")
        check(json.loads(decode(coding, body)) == BIG, f"a large {coding} body changed")


def what_is_left_alone(client: TestClient) -> None:
    for path, why in [
        ("/small", "a body under min_size"),
        ("/png", "an image"),
        ("/kept", "a reply marked no-transform"),
        ("/partial", "a 206"),
    ]:
        status, headers, body = raw(client, path, "br, gzip")
        check(headers.get("content-encoding") is None,
              f"{why} was compressed as {headers.get('content-encoding')}")
        check("accept-encoding" not in vary_values(headers),
              f"{why} says it varies by Accept-Encoding, and it never does")

    status, headers, body = raw(client, "/encoded", "br")
    check(headers.get("content-encoding") == "gzip",
          f"the handler's own gzip became {headers.get('content-encoding')!r}")
    check(json.loads(gzip.decompress(body)) == ITEMS,
          "a body the handler encoded was encoded again")

    status, headers, body = raw(client, "/static/app.js", "br, gzip")
    check(status == 200 and headers.get("content-encoding") is None,
          f"a static file was compressed: {status} {headers.get('content-encoding')}")
    check(body == (STATIC / "app.js").read_bytes(), "a static file's bytes changed")

    with client.stream("GET", "/events", headers={"accept-encoding": "br, gzip"}) as response:
        first = next(response.iter_raw())
    check(response.headers.get("content-encoding") is None,
          "an event stream was compressed, so events would wait for a full block")
    check(b"data:" in first, f"the first event was not readable: {first[:60]!r}")


def errors_compress_too(client: TestClient) -> None:
    status, headers, body = raw(client, "/missing", "gzip")
    check(status == 404 and headers.get("content-encoding") == "gzip",
          f"a large 404 from a handler: {status} {headers.get('content-encoding')}")
    check(json.loads(gzip.decompress(body))["error"].startswith("x"), "the 404 body changed")


def headers_stay_right(client: TestClient) -> None:
    _status, headers, body = raw(client, "/tagged", "br")
    check(headers.get("etag") == 'W/"v1"',
          f"a strong ETag on a compressed body is {headers.get('etag')!r}, not weakened")
    check(vary_values(headers) == ["cookie", "accept-encoding"],
          f"the handler's Vary was not kept alongside ours: {vary_values(headers)}")
    _status, headers, body = raw(client, "/tagged", None)
    check(headers.get("etag") == '"v1"', "an uncompressed reply's ETag was weakened")

    _status, headers, body = raw(client, "/varies", "gzip")
    check(vary_values(headers).count("accept-encoding") == 1,
          f"Vary repeats Accept-Encoding: {headers.get_list('vary')}")

    # A HEAD says what a GET would: the coding, and no length it cannot know.
    _status, headers, body = raw(client, "/items", "br", method="HEAD")
    check(headers.get("content-encoding") == "br" and body == b"",
          f"HEAD with br: {headers.get('content-encoding')!r}, {len(body)} body bytes")
    check(headers.get("content-length") is None,
          f"HEAD claimed the uncompressed length {headers.get('content-length')}")
    _status, headers, body = raw(client, "/items", None, method="HEAD")
    check(headers.get("content-encoding") is None
          and headers.get("content-length") == str(len(json.dumps(ITEMS, separators=(",", ":")))),
          f"HEAD without compression lost its length: {headers.get('content-length')}")


def with_cors(client: TestClient) -> None:
    _status, headers, _body = raw(client, "/items", "gzip")
    check(set(vary_values(headers)) >= {"origin", "accept-encoding"},
          f"CORS and compression did not both add to Vary: {headers.get_list('vary')}")


def server_order_breaks_ties(client: TestClient) -> None:
    _status, headers, _body = raw(client, "/items", "br, gzip")
    check(headers.get("content-encoding") == "gzip",
          f"encodings=('gzip', 'br') chose {headers.get('content-encoding')!r} on a tie")
    _status, headers, _body = raw(client, "/small", "gzip")
    check(headers.get("content-encoding") == "gzip",
          "min_size=0 left a small body uncompressed")


def off_by_default(client: TestClient) -> None:
    _status, headers, _body = raw(client, "/items", "br, gzip")
    check(headers.get("content-encoding") is None, "an app without compression compressed")
    check("accept-encoding" not in vary_values(headers),
          "an app without compression said it varies by Accept-Encoding")


def main() -> None:
    try:
        configuration_is_checked()
        print("  configuration_is_checked: ok")
    except Exception as exc:
        failures.append(f"configuration_is_checked raised {type(exc).__name__}: {exc}")

    for app, steps in [
        (make(Compression()), (negotiation, a_real_client_reads_it, large_bodies_round_trip,
                               what_is_left_alone, errors_compress_too, headers_stay_right)),
        (make(Compression(), CORS(allow_origins=["https://app.example.com"])), (with_cors,)),
        (make(Compression(encodings=["gzip", "br"], min_size=0)), (server_order_breaks_ties,)),
        (make(None), (off_by_default,)),
    ]:
        with TestClient(app, workers=1) as client:
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
