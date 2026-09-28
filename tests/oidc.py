#!/usr/bin/env python3
"""OpenID Connect, against an issuer this suite controls and against Keycloak.

The first half serves a discovery document and key set from a small server of
its own, because the attacks need an issuer that misbehaves on request:

* Only public-key algorithms, each with its own kind of key. An HMAC token
  signed with the provider's public key, an unsigned one, one whose header
  names a key of another type, and one signed with a published symmetric or
  encryption key are all refused.
* Keys are fetched once per process, whatever the number of loops asking at
  once, and a token naming an unknown key id fetches them again at most once
  a minute: a stream of made-up key ids is not a stream of fetches.
* A rotated-in key is picked up; a withdrawn one stops working at the next
  refresh, including for tokens already verified and remembered.
* A provider that cannot be reached is `503`, not `401`, and is not asked
  again by every request meanwhile. One that goes away after keys were
  fetched changes nothing.
* A discovery document naming another issuer, and one too large to be one,
  are refused. Redirects never go from HTTPS to plain HTTP.
* A verified token is remembered until its `exp`, and a refused one never is.
* No token reaches a log line.

The second half runs against a real Keycloak (`make up`): real tokens for
users and for a service client, audience and role checks, an ID token offered
as an access token, the same principal over HTTP, a WebSocket and an MCP tool
call, and a signing key rotated mid-run through Keycloak's admin API.

With OXBROOK_REQUIRE_KEYCLOAK set an unreachable Keycloak is a failure rather
than a SKIP of that half; `make verify` and CI both set it.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from oxbrook import App, Depends, Request, _jwks
from oxbrook.auth import JWT, OIDC, Principal, principal
from oxbrook.testing import TestClient

KEYCLOAK = os.environ.get("OXBROOK_TEST_KEYCLOAK", "http://localhost:8199")
REALM = "oxbrook"

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ---- an issuer that does as it is told ----------------------------------------


def _public_jwk(private, kid: str, alg: str | None, use: str | None = "sig") -> dict:
    to_jwk = RSAAlgorithm.to_jwk if isinstance(private, rsa.RSAPrivateKey) else ECAlgorithm.to_jwk
    jwk = to_jwk(private.public_key(), as_dict=True)
    jwk["kid"] = kid
    if alg is not None:
        jwk["alg"] = alg
    if use is not None:
        jwk["use"] = use
    return jwk


RSA_A = rsa.generate_private_key(public_exponent=65537, key_size=2048)
RSA_B = rsa.generate_private_key(public_exponent=65537, key_size=2048)
RSA_ENC = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
OCT_SECRET = b"published-symmetric-OCTSENTINEL-" + b"k" * 16
PUBLIC_PEM_A = RSA_A.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)


class Issuer:
    """A discovery document and key set, served from 127.0.0.1."""

    def __init__(self, keys: list[dict], *, delay: float = 0.0) -> None:
        self.keys = list(keys)
        self.delay = delay
        self.named_issuer: str | None = None
        self.failing = False
        self.huge = False
        self.discoveries = 0
        self.key_fetches = 0
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                time.sleep(issuer.delay)
                if issuer.failing:
                    self.send_response(500)
                    self.end_headers()
                    return
                if self.path == "/.well-known/openid-configuration":
                    issuer.discoveries += 1
                    body = json.dumps({
                        "issuer": issuer.named_issuer or issuer.url,
                        "jwks_uri": issuer.url + "/keys",
                        # HS256 advertised on purpose: it must still be refused.
                        "id_token_signing_alg_values_supported": ["RS256", "ES256", "HS256"],
                    }).encode()
                elif self.path == "/keys":
                    issuer.key_fetches += 1
                    if issuer.huge:
                        # A usable key set, only too large: refused for its size alone.
                        body = json.dumps({"keys": issuer.keys, "pad": "x" * (2 << 20)}).encode()
                    else:
                        body = json.dumps({"keys": issuer.keys}).encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass  # the huge key set: the client stops reading, as it should

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


STANDARD_KEYS = [
    _public_jwk(RSA_A, "a", "RS256"),
    _public_jwk(EC_KEY, "ec", "ES256"),
    _public_jwk(RSA_ENC, "enc", "RSA-OAEP", use="enc"),
    # An encryption key with no `alg`, so only its `use` says not to sign with it.
    _public_jwk(RSA_ENC, "enc-bare", None, use="enc"),
    {"kty": "oct", "kid": "oct", "alg": "HS256", "k": b64(OCT_SECRET)},
]


def claims_for(issuer: Issuer, **overrides) -> dict:
    now = int(time.time())
    claims = {
        "sub": "u1", "aud": "api", "iss": issuer.url, "iat": now, "exp": now + 600,
        "scope": "notes:read notes:write", "groups": ["admin"],
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def signed(issuer: Issuer, key=RSA_A, kid="a", alg="RS256", **overrides) -> str:
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims_for(issuer, **overrides), key, algorithm=alg, headers=headers)


def forged(issuer: Issuer, header: dict, secret: bytes | None) -> str:
    """A token put together by hand, for what PyJWT refuses to produce."""
    signing_input = (
        b64(json.dumps(header).encode()) + "." + b64(json.dumps(claims_for(issuer)).encode())
    )
    signature = b"" if secret is None else hmac.new(
        secret, signing_input.encode(), hashlib.sha256
    ).digest()
    return signing_input + "." + b64(signature)


# ---- the app over the issuer --------------------------------------------------

main_issuer = Issuer(STANDARD_KEYS)
rotating_issuer = Issuer([_public_jwk(RSA_A, "a", "RS256")])
withdrawing_issuer = Issuer([_public_jwk(RSA_A, "a", "RS256"), _public_jwk(RSA_B, "b", "RS256")])
flaky_issuer = Issuer([_public_jwk(RSA_A, "a", "RS256")])
slow_issuer = Issuer([_public_jwk(RSA_A, "a", "RS256")], delay=0.4)
lying_issuer = Issuer(STANDARD_KEYS)
lying_issuer.named_issuer = "https://someone-else.example.com"
huge_issuer = Issuer(STANDARD_KEYS)
huge_issuer.huge = True

with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    CLOSED_PORT = probe.getsockname()[1]
DOWN = f"http://127.0.0.1:{CLOSED_PORT}"

provider = OIDC(main_issuer.url, audience="api", roles_claim="groups")
rotating = OIDC(rotating_issuer.url, audience="api", name="rotating")
withdrawing = OIDC(withdrawing_issuer.url, audience="api", keys_max_age=0.5, name="withdrawing")
flaky = OIDC(flaky_issuer.url, audience="api", keys_max_age=0.3, cache_size=0, name="flaky")
slow = OIDC(slow_issuer.url, audience="api", name="slow")
lying = OIDC(lying_issuer.url, audience="api", name="lying")
huge = OIDC(huge_issuer.url, audience="api", name="huge")
down = OIDC(DOWN, audience="api", name="down")
small = OIDC(main_issuer.url, audience="api", cache_size=4, name="small")
narrowed = OIDC(main_issuer.url, audience="api", algorithms=["RS256"], name="narrowed")
cognito_like = OIDC(main_issuer.url, audience=None, name="cognito_like",
                    claims={"token_use": "access", "client_id": ["web", "cli"]})

app = App()


def describe(who: Principal | None):
    if who is None:
        return None
    return {"subject": who.subject, "scheme": who.scheme, "scopes": sorted(who.scopes),
            "roles": sorted(who.roles)}


for path, scheme in (("/main", provider), ("/rotating", rotating), ("/withdrawing", withdrawing),
                     ("/flaky", flaky), ("/slow", slow), ("/lying", lying), ("/huge", huge),
                     ("/down", down), ("/small", small), ("/narrowed", narrowed),
                     ("/cognito", cognito_like)):
    def route(scheme=scheme):
        async def handler(_: Request, who=Depends(principal)):
            return describe(who)
        return handler

    app.get(path, auth=scheme)(route())


@app.get("/main-admin", auth=provider.requires("notes:read", roles=("admin",)))
async def main_admin(_: Request):
    return {"ok": True}


@app.get("/main-root", auth=provider.requires(roles=("root",)))
async def main_root(_: Request):
    return {"ok": True}


# ---- cases: the issuer this suite controls -------------------------------------


def construction_rules() -> None:
    def refused(fn, kind=ValueError):
        try:
            fn()
        except kind:
            return True
        return False

    check(refused(lambda: OIDC("http://login.example.com", audience="a")),
          "a plain-HTTP issuer off loopback was accepted")
    OIDC("http://sso.internal", audience="a", allow_http=True)
    OIDC("http://localhost:8199/realms/x", audience="a")
    check(refused(lambda: OIDC("https://x.example.com", audience="a", algorithms=["HS256"])),
          "an HMAC algorithm was accepted for a provider's tokens")
    check(refused(lambda: OIDC("https://x.example.com", audience="a", algorithms=["none"])),
          "the none algorithm was accepted")
    check(refused(lambda: OIDC("https://x.example.com", audience="a", roles_claim=("",)),
                  TypeError), "an empty claim path was accepted")
    check(refused(lambda: OIDC("https://x.example.com", audience="a", cache_size=-1)),
          "a negative cache size was accepted")
    check(refused(lambda: OIDC.entra("common", audience="a")),
          "Entra's multi-tenant 'common' was accepted as an issuer")
    check(refused(lambda: OIDC.auth0("https://acme.auth0.com", audience="a")),
          "auth0 took a URL where it wants a domain")

    preset = OIDC.keycloak("https://sso.example.com/", "acme", audience="api")
    check(preset.issuer == "https://sso.example.com/realms/acme"
          and preset.roles_claim == ("realm_access", "roles"),
          f"keycloak preset: {preset.issuer} {preset.roles_claim}")
    check(OIDC.auth0("acme.eu.auth0.com", audience="a").issuer == "https://acme.eu.auth0.com/",
          "auth0's issuer ends in a slash")
    v2 = OIDC.entra("tid", audience="a")
    v1 = OIDC.entra("tid", audience="a", version=1)
    check(v2.issuer == "https://login.microsoftonline.com/tid/v2.0" and v2.scopes_claim == "scp"
          and v2.roles_claim == "roles", f"entra v2: {v2.issuer}")
    check(v1.issuer == "https://sts.windows.net/tid/" and v1.discovery_url
          == "https://login.microsoftonline.com/tid/.well-known/openid-configuration",
          f"entra v1: {v1.issuer} {v1.discovery_url}")
    check(OIDC.okta("acme.okta.com", audience="api://default").issuer
          == "https://acme.okta.com/oauth2/default", "okta issuer")
    cognito = OIDC.cognito("eu-west-1", "eu-west-1_Abc", client_id="c1")
    check(cognito.issuer == "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_Abc"
          and cognito.audience is None
          and cognito.expected == {"token_use": "access", "client_id": frozenset({"c1"})},
          f"cognito: {cognito.issuer} {cognito.expected}")
    google = OIDC.google("client.apps.googleusercontent.com")
    check(google._issuers == ["https://accounts.google.com", "accounts.google.com"],
          f"google accepts {google._issuers}")
    firebase = OIDC.firebase("acme-app")
    check(firebase.issuer == "https://securetoken.google.com/acme-app"
          and firebase.audience == "acme-app", "firebase issuer and audience")
    # A preset's settings are defaults: anything may be overridden.
    check(OIDC.auth0("a.auth0.com", audience="x", scopes_claim="permissions").scopes_claim
          == "permissions", "a preset ignored an override")


def issuer_tokens_are_accepted(c: TestClient) -> None:
    r = c.get("/main", headers=bearer(signed(main_issuer)))
    check(r.status_code == 200 and r.json() == {
        "subject": "u1", "scheme": "oidc", "scopes": ["notes:read", "notes:write"],
        "roles": ["admin"]}, f"an RS256 token from the issuer gave {r.status_code} {r.text}")
    r = c.get("/main", headers=bearer(signed(main_issuer, key=EC_KEY, kid="ec", alg="ES256")))
    check(r.status_code == 200, f"an ES256 token from the issuer gave {r.status_code}")
    check(c.get("/main-admin", headers=bearer(signed(main_issuer))).status_code == 200,
          "a role the token has was refused")
    r = c.get("/main-root", headers=bearer(signed(main_issuer)))
    check(r.status_code == 403 and r.json()["detail"] == "requires role root",
          f"a missing role gave {r.status_code} {r.text}")
    # A role and a scope with the same name are not the same thing.
    r = c.get("/main-admin", headers=bearer(signed(main_issuer, groups=None, scope="admin")))
    check(r.status_code == 403, f"a scope named like the role passed for it: {r.status_code}")
    check(provider.keys.fetches == 1 and main_issuer.discoveries == 1,
          f"{provider.keys.fetches} fetches, {main_issuer.discoveries} discoveries for one issuer")


def algorithms_are_the_providers(c: TestClient) -> None:
    attacks = {
        # The public key, as published, used as an HMAC secret.
        "HMAC signed with the public key": forged(
            main_issuer, {"alg": "HS256", "kid": "a", "typ": "JWT"}, PUBLIC_PEM_A),
        "HMAC with a published symmetric key": forged(
            main_issuer, {"alg": "HS256", "kid": "oct", "typ": "JWT"}, OCT_SECRET),
        "unsigned": forged(main_issuer, {"alg": "none", "kid": "a", "typ": "JWT"}, None),
        "RS256 header on the EC key": signed(main_issuer, kid="ec"),
        "ES256 header on the RSA key": signed(main_issuer, key=EC_KEY, kid="a", alg="ES256"),
        "signed with the encryption key": signed(main_issuer, key=RSA_ENC, kid="enc"),
        "signed with an encryption key without alg": signed(main_issuer, key=RSA_ENC,
                                                             kid="enc-bare"),
        "signed by a stranger": signed(main_issuer, key=RSA_B, kid="a"),
        "no key id, with several keys": signed(main_issuer, kid=None),
    }
    for label, token in attacks.items():
        r = c.get("/main", headers=bearer(token))
        check(r.status_code == 401 and r.json()["detail"] == "token invalid",
              f"{label}: {r.status_code} {r.text}")
    # algorithms= narrows what the provider advertises.
    check(c.get("/narrowed", headers=bearer(signed(main_issuer))).status_code == 200,
          "an RS256 token was refused where RS256 is the one algorithm allowed")
    r = c.get("/narrowed", headers=bearer(signed(main_issuer, key=EC_KEY, kid="ec", alg="ES256")))
    check(r.status_code == 401, f"ES256 passed where only RS256 is allowed: {r.status_code}")


def claims_are_checked(c: TestClient) -> None:
    cases = {
        "another audience": ({"aud": "other"}, "token invalid"),
        "another issuer": ({"iss": "https://someone-else.example.com"}, "token invalid"),
        "expired": ({"exp": int(time.time()) - 300}, "token expired"),
        "not yet valid": ({"nbf": int(time.time()) + 600}, "token invalid"),
        "no exp": ({"exp": None}, "token invalid"),
        "no sub": ({"sub": None}, "token invalid"),
    }
    for label, (overrides, detail) in cases.items():
        r = c.get("/main", headers=bearer(signed(main_issuer, **overrides)))
        check(r.status_code == 401 and r.json()["detail"] == detail,
              f"{label}: {r.status_code} {r.text}")
    # claims= : a value, or one of a list.
    good = signed(main_issuer, aud=None, token_use="access", client_id="cli")
    check(c.get("/cognito", headers=bearer(good)).status_code == 200,
          "expected claims that match were refused")
    for label, overrides in {"an ID token": {"token_use": "id", "client_id": "cli"},
                             "another client": {"token_use": "access", "client_id": "evil"},
                             "no client": {"token_use": "access"},
                             "a list": {"token_use": "access", "client_id": ["cli"]}}.items():
        r = c.get("/cognito", headers=bearer(signed(main_issuer, aud=None, **overrides)))
        check(r.status_code == 401, f"claims= accepted {label}: {r.status_code}")


def one_fetch_for_every_loop(c: TestClient) -> None:
    token = signed(slow_issuer)
    with ThreadPoolExecutor(16) as pool:
        statuses = list(pool.map(
            lambda _: httpx.get(c.base_url + "/slow", headers=bearer(token)).status_code,
            range(16)))
    check(statuses == [200] * 16, f"concurrent first requests answered {statuses}")
    check(slow.keys.fetches == 1 and slow_issuer.key_fetches == 1,
          f"16 concurrent first requests made {slow_issuer.key_fetches} key fetches")


def unknown_keys_are_rate_limited(c: TestClient) -> None:
    saved = _jwks.REFETCH_INTERVAL
    _jwks.REFETCH_INTERVAL = 1.0
    try:
        check(c.get("/rotating", headers=bearer(signed(rotating_issuer))).status_code == 200,
              "the rotating issuer's first token was refused")
        # Straight after a fetch, made-up key ids fetch nothing at all.
        for i in range(30):
            r = c.get("/rotating", headers=bearer(signed(rotating_issuer, kid=f"made-up-{i}")))
            check(r.status_code == 401, f"a made-up key id answered {r.status_code}")
        check(rotating_issuer.key_fetches == 1,
              f"30 made-up key ids made {rotating_issuer.key_fetches - 1} fetches")
        # Once the interval has passed, a flood of them makes exactly one.
        time.sleep(1.1)
        for i in range(30):
            c.get("/rotating", headers=bearer(signed(rotating_issuer, kid=f"again-{i}")))
        check(rotating_issuer.key_fetches == 2,
              f"a flood after the interval made {rotating_issuer.key_fetches - 1} fetches, not 1")
        # A key rotated in is picked up by the first token that names it.
        time.sleep(1.1)
        rotating_issuer.keys.append(_public_jwk(RSA_B, "b", "RS256"))
        r = c.get("/rotating", headers=bearer(signed(rotating_issuer, key=RSA_B, kid="b")))
        check(r.status_code == 200, f"a rotated-in key's token answered {r.status_code}")
        check(c.get("/rotating", headers=bearer(signed(rotating_issuer))).status_code == 200,
              "the old key stopped working while still published")
    finally:
        _jwks.REFETCH_INTERVAL = saved


def withdrawn_keys_stop_working(c: TestClient) -> None:
    old = signed(withdrawing_issuer)
    new = signed(withdrawing_issuer, key=RSA_B, kid="b")
    check(c.get("/withdrawing", headers=bearer(old)).status_code == 200, "old key refused")
    check(len(withdrawing._verified) == 1, "a verified token was not remembered")
    withdrawing_issuer.keys = [k for k in withdrawing_issuer.keys if k["kid"] != "a"]
    # The remembered token too: the cache must not outlive the key.
    deadline = time.monotonic() + 5
    status = 200
    while time.monotonic() < deadline and status == 200:
        time.sleep(0.2)
        status = c.get("/withdrawing", headers=bearer(old)).status_code
    check(status == 401, f"a withdrawn key's token still answered {status} after 5s")
    check(c.get("/withdrawing", headers=bearer(new)).status_code == 200,
          "the key still published stopped working")


def an_unreachable_provider_is_503(c: TestClient) -> None:
    r = c.get("/down", headers=bearer(signed(main_issuer)))
    check(r.status_code == 503 and r.headers.get("retry-after") == "5",
          f"an unreachable provider answered {r.status_code} {r.headers.get('retry-after')}")
    check(c.get("/down").status_code == 401, "no token at all was not a 401")
    before = down.keys.fetches
    for _ in range(10):
        c.get("/down", headers=bearer(signed(main_issuer)))
    check(down.keys.fetches == before,
          f"ten requests to a provider that just failed made {down.keys.fetches - before} "
          f"more attempts")
    try:
        asyncio.run(down.load())
        check(False, "load() against an unreachable provider did not raise")
    except RuntimeError:
        pass

    # Keys in hand keep working when the provider goes away afterwards.
    token = signed(flaky_issuer)
    check(c.get("/flaky", headers=bearer(token)).status_code == 200, "flaky: first token")
    flaky_issuer.failing = True
    time.sleep(0.4)
    for _ in range(3):
        r = c.get("/flaky", headers=bearer(token))
        check(r.status_code == 200, f"a provider gone after the first fetch gave {r.status_code}")
        time.sleep(0.1)


def bad_documents_are_refused(c: TestClient) -> None:
    r = c.get("/lying", headers=bearer(signed(lying_issuer)))
    check(r.status_code == 503, f"a discovery document naming another issuer gave {r.status_code}")
    check(lying.keys.keys is None and lying_issuer.key_fetches == 0,
          "keys were fetched from a discovery document naming another issuer")
    r = c.get("/huge", headers=bearer(signed(huge_issuer)))
    check(r.status_code == 503, f"a 2 MB key set gave {r.status_code}")

    handler = _jwks._NoDowngrade()
    request = urllib.request.Request("https://login.example.com/keys")
    try:
        handler.redirect_request(request, None, 302, "Found", {}, "http://login.example.com/k")
        check(False, "a redirect from https to http was followed")
    except urllib.error.HTTPError:
        pass
    followed = handler.redirect_request(request, None, 302, "Found", {},
                                        "https://cdn.example.com/keys")
    check(followed is not None, "an https to https redirect was refused")


def verified_tokens_are_remembered(c: TestClient) -> None:
    provider._verified.clear()
    token = signed(main_issuer)
    c.get("/main", headers=bearer(token))
    first = provider._verified.get(token)
    check(first is not None, "a verified token was not remembered")
    c.get("/main", headers=bearer(token))
    check(provider._verified.get(token) is first, "a remembered token was verified again")
    # Refusals are never remembered, so forgeries cannot fill it.
    for i in range(5):
        c.get("/main", headers=bearer(signed(main_issuer, key=RSA_B, sub=f"forged-{i}")))
    check(len(provider._verified) == 1, f"{len(provider._verified)} tokens remembered, not 1")
    # A remembered token still expires.
    quick = OIDC(main_issuer.url, audience="api", leeway=0, name="quick")
    app_quick = App()

    @app_quick.get("/", auth=quick)
    async def root(_: Request):
        return {"ok": True}

    with TestClient(app_quick) as q:
        short = signed(main_issuer, exp=int(time.time()) + 2)
        check(q.get("/", headers=bearer(short)).status_code == 200, "a short token refused")
        time.sleep(3.1)
        r = q.get("/", headers=bearer(short))
        check(r.status_code == 401 and r.json()["detail"] == "token expired",
              f"a remembered token past its exp gave {r.status_code} {r.text}")
    # Bounded.
    for i in range(10):
        c.get("/small", headers=bearer(signed(main_issuer, sub=f"u{i}")))
    check(len(small._verified) <= 4, f"a cache of 4 holds {len(small._verified)}")

    # JWT remembers too; that is what makes RS256 cheap past the first request.
    local = JWT(key=RSA_A.public_key(), algorithms=["RS256"], audience="api")
    principal_a = asyncio.run(local.check(signed(main_issuer)))
    check(isinstance(principal_a, Principal) and len(local._verified) == 1,
          "JWT did not remember a verified token")


def openapi_says_where_the_provider_is(c: TestClient) -> None:
    doc = c.get("/openapi.json").json()
    entry = doc["components"]["securitySchemes"].get("oidc")
    check(entry == {"type": "openIdConnect",
                    "openIdConnectUrl": main_issuer.url + "/.well-known/openid-configuration"},
          f"the OIDC security scheme is {entry}")


def no_token_reaches_a_log(records: list[logging.LogRecord], tokens: list[str]) -> None:
    text = "\n".join(r.getMessage() + repr(r.__dict__) for r in records)
    # The signature is the part that is secret-shaped and unique; an unsigned
    # token has none to look for.
    leaked = [t[:20] for t in tokens if t.split(".")[-1] and t.split(".")[-1] in text]
    check(not leaked, f"a token's signature reached a log line: {leaked}")
    check(b"OCTSENTINEL".decode() not in text, "a published symmetric key reached the log")
    check("names issuer" in text, "the refused discovery document left no warning")


# ---- cases: Keycloak ---------------------------------------------------------


def keycloak_reachable() -> bool:
    try:
        with urllib.request.urlopen(
            f"{KEYCLOAK}/realms/{REALM}/.well-known/openid-configuration", timeout=3
        ) as r:
            return r.status == 200
    except Exception:
        return False


def kc_token(**form) -> dict:
    data = urllib.parse.urlencode(form).encode()
    with urllib.request.urlopen(
        f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/token", data, timeout=10
    ) as r:
        return json.load(r)


def kc_user(username: str, password: str, client: str = "notes-web", scope: str = "") -> dict:
    form = {"grant_type": "password", "client_id": client, "username": username,
            "password": password}
    if scope:
        form["scope"] = scope
    return kc_token(**form)


class Admin:
    """Just enough of Keycloak's admin API to rotate a realm's signing key."""

    def __init__(self) -> None:
        with urllib.request.urlopen(
            f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
            urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli",
                                    "username": "admin", "password": "admin"}).encode(),
            timeout=10,
        ) as r:
            self.token = json.load(r)["access_token"]

    def call(self, method: str, path: str, body=None):
        request = urllib.request.Request(
            f"{KEYCLOAK}/admin/realms/{REALM}{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"authorization": f"Bearer {self.token}",
                     "content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as r:
            raw = r.read()
        return json.loads(raw) if raw else None

    def signing_providers(self) -> list[dict]:
        return [c for c in self.call("GET", "/components?type=org.keycloak.keys.KeyProvider")
                if c["providerId"] == "rsa-generated"]


kc = OIDC.keycloak(KEYCLOAK, REALM, audience="notes-api", name="keycloak")
kc_rotating = OIDC.keycloak(KEYCLOAK, REALM, audience="notes-api", keys_max_age=1,
                            name="keycloak_rotating")
kc_app = App(auth=kc)


@kc_app.get("/me")
async def kc_me(_: Request, who=Depends(principal)):
    return describe(who)


@kc_app.post("/notes", auth=kc.requires("notes:write", roles=("editor",)))
async def kc_create(_: Request, who=Depends(principal)):
    return {"by": who.subject}


@kc_app.get("/rotating", auth=kc_rotating)
async def kc_rotating_route(_: Request):
    return {"ok": True}


@kc_app.websocket("/ws")
async def kc_ws(_: Request, sock, who=Depends(principal)):
    await sock.send(json.dumps(describe(who)))


@kc_app.post("/publish", tool=True, auth=kc.requires(roles=("editor",)))
async def kc_publish(_: Request, who=Depends(principal)):
    """Publish the notes."""
    return {"published_by": sorted(who.roles)}


def keycloak_tokens_are_checked(c: TestClient) -> None:
    ada = kc_user("ada", "ada-password", scope="notes:read notes:write")["access_token"]
    bob = kc_user("bob", "bob-password", scope="notes:write")["access_token"]
    service = kc_token(grant_type="client_credentials", client_id="reporter",
                       client_secret="reporter-secret")["access_token"]
    other = kc_user("bob", "bob-password", client="other-app")["access_token"]
    id_token = kc_user("ada", "ada-password", scope="openid")["id_token"]

    r = c.get("/me", headers=bearer(ada))
    got = r.json() if r.status_code == 200 else r.text
    check(r.status_code == 200 and got["scheme"] == "keycloak"
          and got["scopes"] == ["notes:read", "notes:write"]
          and got["roles"] == ["editor", "viewer"], f"ada's token gave {r.status_code} {got}")
    r = c.get("/me", headers=bearer(service))
    check(r.status_code == 200 and r.json()["scopes"] == ["notes:read"]
          and r.json()["roles"] == ["viewer"], f"a service client's token gave {r.text}")
    check(c.post("/notes", headers=bearer(ada)).status_code == 200, "ada could not write")
    r = c.post("/notes", headers=bearer(bob))
    check(r.status_code == 403 and r.json()["detail"] == "requires role editor",
          f"bob, with the scope and without the role, got {r.status_code} {r.text}")
    for label, token in (("another client's token", other), ("an ID token", id_token),
                         ("a truncated token", ada[:-10])):
        r = c.get("/me", headers=bearer(token))
        check(r.status_code == 401, f"{label} answered {r.status_code}")
    r = c.get("/me")
    check(r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer",
          f"no token answered {r.status_code} {r.headers.get('www-authenticate')}")

    async def socket_sees(token):
        async with c.websocket("/ws", headers=bearer(token)) as sock:
            return json.loads(await asyncio.wait_for(sock.recv(), 5))

    got = asyncio.run(socket_sees(ada))
    check(got["roles"] == ["editor", "viewer"], f"the socket saw {got}")

    result = c.mcp("tools/call", {"name": "kc_publish", "arguments": {}}, headers=bearer(bob))
    text = "".join(part.get("text", "") for part in result["content"])
    check(result["isError"] and json.loads(text)["status"] == 403,
          f"bob's tool call without the role gave {result}")
    result = c.mcp("tools/call", {"name": "kc_publish", "arguments": {}}, headers=bearer(ada))
    check(not result["isError"], f"ada's tool call gave {result}")
    check(kc.keys.fetches == 1, f"one realm, {kc.keys.fetches} key fetches")


def keycloak_rotates_a_key(c: TestClient) -> None:
    admin = Admin()
    realm_id = admin.call("GET", "")["id"]
    before = admin.signing_providers()
    old_token = kc_user("ada", "ada-password")["access_token"]
    old_kid = jwt.get_unverified_header(old_token)["kid"]
    check(c.get("/rotating", headers=bearer(old_token)).status_code == 200,
          "the realm's first token was refused")
    fetches = kc_rotating.keys.fetches

    top = max(int(p["config"].get("priority", ["0"])[0]) for p in before)
    admin.call("POST", "/components", {
        "name": f"rotated-{int(time.time())}", "providerId": "rsa-generated",
        "providerType": "org.keycloak.keys.KeyProvider", "parentId": realm_id,
        "config": {"priority": [str(top + 100)], "keySize": ["2048"], "active": ["true"],
                   "enabled": ["true"], "algorithm": ["RS256"]},
    })
    new_token = kc_user("ada", "ada-password")["access_token"]
    new_kid = jwt.get_unverified_header(new_token)["kid"]
    check(new_kid != old_kid, "Keycloak did not sign with the new key")

    saved = _jwks.REFETCH_INTERVAL
    _jwks.REFETCH_INTERVAL = 0.5
    try:
        time.sleep(0.6)
        r = c.get("/rotating", headers=bearer(new_token))
        check(r.status_code == 200, f"a token from the rotated-in key answered {r.status_code}")
        check(kc_rotating.keys.fetches == fetches + 1,
              f"picking up one new key took {kc_rotating.keys.fetches - fetches} fetches")
        check(c.get("/rotating", headers=bearer(old_token)).status_code == 200,
              "the old key stopped working while still published")

        for provider in before:
            admin.call("DELETE", f"/components/{provider['id']}")
        deadline = time.monotonic() + 8
        status = 200
        while time.monotonic() < deadline and status == 200:
            time.sleep(0.3)
            status = c.get("/rotating", headers=bearer(old_token)).status_code
        check(status == 401, f"a token from a withdrawn Keycloak key still answered {status}")
        check(c.get("/rotating", headers=bearer(new_token)).status_code == 200,
              "the current key's token stopped working")
    finally:
        _jwks.REFETCH_INTERVAL = saved


def the_example_works(_: TestClient) -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "examples" / "oidc.py"
    spec = importlib.util.spec_from_file_location("oidc_example", path)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)

    ada = bearer(kc_user("ada", "ada-password", scope="notes:read notes:write")["access_token"])
    bob = bearer(kc_user("bob", "bob-password", scope="notes:read notes:write")["access_token"])
    reporter = bearer(kc_token(grant_type="client_credentials", client_id="reporter",
                               client_secret="reporter-secret")["access_token"])
    with TestClient(example.app) as c:
        # The lifespan fetched the keys before the first request.
        check(example.users.keys.keys is not None, "the example's lifespan did not load keys")
        check(c.get("/health").status_code == 200, "the example's health check is not public")
        check(c.get("/notes").status_code == 401, "the example's notes are not protected")
        r = c.get("/me", headers=ada)
        check(r.status_code == 200 and r.json()["name"] == "ada"
              and r.json()["roles"] == ["editor", "viewer"], f"ada's /me gave {r.text}")
        r = c.post("/notes", json={"title": "first"}, headers=ada)
        check(r.status_code == 200, f"ada's note answered {r.status_code} {r.text}")
        r = c.post("/notes", json={"title": "x"}, headers=bob)
        check(r.status_code == 403, f"bob, a viewer, wrote a note: {r.status_code}")
        r = c.get("/notes", headers=reporter)
        check(r.status_code == 200 and r.json()[0]["title"] == "first",
              f"the reporter client could not read: {r.status_code}")
        check(c.post("/notes", json={"title": "x"}, headers=reporter).status_code == 403,
              "the reporter client wrote a note")


# ---- running -----------------------------------------------------------------


def run(steps, c) -> None:
    for step in steps:
        try:
            step(c) if c is not None else step()
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            import traceback

            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}\n"
                            + traceback.format_exc())
            print(f"  {step.__name__}: ERROR")


def main() -> None:
    check_only = "--check" in sys.argv[1:]
    reachable = keycloak_reachable()
    if check_only or not reachable:
        if not reachable:
            print(f"keycloak: unavailable (nothing at {KEYCLOAK}/realms/{REALM})")
            if os.environ.get("OXBROOK_REQUIRE_KEYCLOAK"):
                print("start it with `make up`, or accept the gap with `make verify KEYCLOAK=`")
                print("\nRESULT: FAIL (keycloak is required and unreachable)")
                sys.exit(1)
        else:
            print(f"keycloak: {KEYCLOAK}")
        if check_only:
            return

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record):
            self.records.append(record)

    capture = Capture()
    log = logging.getLogger("oxbrook")
    log.addHandler(capture)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    tokens: list[str] = []
    original = OIDC.check

    async def recording(self, token):
        tokens.append(token)
        return await original(self, token)

    OIDC.check = recording

    print("issuer under test control")
    run([construction_rules], None)
    with TestClient(app) as c:
        run([issuer_tokens_are_accepted, algorithms_are_the_providers, claims_are_checked,
             one_fetch_for_every_loop, unknown_keys_are_rate_limited,
             withdrawn_keys_stop_working, an_unreachable_provider_is_503,
             bad_documents_are_refused, verified_tokens_are_remembered,
             openapi_says_where_the_provider_is], c)

    if reachable:
        print("keycloak")
        with TestClient(kc_app) as c:
            run([keycloak_tokens_are_checked, the_example_works, keycloak_rotates_a_key], c)
    else:
        print("keycloak: SKIP")

    OIDC.check = original
    no_token_reaches_a_log(capture.records, tokens)
    print("  no_token_reaches_a_log: ok")

    for issuer in (main_issuer, rotating_issuer, withdrawing_issuer, flaky_issuer, slow_issuer,
                   lying_issuer, huge_issuer):
        issuer.close()

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
