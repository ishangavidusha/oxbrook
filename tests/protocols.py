#!/usr/bin/env python3
"""TLS termination and HTTP/2.

What is asserted:

* a certificate that cannot be served stops the server at startup: a missing
  file, an empty one, a key that belongs to another certificate, one file
  without the other;
* HTTPS serves everything plain HTTP does, WebSockets included, and refuses
  TLS older than 1.2;
* HTTP/2, over TLS through ALPN and in cleartext by prior knowledge, carries
  bodies larger than its flow-control window, streamed request bodies, SSE,
  HEAD, CORS and the error statuses, and exposes the `:authority` as `host`;
* `http2=False` serves HTTP/1.1 alone, and ALPN says so;
* a client that opens HTTP/2 streams and resets each one once its handler has
  started cannot run more than 200 handlers from one connection. Found while
  building this (CVE-2023-44487): hyper only limits resets before a request is
  accepted, and a reset does not stop a handler, so one connection ran almost
  4,000 handlers in two seconds and every other client got 503;
* a connection that says nothing is closed: one that never finishes a TLS
  handshake, one that never sends a byte, and an HTTP/2 connection with
  nothing in flight, which gets a GOAWAY first and is dropped if it never
  acknowledges it. hyper has no idle timeout for HTTP/2 or for the bytes it
  reads to choose a protocol, so all three held a connection slot forever.
  One with a long response in flight is left alone.

The idle cases take about half a minute, run side by side.
"""
import asyncio
import contextlib
import datetime
import ipaddress
import socket
import ssl
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from oxbrook import CORS, SSE, App, BodyStream, Request, Response
from oxbrook.testing import TestClient

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


# ---------------------------------------------------------------------------
# certificates
# ---------------------------------------------------------------------------
def issue(directory: Path, name: str) -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1 and localhost, and its key."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"oxbrook {name}")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / f"{name}.crt"
    key_path = directory / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


# ---------------------------------------------------------------------------
# the app
# ---------------------------------------------------------------------------
app = App(cors=CORS(allow_origins=["https://site.example"]), websocket_origins=["*"])
slow_running = 0
slow_peak = 0
slow_lock = threading.Lock()


@app.get("/")
async def root(request: Request):
    return {"host": request.headers.get("host")}


@app.post("/echo")
async def echo(request: Request):
    import hashlib

    return {"length": len(request.body), "sha": hashlib.sha256(request.body).hexdigest()}


@app.post("/upload")
async def upload(request: Request, body: BodyStream):
    import hashlib

    digest = hashlib.sha256()
    size = 0
    async for chunk in body:
        digest.update(chunk)
        size += len(chunk)
    return {"length": size, "sha": digest.hexdigest()}


@app.get("/events")
async def events(request: Request):
    async def ticks():
        for i in range(3):
            yield {"n": i}

    return SSE(ticks())


@app.get("/long")
async def long(request: Request):
    async def ticks():
        for i in range(8):
            await asyncio.sleep(5)
            yield {"n": i}

    return SSE(ticks())


@app.get("/hop")
async def hop(request: Request):
    # Headers HTTP/2 forbids. They must not break the stream.
    return Response(b"ok", headers={"connection": "close", "keep-alive": "timeout=5",
                                    "x-kept": "yes"})


@app.get("/slow")
async def slow(request: Request):
    global slow_running, slow_peak
    with slow_lock:
        slow_running += 1
        slow_peak = max(slow_peak, slow_running)
    try:
        await asyncio.sleep(1.5)
    finally:
        with slow_lock:
            slow_running -= 1
    return {"ok": True}


@app.websocket("/ws")
async def ws(request: Request, sock):
    async for message in sock:
        await sock.send(message)


def expect_refusal(label: str, fragment: str, **options) -> None:
    try:
        with TestClient(App(), **options):
            pass
    except ValueError as exc:
        check(fragment in str(exc), f"{label}: message {exc!r} lacks {fragment!r}")
    except Exception as exc:
        failures.append(f"{label}: raised {type(exc).__name__}: {exc}, expected ValueError")
    else:
        failures.append(f"{label}: server started")


