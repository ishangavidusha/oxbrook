#!/usr/bin/env python3
"""Static files, served from Rust.

The part that matters most is what is refused. A static directory is served to
anyone, so a path that escapes it, or a secret sitting inside it, is a leak.
Every refusal here is a `404`, whatever the reason, so a refusal says nothing
about what exists:

* `..` in any spelling — plain, percent-encoded, with an encoded slash, with a
  backslash — and NUL;
* a symlink inside the directory pointing outside it;
* dotfiles, unless the mount allows them, because `.env` and `.git` are the
  files most likely to be in a static directory by accident.

Raw sockets for those: an HTTP client normalises `..` before sending it, which
would test the client rather than the server.

Also asserted: content types, redirects to a directory's slash, the single-page
app fallback — which answers a browser loading a page but not a mistyped
`fetch`, found while building this: with the app at `/`, an API typo got
`index.html` and a 200 — ranges, conditional requests, HEAD, and that a file is
served even when every Python worker is busy.
"""
import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from oxbrook import CORS, App, Request
from oxbrook.testing import TestClient

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def raw(port: int, target: str, extra: str = "") -> tuple[int, dict[str, str], bytes]:
    sock = socket.create_connection(("127.0.0.1", port))
    sock.settimeout(5)
    sock.sendall(f"GET {target} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{extra}\r\n".encode())
    data = b""
    while chunk := sock.recv(65536):
        data += chunk
    sock.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {k.lower(): v for k, _, v in (line.partition(": ") for line in lines[1:])}
    return int(lines[0].split(" ")[1]), headers, body


#: Cleared by `make_site` when this host will not create a symlink.
SYMLINKS = True


def make_site() -> tuple[Path, Path]:
    base = Path(tempfile.mkdtemp(prefix="oxbrook-files-"))
    (base / "outside-secret.txt").write_text("SECRET OUTSIDE")
    site = base / "site"
    (site / "docs").mkdir(parents=True)
    (site / "nested" / "deep").mkdir(parents=True)
    (site / ".git").mkdir()
    (site / "index.html").write_text("<h1>home</h1>")
    (site / "app.js").write_text("console.log('hi')")
    (site / "style.css").write_text("body{}")
    (site / "data.json").write_text("{}")
    (site / "logo.png").write_bytes(b"\x89PNG\r\n")
    (site / "docs" / "index.html").write_text("<h1>docs</h1>")
    (site / "nested" / "deep" / "file.txt").write_text("deep")
    (site / ".env").write_text("SECRET=1")
    (site / ".git" / "config").write_text("[core]")
    (site / ".well-known").mkdir()
    (site / ".well-known" / "security.txt").write_text("contact")
    (site / "café menu.txt").write_text("unicode")
    (site / "big.bin").write_bytes(bytes(range(256)) * 4096)
    (site / "empty.txt").write_text("")
    global SYMLINKS
    try:
        os.symlink(base / "outside-secret.txt", site / "escape.txt")
        os.symlink(site / "app.js", site / "alias.js")
    except OSError:
        # Windows only grants symlink creation to a privileged process or one
        # in developer mode. The cases that need a link are then skipped, and
        # say so, rather than passing because the link was never there.
        SYMLINKS = False
        print("  (no symlinks on this host: the symlink cases are skipped)", flush=True)
    return base, site


def refusals(port: int) -> None:
    for target in [
        "/.env", "/.git/config",
        *(["/escape.txt"] if SYMLINKS else []),
        "/../outside-secret.txt", "/%2e%2e/outside-secret.txt", "/%2E%2E/outside-secret.txt",
        "/docs/..%2f..%2foutside-secret.txt", "/docs%2f..%2f..%2foutside-secret.txt",
        "/./app.js", "/..\\outside-secret.txt", "/app.js%00", "/%00",
        # Names Windows resolves to something other than a file under the
        # mount, refused on every platform so that a mount behaves the same
        # everywhere: a drive-relative segment, a device, and a trailing dot
        # that Windows would trim back to `app.js`.
        "/C:/outside-secret.txt", "/app.js:stream", "/nul", "/NUL", "/CON.txt", "/com1",
        "/app.js.", "/app.js%20", "/docs./index.html",
    ]:
        status, _, body = raw(port, target)
        check(status == 404, f"{target} returned {status}, expected 404")
        check(b"SECRET" not in body, f"{target} leaked a secret: {body[:40]!r}")


