"""Self-signed test certificates, with nothing but the standard library.

`cryptography` publishes no wheel for some platforms the server's wheels are
built for (Windows on ARM), and a wheel is tested on the platform it was
built for. So the TLS suites make their certificates here: a P-256 key, an
ECDSA signature and the few DER structures a certificate needs. Only ever for
tests: the nonce is random and nothing here is constant-time.

A mistake here cannot pass quietly: the server refuses a key it cannot load,
and a client refuses a certificate whose signature does not verify.
"""

import base64
import datetime
import hashlib
import ipaddress
import secrets
from pathlib import Path

# P-256 (secp256r1).
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    (x1, y1), (x2, y2) = p, q
    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return None
        slope = (3 * x1 * x1 + _A) * pow(2 * y1, -1, _P) % _P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (slope * slope - x1 - x2) % _P
    return x3, (slope * (x1 - x3) - y1) % _P


def _mul(k: int, point):
    result = None
    while k:
        if k & 1:
            result = _add(result, point)
        point = _add(point, point)
        k >>= 1
    return result


# ---- DER ----------------------------------------------------------------------
def _tlv(tag: int, body: bytes) -> bytes:
    n = len(body)
    if n < 0x80:
        length = bytes([n])
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + body


def _seq(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _int(n: int) -> bytes:
    raw = n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big")  # a leading 0 keeps it positive
    return _tlv(0x02, raw)


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    out = bytearray([40 * parts[0] + parts[1]])
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.append(0x80 | (part & 0x7F))
            part >>= 7
        out.extend(reversed(chunk))
    return _tlv(0x06, bytes(out))


def _bits(raw: bytes) -> bytes:
    return _tlv(0x03, b"\x00" + raw)


def _octets(raw: bytes) -> bytes:
    return _tlv(0x04, raw)


def _time(moment: datetime.datetime) -> bytes:
    return _tlv(0x17, moment.strftime("%y%m%d%H%M%SZ").encode())


_EC_KEY = _oid("1.2.840.10045.2.1")
_P256 = _oid("1.2.840.10045.3.1.7")
_ECDSA_SHA256 = _seq(_oid("1.2.840.10045.4.3.2"))
_TRUE = _tlv(0x01, b"\xff")


def _pem(label: str, der: bytes) -> bytes:
    lines = base64.encodebytes(der).decode().replace("\n", "")
    wrapped = "\n".join(lines[i:i + 64] for i in range(0, len(lines), 64))
    return f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n".encode()


def issue(directory: Path, name: str) -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1 and localhost, valid for a
    day, and its key, written as `<name>.crt` and `<name>.key`."""
    secret = secrets.randbelow(_N - 1) + 1
    x, y = _mul(secret, _G)
    public = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")

    subject = _seq(_tlv(0x31, _seq(_oid("2.5.4.3"), _tlv(0x0C, f"oxbrook {name}".encode()))))
    now = datetime.datetime.now(datetime.UTC)
    names = _seq(_tlv(0x82, b"localhost"),
                 _tlv(0x87, ipaddress.ip_address("127.0.0.1").packed))
    extensions = _tlv(0xA3, _seq(
        _seq(_oid("2.5.29.17"), _octets(names)),
        _seq(_oid("2.5.29.19"), _TRUE, _octets(_seq(_TRUE))),
    ))
    tbs = _seq(
        _tlv(0xA0, _int(2)),
        _int(secrets.randbits(63) | 1),
        _ECDSA_SHA256,
        subject,
        _seq(_time(now - datetime.timedelta(hours=1)), _time(now + datetime.timedelta(days=1))),
        subject,
        _seq(_seq(_EC_KEY, _P256), _bits(public)),
        extensions,
    )

    digest = int.from_bytes(hashlib.sha256(tbs).digest(), "big")
    while True:
        nonce = secrets.randbelow(_N - 1) + 1
        r = _mul(nonce, _G)[0] % _N
        s = pow(nonce, -1, _N) * (digest + r * secret) % _N
        if r and s:
            break
    certificate = _seq(tbs, _ECDSA_SHA256, _bits(_seq(_int(r), _int(s))))

    key = _seq(
        _int(0),
        _seq(_EC_KEY, _P256),
        _octets(_seq(_int(1), _octets(secret.to_bytes(32, "big")), _tlv(0xA1, _bits(public)))),
    )
    cert_path = directory / f"{name}.crt"
    key_path = directory / f"{name}.key"
    cert_path.write_bytes(_pem("CERTIFICATE", certificate))
    key_path.write_bytes(_pem("PRIVATE KEY", key))
    return cert_path, key_path