def startup_refusals(certs: Path) -> None:
    cert, key = issue(certs, "refusal")
    other_cert, other_key = issue(certs, "other")
    empty = certs / "empty.pem"
    empty.write_text("")
    expect_refusal("cert alone", "together", tls_cert=cert)
    expect_refusal("key alone", "together", tls_key=key)
    expect_refusal("missing cert", "missing.crt", tls_cert=certs / "missing.crt", tls_key=key)
    expect_refusal("missing key", "missing.key", tls_cert=cert, tls_key=certs / "missing.key")
    expect_refusal("empty cert", "no certificate", tls_cert=empty, tls_key=key)
    expect_refusal("empty key", str(empty), tls_cert=cert, tls_key=empty)
    expect_refusal("certificate as key", str(other_cert), tls_cert=cert, tls_key=other_cert)
    expect_refusal("mismatched key", "does not fit", tls_cert=cert, tls_key=other_key)


# ---------------------------------------------------------------------------
# HTTPS over HTTP/1.1
# ---------------------------------------------------------------------------
def https(client: TestClient) -> None:
    response = client.get("/")
    check(response.status_code == 200, f"https GET: {response.status_code}")
    check(response.http_version == "HTTP/1.1", f"TestClient speaks {response.http_version}")
    check(response.json()["host"] == f"127.0.0.1:{client.port}", f"host {response.text}")
    body = bytes(range(256)) * 4096
    posted = client.post("/echo", content=body).json()
    check(posted["length"] == len(body), f"https POST length {posted}")
    with client.stream("GET", "/events") as stream:
        text = "".join(stream.iter_text())
    check(text.count("data:") == 3, f"https SSE: {text!r}")

    async def echo_socket() -> str:
        async with client.websocket("/ws") as sock:
            await sock.send("over tls")
            return await sock.recv()

    check(asyncio.run(echo_socket()) == "over tls", "wss echo")

    # A TLS 1.1 client gets no connection at all.
    old = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    old.load_verify_locations(client.app_cert)
    try:
        # Deprecated, which is the point.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            old.maximum_version = ssl.TLSVersion.TLSv1_1
            old.minimum_version = ssl.TLSVersion.TLSv1_1
        with socket.create_connection(("127.0.0.1", client.port), timeout=5) as raw:
            with old.wrap_socket(raw, server_hostname="127.0.0.1"):
                failures.append("a TLS 1.1 handshake succeeded")
    except (ssl.SSLError, ValueError, OSError):
        pass

    # Plain HTTP on the TLS port fails cleanly and leaves the server serving.
    with socket.create_connection(("127.0.0.1", client.port), timeout=5) as raw:
        raw.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        try:
            reply = raw.recv(4096)
        except OSError:
            reply = b""
    check(not reply.startswith(b"HTTP/1.1 200"), f"plain HTTP answered over TLS: {reply[:40]!r}")
    check(client.get("/").status_code == 200, "server stopped serving after a plain request")