def serving(port: int) -> None:
    for target, content_type, body in [
        ("/", "text/html; charset=utf-8", b"<h1>home</h1>"),
        ("/index.html", "text/html; charset=utf-8", b"<h1>home</h1>"),
        ("/app.js", "text/javascript; charset=utf-8", b"console.log('hi')"),
        ("/style.css", "text/css; charset=utf-8", b"body{}"),
        ("/data.json", "application/json; charset=utf-8", b"{}"),
        ("/logo.png", "image/png", b"\x89PNG\r\n"),
        ("/docs/", "text/html; charset=utf-8", b"<h1>docs</h1>"),
        ("/nested/deep/file.txt", "text/plain; charset=utf-8", b"deep"),
        ("/caf%C3%A9%20menu.txt", "text/plain; charset=utf-8", b"unicode"),
        *([("/alias.js", "text/javascript; charset=utf-8", b"console.log('hi')")]
          if SYMLINKS else []),
        ("/empty.txt", "text/plain; charset=utf-8", b""),
    ]:
        status, headers, got = raw(port, target)
        check(status == 200, f"{target} returned {status}")
        check(headers.get("content-type") == content_type,
              f"{target} content type was {headers.get('content-type')!r}")
        check(got == body, f"{target} body was {got[:40]!r}")
        check(headers.get("content-length") == str(len(body)),
              f"{target} content-length was {headers.get('content-length')!r}")

    status, headers, _ = raw(port, "/docs?page=2")
    check(status == 308 and headers.get("location") == "/docs/?page=2",
          f"a directory without its slash gave {status} {headers.get('location')!r}")
    status, _, _ = raw(port, "/nested")
    check(status == 404, f"a directory with no index redirected or served: {status}")


def fallback(port: int) -> None:
    browser = "Accept: text/html,application/xhtml+xml,*/*;q=0.8\r\n"
    status, _, body = raw(port, "/users/42/settings", browser)
    check(status == 200 and body == b"<h1>home</h1>",
          f"a browser loading a client-side route got {status} {body[:30]!r}")
    for label, accept in [("*/*", "Accept: */*\r\n"), ("JSON", "Accept: application/json\r\n"),
                          ("no Accept", "")]:
        status, _, _ = raw(port, "/api/usrs", accept)
        check(status == 404, f"a mistyped API call with {label} got {status}, not 404")
    status, _, _ = raw(port, "/missing.js", browser)
    check(status == 404, f"a missing asset got the fallback: {status}")
    status, _, body = raw(port, "/api/users", "Accept: application/json\r\n")
    check(status == 200 and body == b"[]", f"a real route beside the mount gave {status}")


