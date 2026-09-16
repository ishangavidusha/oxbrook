#!/usr/bin/env python3
"""What TLS and HTTP/2 cost, and what HTTP/2 detection costs HTTP/1.1.

    bench/protocols.py --python .venv/bin/python

Five ways to serve the same hello-world route, interleaved over several rounds
so drift in the host lands on every mode alike:

* HTTP/1.1 on a plain port with `http2=False`, the old connection handler;
* HTTP/1.1 on a plain port with HTTP/2 on, which sniffs every connection's
  first bytes before choosing — the cost every plain HTTP/1.1 user pays;
* HTTP/2 in cleartext, by prior knowledge;
* HTTPS over HTTP/1.1;
* HTTPS over HTTP/2.

Concurrency is the same in-flight count for every mode: HTTP/1.1 uses one
connection per request, HTTP/2 multiplexes the same number over fewer
connections.
"""
import argparse
import datetime
import ipaddress
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from machine import Session

ROOT = Path(__file__).resolve().parent.parent
PORT = 8772


def certificate(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "oxbrook bench")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def wait_port(proc, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def measure(py, server_args, client_args, scheme, seconds):
    proc = subprocess.Popen(
        [py, "bench/oxbrook_app.py", "--port", str(PORT), *server_args],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        if not wait_port(proc):
            raise RuntimeError(f"server failed: {(proc.stderr.read() or '')[-400:]}")
        url = f"{scheme}://127.0.0.1:{PORT}/"
        base = ["oha", "--no-tui", "--output-format", "json", *client_args, url]
        subprocess.run([*base[:4], "-z", "1s", *base[4:]], capture_output=True, check=True)
        out = subprocess.run([*base[:4], "-z", f"{seconds}s", *base[4:]],
                             capture_output=True, text=True, check=True).stdout
        result = json.loads(out)
        return {
            "rps": result["summary"]["requestsPerSec"],
            "success": result["summary"]["successRate"],
            "p99_ms": (result["latencyPercentiles"].get("p99") or 0) * 1000,
        }
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--duration", type=int, default=6)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument("--streams", type=int, default=8, help="HTTP/2 streams per connection")
    ap.add_argument("--strict", action="store_true",
                    help="refuse to measure when the host fails preflight")
    args = ap.parse_args()

    py = str(Path(args.python).absolute())
    session = Session("protocols", executable=py, conns=args.concurrency, strict=args.strict)
    session.announce()
    cert, key = certificate(Path(tempfile.mkdtemp(prefix="oxbrook-bench-")))
    tls = ["--tls-cert", str(cert), "--tls-key", str(key)]
    h1 = ["-c", str(args.concurrency)]
    h2 = ["--http2", "-c", str(max(1, args.concurrency // args.streams)),
          "-p", str(args.streams)]
    modes = [
        ("http/1.1, http2=False", ["--no-http2"], h1, "http"),
        ("http/1.1, http2 on", [], h1, "http"),
        ("h2c", [], h2, "http"),
        ("https http/1.1", tls, [*h1, "--insecure"], "https"),
        ("https h2", tls, [*h2, "--insecure"], "https"),
    ]

    runs: dict[str, list[dict]] = {label: [] for label, *_ in modes}
    for round_ in range(args.rounds):
        for label, server_args, client_args, scheme in modes:
            runs[label].append(measure(py, server_args, client_args, scheme, args.duration))
        print(f"round {round_ + 1}/{args.rounds} done", flush=True)

    rows = []
    baseline = statistics.median(r["rps"] for r in runs[modes[0][0]])
    print(f"\n{'mode':<24}{'req/s':>10}{'vs http2=False':>16}{'p99 ms':>9}{'ok%':>7}")
    for label, *_ in modes:
        rps = statistics.median(r["rps"] for r in runs[label])
        p99 = statistics.median(r["p99_ms"] for r in runs[label])
        ok = min(r["success"] for r in runs[label])
        rows.append({"mode": label, "rps": rps, "p99_ms": p99, "success": ok,
                     "runs": [r["rps"] for r in runs[label]]})
        print(f"{label:<24}{rps:>10,.0f}{rps / baseline:>15.2f}x{p99:>9.2f}{ok * 100:>7.1f}")

    out = session.finish({"args": vars(args), "rows": rows})
    print(f"saved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