# ---------------------------------------------------------------------------
# HTTP/2
# ---------------------------------------------------------------------------
def http2(base_url: str, verify, port: int, label: str, prior_knowledge: bool = False) -> None:
    options = {"http1": False} if prior_knowledge else {}
    with httpx.Client(http2=True, verify=verify, base_url=base_url, timeout=10, **options) as h:
        response = h.get("/")
        check(response.http_version == "HTTP/2", f"{label}: spoke {response.http_version}")
        check(response.json()["host"] == f"127.0.0.1:{port}",
              f"{label}: host from :authority, got {response.text}")

        # Larger than the 65,535-byte default window, in both directions.
        body = bytes(range(256)) * 12_000
        import hashlib

        sha = hashlib.sha256(body).hexdigest()
        echoed = h.post("/echo", content=body).json()
        check(echoed == {"length": len(body), "sha": sha}, f"{label}: echo {echoed}")

        def chunks():
            for start in range(0, len(body), 100_000):
                yield body[start:start + 100_000]

        streamed = h.post("/upload", content=chunks()).json()
        check(streamed == {"length": len(body), "sha": sha}, f"{label}: streamed {streamed}")

        with h.stream("GET", "/events") as stream:
            text = "".join(stream.iter_text())
        check(text.count("data:") == 3, f"{label}: SSE {text!r}")

        head = h.head("/")
        check(head.status_code == 200 and head.content == b"", f"{label}: HEAD {head}")
        check(head.headers.get("content-length") == str(len(h.get("/").content)),
              f"{label}: HEAD length {head.headers}")
        check(h.get("/nope").status_code == 404, f"{label}: 404")
        check(h.delete("/").status_code == 405, f"{label}: 405")

        hop_response = h.get("/hop")
        check(hop_response.status_code == 200 and hop_response.text == "ok",
              f"{label}: hop-by-hop headers broke the stream: {hop_response}")
        check(hop_response.headers.get("x-kept") == "yes", f"{label}: {hop_response.headers}")

        preflight = h.options("/echo", headers={
            "origin": "https://site.example",
            "access-control-request-method": "POST",
        })
        check(preflight.status_code == 204, f"{label}: preflight {preflight.status_code}")
        check(preflight.headers.get("access-control-allow-origin") == "https://site.example",
              f"{label}: preflight headers {preflight.headers}")

        # A socket route cannot be upgraded over HTTP/2; the answer says so.
        upgrade = h.get("/ws")
        check(upgrade.status_code == 426, f"{label}: websocket route over h2 {upgrade}")

    async def many() -> list[int]:
        async with httpx.AsyncClient(http2=True, verify=verify, base_url=base_url,
                                     timeout=10, **options) as h:
            replies = await asyncio.gather(*(h.get("/") for _ in range(150)))
            return [r.status_code for r in replies if r.http_version == "HTTP/2"]

    statuses = asyncio.run(many())
    check(statuses == [200] * 150, f"{label}: 150 multiplexed requests: {statuses[:5]}...")


def limited_body(cert: Path, key: Path, trust) -> None:
    with TestClient(app, tls_cert=cert, tls_key=key, max_body=1000) as client:
        with httpx.Client(http2=True, verify=trust, base_url=client.base_url) as h:
            response = h.post("/echo", content=b"x" * 5000)
            check(response.status_code == 413, f"h2 over max_body: {response.status_code}")
            check(h.get("/").status_code == 200, "h2 connection unusable after a 413")


def http1_only(cert: Path, key: Path, trust) -> None:
    with TestClient(app, tls_cert=cert, tls_key=key, http2=False) as client:
        with httpx.Client(http2=True, verify=trust) as h:
            version = h.get(client.base_url + "/").http_version
            check(version == "HTTP/1.1", f"http2=False negotiated {version}")
        with socket.create_connection(("127.0.0.1", client.port)) as raw:
            context = ssl.create_default_context(cafile=cert)
            context.set_alpn_protocols(["h2", "http/1.1"])
            with context.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                check(tls.selected_alpn_protocol() == "http/1.1",
                      f"ALPN chose {tls.selected_alpn_protocol()}")
    with TestClient(app, http2=False) as client:
        with httpx.Client(http1=False, http2=True) as h:
            try:
                h.get(client.base_url + "/")
                failures.append("http2=False answered prior-knowledge HTTP/2")
            except httpx.HTTPError:
                pass