def protocol(port: int) -> None:
    length = 256 * 4096
    status, headers, body = raw(port, "/big.bin")
    check(status == 200 and len(body) == length, f"a 1 MB file gave {status}, {len(body)} bytes")
    check(body == bytes(range(256)) * 4096, "a streamed file arrived corrupted")
    check(headers.get("accept-ranges") == "bytes", "Accept-Ranges missing")
    check(headers.get("cache-control") == "no-cache", "cache_control not applied")
    etag, modified = headers.get("etag", ""), headers.get("last-modified", "")
    check(etag.startswith('W/"'), f"ETag was {etag!r}")

    for label, extra, want_status, want_range, want_body in [
        ("a closed range", "Range: bytes=10-19\r\n", 206, f"bytes 10-19/{length}",
         bytes(range(10, 20))),
        ("an open range", f"Range: bytes={length - 3}-\r\n", 206,
         f"bytes {length - 3}-{length - 1}/{length}", bytes([253, 254, 255])),
        ("a suffix range", "Range: bytes=-2\r\n", 206, f"bytes {length - 2}-{length - 1}/{length}",
         bytes([254, 255])),
        ("a range past the end", f"Range: bytes={length}-\r\n", 416, f"bytes */{length}", b""),
        ("several ranges", "Range: bytes=0-1,5-6\r\n", 200, None, None),
        ("a stale If-Range", 'Range: bytes=0-1\r\nIf-Range: "other"\r\n', 200, None, None),
        ("a matching If-Range", f"Range: bytes=0-1\r\nIf-Range: {etag}\r\n", 206,
         f"bytes 0-1/{length}", bytes([0, 1])),
    ]:
        status, headers, body = raw(port, "/big.bin", extra)
        check(status == want_status, f"{label} gave {status}, expected {want_status}")
        if want_range is not None:
            check(headers.get("content-range") == want_range,
                  f"{label} Content-Range was {headers.get('content-range')!r}")
        if want_body is not None:
            check(body == want_body, f"{label} body was {body[:10]!r}")

    for label, extra in [("If-None-Match", f"If-None-Match: {etag}\r\n"),
                         ("If-None-Match without W/", f"If-None-Match: {etag[2:]}\r\n"),
                         ("If-Modified-Since", f"If-Modified-Since: {modified}\r\n")]:
        status, headers, body = raw(port, "/big.bin", extra)
        check(status == 304 and body == b"", f"{label} gave {status} with {len(body)} bytes")
    status, _, _ = raw(port, "/big.bin", 'If-None-Match: "different"\r\n')
    check(status == 200, f"a non-matching If-None-Match gave {status}")


def methods(client: TestClient) -> None:
    response = client.head("/big.bin")
    check(response.status_code == 200 and response.headers.get("content-length") == str(256 * 4096)
          and response.content == b"", "HEAD did not report the length without a body")
    response = client.post("/app.js")
    check(response.status_code == 405, f"POST to a static file gave {response.status_code}")


def cors_and_dotfiles(site: Path) -> None:
    app = App(openapi_url=None, docs_url=None, mcp_url=None,
              cors=CORS(allow_origins=["https://app.example.com"]))
    app.static("/files", site, dotfiles=True, index=None)
    with TestClient(app, workers=1) as client:
        response = client.get("/files/app.js", headers={"origin": "https://app.example.com"})
        check(response.headers.get("access-control-allow-origin") == "https://app.example.com",
              "a static file lacked CORS headers")
        check(client.get("/files/.well-known/security.txt").status_code == 200,
              "dotfiles=True still refused a dotfile")
        check(client.get("/files/docs/").status_code == 404, "index=None still served an index")
        if SYMLINKS:
            status, _, body = raw(client.port, "/files/escape.txt")
            check(status == 404, f"dotfiles=True let a symlink escape: {status} {body[:20]!r}")
        # With dotfiles allowed, the dotfile rule no longer happens to catch
        # `..`, so this is where the traversal check stands on its own.
        for target in ("/files/../outside-secret.txt", "/files/%2e%2e/outside-secret.txt",
                       "/files/docs/..%2f..%2f..%2foutside-secret.txt"):
            status, _, body = raw(client.port, target)
            check(status == 404 and b"SECRET" not in body,
                  f"dotfiles=True let {target} through: {status} {body[:20]!r}")
        check(client.get("/files", follow_redirects=False).status_code == 308,
              "the bare prefix did not redirect to its slash")


def workers_are_not_needed(site: Path) -> None:
    """Every worker is busy; files still arrive."""
    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/hold")
    async def hold(_: Request):
        await asyncio.sleep(2)
        return {}

    app.static("/files", site)
    with TestClient(app, workers=1, max_concurrency=1, timeout=10) as client:
        holder = threading.Thread(target=lambda: client.get("/hold"), daemon=True)
        holder.start()
        time.sleep(0.3)
        check(client.get("/hold").status_code == 503, "the only worker slot was not taken")
        started = time.perf_counter()
        response = client.get("/files/app.js")
        took = time.perf_counter() - started
        check(response.status_code == 200,
              f"a file with every worker busy gave {response.status_code}")
        check(took < 1.0, f"a file waited {took:.2f}s for a worker it does not need")
        holder.join()


def nested_mounts_keep_their_policies(site: Path) -> None:
    """The caching pattern the guide recommends: hashed assets apart from the app."""
    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/api/users")
    async def users(_: Request):
        return []

    app.static("/docs", site / "docs", cache_control="public, max-age=31536000, immutable")
    app.static("/", site, fallback="index.html", cache_control="no-cache")
    with TestClient(app, workers=1) as client:
        response = client.get("/docs/index.html")
        check(response.status_code == 200 and
              response.headers.get("cache-control") == "public, max-age=31536000, immutable",
              f"the inner mount lost its policy: {response.headers.get('cache-control')!r}")
        response = client.get("/deep/link", headers={"accept": "text/html"})
        check(response.status_code == 200 and response.headers.get("cache-control") == "no-cache",
              "the outer mount's fallback lost its policy")
        check(client.get("/api/users").json() == [], "a route beside two mounts stopped working")


def registration(site: Path) -> None:
    def refuses(build, kind=ValueError) -> bool:
        target = App(openapi_url=None, docs_url=None, mcp_url=None)
        try:
            build(target)
        except kind:
            return True
        return False

    def route_then_mount(target):
        @target.get("/assets/{name}")
        async def asset(_: Request, name: str):
            return name

        target.static("/assets", site)

    def mount_then_route(target):
        target.static("/", site)

        @target.get("/")
        async def root(_: Request):
            return 1

    for label, build in [
        ("a missing directory", lambda a: a.static("/x", site / "nope")),
        ("a prefix with a trailing slash", lambda a: a.static("/x/", site)),
        ("a prefix with a parameter", lambda a: a.static("/{x}", site)),
        ("a fallback that does not exist", lambda a: a.static("/", site, fallback="nope.html")),
        ("a fallback outside the directory",
         lambda a: a.static("/", site, fallback="../outside-secret.txt")),
        ("an index that is a path", lambda a: a.static("/", site, index="docs/index.html")),
        ("a route, then a mount on its shape", route_then_mount),
        ("a mount, then a route on its shape", mount_then_route),
        ("two mounts at one prefix", lambda a: (a.static("/x", site), a.static("/x", site))),
    ]:
        check(refuses(build), f"{label} was accepted")

    beside = App(openapi_url=None, docs_url=None, mcp_url=None)

    @beside.get("/assets/manifest.json")
    async def manifest(_: Request):
        return {}

    beside.static("/assets", site)  # a static route beside the mount is fine


def main() -> None:
    base, site = make_site()
    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/api/users")
    async def users(_: Request):
        return []

    app.static("/", site, fallback="index.html", cache_control="no-cache")

    try:
        registration(site)
        print("  registration: ok")
    except Exception as exc:
        failures.append(f"registration raised {type(exc).__name__}: {exc}")

    with TestClient(app, workers=1) as client:
        for step, arg in [(refusals, client.port), (serving, client.port), (fallback, client.port),
                          (protocol, client.port), (methods, client)]:
            try:
                step(arg)
                print(f"  {step.__name__}: ok")
            except Exception as exc:
                failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
                print(f"  {step.__name__}: ERROR")

    for step in (cors_and_dotfiles, workers_are_not_needed, nested_mounts_keep_their_policies):
        try:
            step(site)
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR")

    import shutil

    shutil.rmtree(base, ignore_errors=True)
    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