# ---------------------------------------------------------------------------
# rapid reset
# ---------------------------------------------------------------------------
class RawH2:
    """A cleartext HTTP/2 connection driven frame by frame."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.sock = socket.create_connection(("127.0.0.1", port))
        self.conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
        self.conn.initiate_connection()
        self.flush()

    def flush(self) -> None:
        self.sock.sendall(self.conn.data_to_send())

    def open(self, path: str) -> int:
        stream = self.conn.get_next_available_stream_id()
        self.conn.send_headers(stream, [
            (":method", "GET"), (":path", path), (":scheme", "http"),
            (":authority", f"127.0.0.1:{self.port}"),
        ], end_stream=True)
        return stream

    def drain(self, wait: float) -> list:
        self.sock.settimeout(wait)
        events = []
        try:
            while data := self.sock.recv(65536):
                for event in self.conn.receive_data(data):
                    events.append(event)
                    # As a real client does, or the window fills and the
                    # server can send nothing more on this connection.
                    if isinstance(event, h2.events.DataReceived):
                        self.conn.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id
                        )
                self.flush()
        except (TimeoutError, BlockingIOError):
            pass
        return events

    def status(self, stream: int, wait: float = 5) -> str | None:
        self.flush()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            for event in self.drain(0.2):
                if isinstance(event, h2.events.ResponseReceived) and event.stream_id == stream:
                    return dict(event.headers)[b":status"].decode()
        return None


def rapid_reset(port: int) -> None:
    global slow_peak
    slow_peak = 0
    flood = RawH2(port)
    closed_by_server = 0
    for _ in range(60):
        try:
            streams = [flood.open("/slow") for _ in range(100)]
            flood.flush()
            # Long enough for the requests to reach their handlers, which is
            # what hyper's own reset limit does not cover.
            flood.drain(0.01)
            for stream in streams:
                # Already closed if the server refused it with a 503.
                with contextlib.suppress(h2.exceptions.StreamClosedError):
                    flood.conn.reset_stream(stream)
            flood.flush()
        except (OSError, h2.exceptions.ProtocolError):
            # hyper sends GOAWAY when too many streams are reset before it
            # accepts them, which happens when the server is slow to accept.
            # That is a defence working; an attacker would reconnect, so this
            # does too, and the limits below must still hold.
            closed_by_server += 1
            flood.sock.close()
            flood = RawH2(port)
    check(slow_peak <= 200, f"one connection ran {slow_peak} handlers at once")
    check(slow_peak >= 50, f"flood never reached its handlers (peak {slow_peak}, "
                           f"{closed_by_server} connections closed); test is void")
    others = httpx.get(f"http://127.0.0.1:{port}/").status_code
    check(others == 200, f"another client got {others} during the flood")

    # The count follows the handlers: once they finish, the same connection
    # is served again.
    time.sleep(2)
    try:
        status = flood.status(flood.open("/"))
    except (OSError, h2.exceptions.ProtocolError):
        flood.sock.close()
        flood = RawH2(port)
        status = flood.status(flood.open("/"))
    check(status == "200", f"the flooded connection got {status} after its handlers finished")
    flood.sock.close()


# ---------------------------------------------------------------------------
# idle connections
# ---------------------------------------------------------------------------
def closed_within(sock: socket.socket, limit: float) -> float | None:
    sock.settimeout(limit)
    start = time.monotonic()
    try:
        while sock.recv(65536):
            pass
    except TimeoutError:
        return None
    except OSError:
        pass
    return time.monotonic() - start


def idle(tls_port: int, plain_port: int) -> None:
    results: dict[str, object] = {}

    def silent_tls() -> None:
        with socket.create_connection(("127.0.0.1", tls_port)) as sock:
            results["silent before a TLS handshake"] = closed_within(sock, 40)

    def silent_plain() -> None:
        with socket.create_connection(("127.0.0.1", plain_port)) as sock:
            results["silent before choosing a protocol"] = closed_within(sock, 40)

    def after_request(port: int, answer_pings: bool) -> tuple[float | None, bool]:
        """Seconds until an HTTP/2 connection idle since one request closes."""
        raw = RawH2(port)
        if raw.status(raw.open("/")) != "200":
            return None, False
        # Frames are read by hand from here: the h2 state machine refuses the
        # PING a graceful shutdown sends after its GOAWAY.
        start = time.monotonic()
        goaway = False
        pending = b""
        raw.sock.settimeout(5)
        try:
            # Keep-alive PINGs arrive every 20 s, so a read timeout alone
            # never ends this if the server never closes.
            while time.monotonic() - start < 45:
                try:
                    data = raw.sock.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    return time.monotonic() - start, goaway
                pending += data
                while len(pending) >= 9 and len(pending) >= 9 + int.from_bytes(pending[:3]):
                    length = int.from_bytes(pending[:3])
                    kind, flags = pending[3], pending[4]
                    payload, pending = pending[9:9 + length], pending[9 + length:]
                    goaway |= kind == 0x7
                    if kind == 0x6 and not flags & 0x1 and answer_pings:
                        raw.sock.sendall(b"\x00\x00\x08\x06\x01\x00\x00\x00\x00" + payload)
        except OSError:
            return time.monotonic() - start, goaway
        finally:
            raw.sock.close()
        return None, goaway

    def idle_h2() -> None:
        results["idle HTTP/2"], results["idle HTTP/2 GOAWAY"] = after_request(plain_port, True)

    def deaf_h2() -> None:
        results["unanswered GOAWAY"], _ = after_request(plain_port, False)

    def busy_h2() -> None:
        raw = RawH2(plain_port)
        stream = raw.open("/long")
        events = 0
        ended = False
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and not ended:
            for event in raw.drain(1):
                if isinstance(event, h2.events.DataReceived) and event.stream_id == stream:
                    events += event.data.count(b"data:")
                if isinstance(event, h2.events.StreamEnded | h2.events.StreamReset
                              | h2.events.ConnectionTerminated):
                    ended = True
        results["long response events"] = events
        raw.sock.close()

    cases = (silent_tls, silent_plain, idle_h2, deaf_h2, busy_h2)
    threads = [threading.Thread(target=case) for case in cases]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for label in ("silent before a TLS handshake", "silent before choosing a protocol",
                  "idle HTTP/2"):
        elapsed = results.get(label)
        check(isinstance(elapsed, float) and 14 <= elapsed <= 25,
              f"{label}: closed after {elapsed} s, expected 15 to 20")
    check(results.get("idle HTTP/2 GOAWAY") is True, "idle HTTP/2 closed without GOAWAY")
    # A client that never acknowledges the GOAWAY's PING is dropped instead.
    deaf = results.get("unanswered GOAWAY")
    check(isinstance(deaf, float) and 29 <= deaf <= 40,
          f"a client ignoring GOAWAY was closed after {deaf} s, expected 30 to 35")
    check(results.get("long response events") == 8,
          f"a 40 s response was cut off: {results.get('long response events')} of 8 events")


def main() -> None:
    certs = Path(tempfile.mkdtemp(prefix="oxbrook-tls-"))
    cert, key = issue(certs, "server")
    trust = ssl.create_default_context(cafile=cert)

    steps = []
    try:
        startup_refusals(certs)
        print("  startup_refusals: ok")
    except Exception as exc:
        failures.append(f"startup_refusals raised {type(exc).__name__}: {exc}")

    with TestClient(app, tls_cert=cert, tls_key=key) as secure, TestClient(app) as plain:
        secure.app_cert = cert
        steps = [
            ("https", lambda: https(secure)),
            ("http2 over tls", lambda: http2(secure.base_url, trust, secure.port, "h2")),
            ("http2 cleartext", lambda: http2(plain.base_url, True, plain.port, "h2c",
                                              prior_knowledge=True)),
            ("http1 still plain", lambda: check(
                plain.get("/").http_version == "HTTP/1.1", "plain HTTP/1.1")),
            ("limited body", lambda: limited_body(cert, key, trust)),
            ("http1 only", lambda: http1_only(cert, key, trust)),
            ("rapid reset", lambda: rapid_reset(plain.port)),
            ("idle", lambda: idle(secure.port, plain.port)),
        ]
        for name, step in steps:
            try:
                step()
                print(f"  {name}: ok")
            except Exception as exc:
                failures.append(f"{name} raised {type(exc).__name__}: {exc}")
                print(f"  {name}: ERROR")

    import shutil

    shutil.rmtree(certs, ignore_errors=True)
    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
