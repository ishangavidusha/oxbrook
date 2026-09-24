#!/usr/bin/env python3
"""Authentication, attacked rule by rule against a running server.

Each case is one rule the framework enforces so an app does not have to get it
right by hand:

* A credential that is present and wrong stops the search. It never falls
  through to the next scheme, and never to anonymous on an optional route:
  that is how an expired token becomes "no token".
* Requirements belong to the scheme they are written on.
* Missing or wrong is 401 with a challenge per scheme; known but not allowed
  is 403 with `insufficient_scope`.
* Authentication runs before the body is read. An anonymous client cannot make
  the server take in an upload, and a request both unauthenticated and
  malformed answers 401, not 422.
* An API key reaches the app as its digest, never itself.
* A JWT is checked for signature, algorithm family, audience, issuer and
  expiry, and a refusal says "invalid" or "expired" and nothing finer.
* No credential reaches a log line or a response body.
* Tokens in the query string are not looked for.
* Basic is refused over plain HTTP.
* The same declaration guards a WebSocket's upgrade and an MCP tool call.

Also covered, because the layer depends on them: `Depends` on a WebSocket
handler, which never worked until the principal needed it, and a header that
repeats, which two challenges need.
"""
import asyncio
import base64
import json
import logging
import socket
import sys
import threading
import time

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from oxbrook import App, BodyStream, Depends, Form, HTTPError, Request, Router, Sessions
from oxbrook.auth import (
    JWT,
    APIKey,
    Basic,
    Bearer,
    Forbidden,
    Principal,
    Scheme,
    SessionAuth,
    Unauthenticated,
    optional,
    principal,
)
from oxbrook.testing import TestClient
from pydantic import BaseModel

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


# ---- credentials ------------------------------------------------------------
# Distinctive, so a leak into a log or a body is found by searching for them.

SECRET = "hs256-secret-SENTINEL-" + "s" * 16
KEY = "sk-live-KEYSENTINEL-0123456789"
ADMIN_KEY = "sk-live-ADMINSENTINEL-0123456789"
PASSWORD = "hunter2-PASSWORDSENTINEL"
WEBHOOK_SECRET = b"whsec-SENTINEL"
SENTINELS = ("SENTINEL",)

#: Every digest `verify` was handed. The raw key must never be among them.
seen_digests: list[str] = []


async def lookup_key(digest: str):
    seen_digests.append(digest)
    if digest == APIKey.digest(KEY):
        return Principal(subject="ci", scopes={"notes:read"})
    if digest == APIKey.digest(ADMIN_KEY):
        return Principal(subject="ops", scopes={"admin"})
    return None


def check_password(username: str, password: str):
    if username == "ada" and password == PASSWORD:
        return Principal(subject="ada")
    return None


keys = APIKey(header="x-api-key", verify=lookup_key)
tokens = JWT(key=SECRET, algorithms=["HS256"], audience="notes", issuer="https://issuer.test/")
staff = Basic(verify=check_password)


def token(secret: str = SECRET, algorithm: str = "HS256", headers=None, **overrides) -> str:
    claims = {
        "sub": "u1",
        "aud": "notes",
        "iss": "https://issuer.test/",
        "exp": int(time.time()) + 300,
        "scope": "notes:read notes:write",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, secret, algorithm=algorithm, headers=headers)


def bearer(value: str) -> dict[str, str]:
    return {"authorization": f"Bearer {value}"}


def basic(user: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"authorization": f"Basic {raw}"}


class Signature(Scheme):
    """A webhook signature over the body: a scheme that has to read it."""

    name = "signature"

    async def authenticate(self, request):
        import hashlib
        import hmac

        signature = request.header("x-signature")
        if signature is None:
            return None
        body = await request.read()
        expected = hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise Unauthenticated("bad signature")
        return Principal(subject="webhook")


def sign(body: bytes) -> str:
    import hashlib
    import hmac

    return hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()


# ---- the app ----------------------------------------------------------------

sessions = Sessions(secret="session-secret-" + "x" * 32, secure=False)
users = {"7": {"name": "Grace"}}


async def load_user(value):
    return users.get(str(value))


web = SessionAuth(sessions, key="user_id", load=load_user)

#: What app middleware saw, per path: the status, and who was calling.
observed: dict[str, tuple] = {}
#: Paths router middleware ran for.
router_ran: list[str] = []
#: What app middleware got from `request.body` on a deferred route.
body_in_middleware: list[str] = []

app = App(auth=tokens | keys, docs_url=None)


@app.middleware
async def watch(request, call_next):
    if request.path == "/middleware-body":
        try:
            _ = request.body
            body_in_middleware.append("read")
        except RuntimeError as exc:
            body_in_middleware.append(str(exc))
    reply = await call_next(request)
    who = principal(request)
    observed[request.path] = (
        reply.status if reply.status is not None else getattr(reply.value, "status", 200),
        None if who is None else who.subject,
    )
    return reply


class Note(BaseModel):
    title: str


class Login(BaseModel):
    user_id: str


def describe(who):
    if who is None:
        return None
    return {"subject": who.subject, "scheme": who.scheme, "scopes": sorted(who.scopes)}


@app.get("/me")
async def me(_: Request, who=Depends(principal)):
    return describe(who)


@app.get("/health", auth=None)
async def health(_: Request, who=Depends(principal)):
    return {"ok": True, "who": describe(who)}


@app.get("/feed", auth=optional(tokens | keys))
async def feed(_: Request, who=Depends(principal)):
    return {"who": describe(who)}


@app.post("/notes", auth=tokens.requires("notes:write"))
async def create(_: Request, note: Note, who=Depends(principal)):
    return {"title": note.title, "by": who.subject}


@app.post("/raw")
async def raw(request: Request):
    return {"length": len(request.body), "head": request.body[:8].decode()}


@app.post("/form")
async def form(_: Request, login: Login = Form()):
    return {"user_id": login.user_id}


@app.put("/upload")
async def upload(_: Request, body: BodyStream):
    size = 0
    async for chunk in body:
        size += len(chunk)
    return {"size": size}


@app.post("/middleware-body")
async def middleware_body(request: Request):
    return {"length": len(request.body)}


@app.get("/own-or-key", auth=tokens.requires("admin") | keys)
async def own_or_key(_: Request, who=Depends(principal)):
    return describe(who)


@app.get("/both-admin", auth=(tokens | keys).requires("admin"))
async def both_admin(_: Request, who=Depends(principal)):
    return describe(who)


@app.get("/any-of", auth=tokens.requires(any_of=("admin", "notes:write")))
async def any_of(_: Request):
    return {}


async def is_u1(who, request):
    return who.subject == "u1"


@app.get("/checked", auth=tokens.requires(check=is_u1))
async def checked(_: Request):
    return {}


@app.get("/either-scope", auth=tokens.requires("admin") | tokens.requires("notes:read"))
async def either_scope(_: Request):
    return {}


@app.get("/staff", auth=staff)
async def staff_only(_: Request, who=Depends(principal)):
    return describe(who)


@app.get("/challenges", auth=tokens | staff | keys)
async def challenges(_: Request):
    return {}


@app.post("/webhook", auth=Signature())
async def webhook(request: Request, note: Note, who=Depends(principal)):
    return {"title": note.title, "by": who.subject, "length": len(request.body)}


@app.get("/session", auth=web)
async def session_route(_: Request, who=Depends(principal)):
    return {"subject": who.subject, "user": who.user}


@app.get("/session-optional", auth=optional(web))
async def session_optional(_: Request, who=Depends(principal)):
    return {"who": None if who is None else who.subject}


@app.post("/login", auth=None)
async def login(_: Request, body: Login, session=Depends(sessions.load)):
    session["user_id"] = body.user_id
    return {}


app.middleware(sessions.middleware)


@app.get("/record/{owner}")
async def record(_: Request, owner: str, who=Depends(principal)):
    if who.subject != owner:
        raise Forbidden()
    return {}


class Broken(Scheme):
    name = "broken"

    async def authenticate(self, request):
        if request.header("x-broken") == "raise":
            # An exception's text is the app's to choose, and goes to the log
            # as a traceback does; what must not happen is its reaching the
            # client, which is what is checked.
            raise ValueError("lookup failed: RESPONSELEAK")
        if request.header("x-broken") == "wrong-type":
            return {"subject": "x"}
        return None


@app.get("/broken", auth=Broken())
async def broken(_: Request):
    return {}


# Nearest declaration wins, through routers.
api = Router(prefix="/api", auth=keys)
admin = Router(prefix="/admin", auth=keys.requires("admin"))
public = Router(prefix="/public", auth=None)


@api.get("/key-only")
async def key_only(_: Request, who=Depends(principal)):
    return describe(who)


@api.get("/token-here", auth=tokens)
async def token_here(_: Request, who=Depends(principal)):
    return describe(who)


@api.middleware
async def mark(request, call_next):
    router_ran.append(request.path)
    return await call_next(request)


@admin.get("/panel")
async def panel(_: Request):
    return {}


@admin.get("/open", auth=None)
async def admin_open(_: Request):
    return {}


@public.get("/page")
async def page(_: Request, who=Depends(principal)):
    return {"who": describe(who)}


api.include(admin)
app.include(api)
app.include(public)


# WebSockets: the same declaration, before the handshake.
seen_by_authorize: list = []


async def only_u1(request):
    who = principal(request)
    seen_by_authorize.append(None if who is None else who.subject)
    if who is not None and who.subject == "ci":
        raise HTTPError(403, "not this one")


@app.websocket("/ws")
async def ws(_: Request, sock, who=Depends(principal)):
    await sock.send(json.dumps(describe(who)))


@app.websocket("/ws-authorized", authorize=only_u1)
async def ws_authorized(_: Request, sock, who=Depends(principal)):
    await sock.send(who.subject)


@app.websocket("/ws-admin", auth=tokens.requires("admin"))
async def ws_admin(_: Request, sock):
    await sock.send("in")


@app.websocket("/ws-open", auth=None)
async def ws_open(_: Request, sock, who=Depends(principal)):
    await sock.send(json.dumps(describe(who)))


# MCP: a tool is guarded by its route's declaration, with the agent's headers.
@app.get("/tools/admin", tool=True, auth=keys.requires("admin"))
async def admin_tool(_: Request, who=Depends(principal)):
    """An admin-only tool."""
    return {"by": who.subject}


@app.get("/tools/public", tool=True, auth=None)
async def public_tool(_: Request, who=Depends(principal)):
    """A tool anyone may call."""
    return {"who": describe(who)}


# ---- helpers ----------------------------------------------------------------


def raw_http(c: TestClient, request: bytes, *, wait: float = 3.0) -> bytes:
    """Send bytes, return what comes back within `wait` seconds."""
    sock = socket.create_connection((c.host, c.port), timeout=wait)
    try:
        sock.sendall(request)
        chunks = []
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\r\n\r\n" in b"".join(chunks):
                    break
        except TimeoutError:
            pass
        return b"".join(chunks)
    finally:
        sock.close()


def status_of(response: bytes) -> bytes:
    return response.split(b"\r\n", 1)[0]


# ---- the cases ----------------------------------------------------------------


def no_credential_is_401_with_challenges(c: TestClient) -> None:
    r = c.get("/me")
    check(r.status_code == 401, f"no credential answered {r.status_code}")
    check(r.headers.get("content-type") == "application/problem+json",
          f"a 401 was {r.headers.get('content-type')}")
    check(r.json() == {"type": "about:blank", "title": "Unauthorized", "status": 401},
          f"a 401 for no credential said {r.json()}")
    check(r.headers.get_list("www-authenticate") == ['Bearer'],
          f"challenges were {r.headers.get_list('www-authenticate')}")

    # One header per challenge, not one header holding both: many clients,
    # browsers among them, parse only the first challenge in a header.
    r = c.get("/challenges")
    check(
        r.headers.get_list("www-authenticate") == ['Bearer', 'Basic realm="api", charset="UTF-8"'],
        f"two schemes with challenges sent {r.headers.get_list('www-authenticate')}",
    )


def credentials_are_accepted(c: TestClient) -> None:
    r = c.get("/me", headers={"x-api-key": KEY})
    check(r.status_code == 200 and r.json() == {
        "subject": "ci", "scheme": "api_key", "scopes": ["notes:read"]},
        f"an API key answered {r.status_code} {r.text}")
    r = c.get("/me", headers=bearer(token()))
    check(r.status_code == 200 and r.json() == {
        "subject": "u1", "scheme": "jwt", "scopes": ["notes:read", "notes:write"]},
        f"a JWT answered {r.status_code} {r.text}")
    r = c.head("/me", headers={"x-api-key": KEY})
    check(r.status_code == 200, f"HEAD with a key answered {r.status_code}")
    r = c.head("/me")
    check(r.status_code == 401, f"HEAD without one answered {r.status_code}")


def wrong_credential_never_falls_through(c: TestClient) -> None:
    # An expired token and a valid key: the token was found first, and is wrong.
    r = c.get("/me", headers={**bearer(token(exp=int(time.time()) - 3600)), "x-api-key": KEY})
    check(r.status_code == 401, f"an expired token beside a valid key answered {r.status_code}")
    check(
        r.headers.get("www-authenticate")
        == 'Bearer error="invalid_token", error_description="token expired"',
        f"the expired token's challenge was {r.headers.get('www-authenticate')!r}",
    )
    # A wrong key beside nothing else.
    r = c.get("/me", headers={"x-api-key": "sk-live-wrong"})
    check(r.status_code == 401, f"a wrong key answered {r.status_code}")
    # On an optional route, a wrong credential is still refused. Treating it as
    # anonymous would hide from the client that its token needs renewing.
    r = c.get("/feed", headers=bearer("not-a-jwt"))
    check(r.status_code == 401, f"a wrong token on an optional route answered {r.status_code}")
    r = c.get("/feed")
    check(r.status_code == 200 and r.json() == {"who": None},
          f"no credential on an optional route answered {r.status_code} {r.text}")
    r = c.get("/feed", headers={"x-api-key": KEY})
    check(r.json()["who"]["subject"] == "ci", f"a key on an optional route gave {r.text}")


def requirements_belong_to_their_scheme(c: TestClient) -> None:
    # tokens.requires("admin") | keys: a key passes without the scope.
    r = c.get("/own-or-key", headers={"x-api-key": KEY})
    check(r.status_code == 200, f"a key on `tokens.requires(admin) | keys` got {r.status_code}")
    r = c.get("/own-or-key", headers=bearer(token()))
    check(r.status_code == 403, f"a token without admin there got {r.status_code}")
    check(r.headers.get("www-authenticate")
          == 'Bearer error="insufficient_scope", scope="admin"',
          f"the 403's challenge was {r.headers.get('www-authenticate')!r}")
    check(r.json().get("detail") == "requires scope admin", f"the 403 said {r.json()}")
    # (tokens | keys).requires("admin"): applies to both.
    r = c.get("/both-admin", headers={"x-api-key": KEY})
    check(r.status_code == 403, f"a key without admin on `(a | b).requires` got {r.status_code}")
    check("www-authenticate" not in r.headers, "an API key's 403 carried a Bearer challenge")
    r = c.get("/both-admin", headers={"x-api-key": ADMIN_KEY})
    check(r.status_code == 200, f"a key with admin got {r.status_code}")
    r = c.get("/both-admin", headers=bearer(token(scope="admin")))
    check(r.status_code == 200, f"a token with admin got {r.status_code}")

    r = c.get("/any-of", headers=bearer(token(scope="notes:write")))
    check(r.status_code == 200, f"any_of with one of the scopes got {r.status_code}")
    r = c.get("/any-of", headers=bearer(token(scope="notes:read")))
    check(r.status_code == 403, f"any_of with neither got {r.status_code}")
    r = c.get("/checked", headers=bearer(token()))
    check(r.status_code == 200, f"a passing check= got {r.status_code}")
    r = c.get("/checked", headers=bearer(token(sub="u2")))
    check(r.status_code == 403, f"a failing check= got {r.status_code}")
    # `a.requires(x) | a.requires(y)` is one credential that may satisfy
    # either, not a first try that fails on x and stops.
    r = c.get("/either-scope", headers=bearer(token(scope="notes:read")))
    check(r.status_code == 200, f"the second of two requirement sets got {r.status_code}")

    # A rule about a particular record stays in the handler.
    r = c.get("/record/u1", headers=bearer(token()))
    check(r.status_code == 200, f"the owner got {r.status_code}")
    r = c.get("/record/u2", headers=bearer(token()))
    check(r.status_code == 403 and r.json()["title"] == "Forbidden",
          f"Forbidden() from a handler answered {r.status_code} {r.text}")


def nearest_declaration_wins(c: TestClient) -> None:
    for path, headers, want, what in (
        ("/api/key-only", {"x-api-key": KEY}, 200, "a router's auth with its credential"),
        ("/api/key-only", bearer(token()), 401, "a router's auth with the app's credential"),
        ("/api/token-here", bearer(token()), 200, "a route's auth over its router's"),
        ("/api/token-here", {"x-api-key": KEY}, 401, "the router's credential on that route"),
        ("/api/admin/panel", {"x-api-key": KEY}, 403, "a nested router without its scope"),
        ("/api/admin/panel", {"x-api-key": ADMIN_KEY}, 200, "a nested router with it"),
        ("/api/admin/open", {}, 200, "auth=None inside a protected router"),
        ("/public/page", {}, 200, "a router declared auth=None"),
        ("/health", {}, 200, "a route declared auth=None"),
    ):
        r = c.get(path, headers=headers)
        check(r.status_code == want, f"{what}: {path} answered {r.status_code}, expected {want}")
    # A public route has no principal, even when the request carries a
    # credential: nothing was asked of it.
    r = c.get("/health", headers={"x-api-key": KEY})
    check(r.json()["who"] is None, f"a public route found a principal: {r.text}")


def auth_runs_before_the_body(c: TestClient) -> None:
    # Headers and the first kilobyte of a megabyte, then nothing. Collected
    # first, this waits for the rest of the body and never answers.
    started = time.perf_counter()
    answer = raw_http(
        c,
        b"POST /notes HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        b"Content-Length: 1000000\r\n\r\n" + b"{" + b" " * 1023,
    )
    elapsed = time.perf_counter() - started
    check(b" 401 " in status_of(answer),
          f"an unauthenticated upload was not refused before it arrived: {status_of(answer)!r}")
    check(elapsed < 2.0, f"the refusal took {elapsed:.2f}s, as if the body were awaited")

    # A client waiting on `100 Continue` is answered without being asked for
    # the body at all.
    answer = raw_http(
        c,
        b"POST /notes HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        b"Content-Length: 1000000\r\nExpect: 100-continue\r\n\r\n",
    )
    check(b" 401 " in status_of(answer),
          f"an unauthenticated upload expecting 100 Continue got {status_of(answer)!r}")

    # Unauthenticated and malformed is 401: who, before what.
    r = c.post("/notes", content=b"{not json", headers={"content-type": "application/json"})
    check(r.status_code == 401, f"an unauthenticated, malformed body answered {r.status_code}")

    # The refusal reads away what was sent, so the connection survives it.
    body = b"a" * 200_000
    sock = socket.create_connection((c.host, c.port), timeout=10)
    try:
        sock.sendall(b"POST /raw HTTP/1.1\r\nHost: x\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\n\r\n" + body)
        first = sock.recv(4096)
        check(b" 401 " in status_of(first), f"the refusal was {status_of(first)!r}")
        try:
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            second = sock.recv(4096)
        except OSError as exc:
            second = f"<{type(exc).__name__}: {exc}>".encode()
        check(b" 200 " in status_of(second),
              f"the connection did not survive a 401 with a body unread: {second[:60]!r}")
    finally:
        sock.close()


def authenticated_bodies_arrive_whole(c: TestClient) -> None:
    key = {"x-api-key": KEY}
    r = c.post("/notes", json={"title": "hello"}, headers=bearer(token()))
    check(r.status_code == 200 and r.json() == {"title": "hello", "by": "u1"},
          f"an authenticated JSON body answered {r.status_code} {r.text}")
    r = c.post("/notes", json={"title": 3}, headers=bearer(token()))
    check(r.status_code == 422, f"an authenticated invalid body answered {r.status_code}")
    big = b"B" * (3 * 1024 * 1024)
    r = c.post("/raw", content=big, headers=key)
    check(r.status_code == 200 and r.json() == {"length": len(big), "head": "BBBBBBBB"},
          f"a 3 MB authenticated body answered {r.status_code} {r.text[:80]}")
    r = c.post("/raw", content=iter([b"chunk-", b"ed"]), headers=key)
    check(r.status_code == 200 and r.json() == {"length": 8, "head": "chunk-ed"},
          f"a chunked authenticated body answered {r.status_code} {r.text}")
    r = c.post("/form", data={"user_id": "7"}, headers=key)
    check(r.status_code == 200 and r.json() == {"user_id": "7"},
          f"an authenticated form answered {r.status_code} {r.text}")
    r = c.put("/upload", content=b"x" * 500_000, headers=key)
    check(r.status_code == 200 and r.json() == {"size": 500_000},
          f"an authenticated streamed upload answered {r.status_code} {r.text}")
    r = c.put("/upload", content=b"x" * 10)
    check(r.status_code == 401, f"an unauthenticated streamed upload answered {r.status_code}")
    r = c.post("/raw", headers=key)
    check(r.status_code == 200 and r.json()["length"] == 0,
          f"an authenticated empty POST answered {r.status_code} {r.text}")

    with TestClient(app, max_body=1000) as small:
        r = small.post("/raw", content=b"x" * 5000, headers=key)
        check(r.status_code == 413, f"an authenticated body over max_body answered {r.status_code}")
        r = small.post("/raw", content=b"x" * 5000)
        check(r.status_code == 401,
              f"an unauthenticated body over max_body answered {r.status_code}: who comes first")

    # Many at once, over every worker loop, each body its own.
    errors: list[str] = []
    local = threading.local()

    def one(i: int) -> None:
        client = getattr(local, "client", None)
        if client is None:
            client = local.client = httpx.Client(base_url=c.base_url, timeout=10)
        payload = f"{i:06d}".encode() * 1000
        r = client.post("/raw", content=payload, headers=key)
        if r.status_code != 200 or r.json() != {"length": len(payload),
                                                 "head": payload[:8].decode()}:
            errors.append(f"{i}: {r.status_code} {r.text[:60]}")

    threads = [threading.Thread(target=lambda n=n: [one(n * 25 + j) for j in range(25)])
               for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(not errors, f"concurrent authenticated bodies went wrong: {errors[:3]}")


def middleware_runs_around_it(c: TestClient) -> None:
    router_ran.clear()
    r = c.get("/api/key-only")
    check(r.status_code == 401, f"expected a refusal, got {r.status_code}")
    check(observed.get("/api/key-only") == (401, None),
          f"app middleware saw {observed.get('/api/key-only')} for a refusal")
    check("/api/key-only" not in router_ran, "router middleware ran for a refused caller")
    c.get("/api/key-only", headers={"x-api-key": KEY})
    check(observed.get("/api/key-only") == (200, "ci"),
          f"app middleware saw {observed.get('/api/key-only')} after an accepted call")
    check("/api/key-only" in router_ran, "router middleware did not run for an accepted caller")

    # The body is read after authentication, so middleware outside it finds
    # it unread, and is told so rather than handed empty bytes.
    body_in_middleware.clear()
    r = c.post("/middleware-body", content=b"abc", headers={"x-api-key": KEY})
    check(r.status_code == 200 and r.json() == {"length": 3},
          f"the handler after it got {r.status_code} {r.text}")
    check(body_in_middleware and "await request.read()" in body_in_middleware[0],
          f"request.body in outer middleware gave {body_in_middleware}")


def a_scheme_may_read_the_body(c: TestClient) -> None:
    body = json.dumps({"title": "paid"}).encode()
    r = c.post("/webhook", content=body, headers={"x-signature": sign(body),
                                                  "content-type": "application/json"})
    check(r.status_code == 200 and r.json() == {"title": "paid", "by": "webhook",
                                                "length": len(body)},
          f"a signature scheme reading the body answered {r.status_code} {r.text}")
    r = c.post("/webhook", content=body, headers={"x-signature": sign(b"other")})
    check(r.status_code == 401 and r.json().get("detail") == "bad signature",
          f"a bad signature answered {r.status_code} {r.text}")


def api_keys_are_looked_up_by_digest(c: TestClient) -> None:
    seen_digests.clear()
    c.get("/me", headers={"x-api-key": KEY})
    check(seen_digests == [APIKey.digest(KEY)], f"verify was handed {seen_digests}")
    check(KEY not in seen_digests, "verify was handed the key itself")
    check(len(APIKey.generate()) >= 43, "APIKey.generate() made a short key")
    check(APIKey.generate("ox_").startswith("ox_"), "APIKey.generate() lost its prefix")


def jwt_rules(c: TestClient) -> None:
    refusals: dict[str, httpx.Response] = {}
    other_secret = "another-secret-" + "y" * 32
    for label, value in (
        ("wrong signature", token(secret=other_secret)),
        ("tampered claims", token()[:-4] + ("AAAA" if not token().endswith("AAAA") else "BBBB")),
        ("wrong audience", token(aud="billing")),
        ("wrong issuer", token(iss="https://evil.test/")),
        ("no expiry", token(exp=None)),
        ("no subject", token(sub=None)),
        ("not yet valid", token(nbf=int(time.time()) + 3600)),
        ("unsigned", jwt.encode({"sub": "u1", "aud": "notes", "iss": "https://issuer.test/",
                                 "exp": int(time.time()) + 300}, None, algorithm="none")),
        ("not a JWT", "abc.def.ghi"),
        ("empty", None),
    ):
        # httpx will not send a header ending in a space, so the empty token is
        # the scheme word alone, which is what a client that lost it sends.
        r = c.get("/me", headers=bearer(value) if value is not None else
                  {"authorization": "Bearer"})
        refusals[label] = r
        check(r.status_code == 401, f"a JWT with {label} answered {r.status_code}")

    # Which check failed is for the log. Every refusal reads the same, so a
    # probe learns nothing about how to forge the next one.
    shapes = {(r.text, r.headers.get("www-authenticate")) for r in refusals.values()}
    check(len(shapes) == 1, f"invalid tokens were told apart: {sorted(shapes)}")
    check(shapes == {('{"type":"about:blank","title":"Unauthorized","status":401,'
                      '"detail":"token invalid"}',
                      'Bearer error="invalid_token", error_description="token invalid"')},
          f"an invalid token's refusal was {shapes}")

    # Clock skew is tolerated, by the leeway.
    r = c.get("/me", headers=bearer(token(exp=int(time.time()) - 10)))
    check(r.status_code == 200, f"a token ten seconds past exp answered {r.status_code}")

    # Public keys: RS256, with the private half signing.
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    rs = JWT(key=public, algorithms=["RS256"], audience="notes")
    signed = jwt.encode({"sub": "r1", "aud": "notes", "exp": int(time.time()) + 60}, pem,
                        algorithm="RS256")
    other = App(auth=rs, openapi_url=None, docs_url=None, mcp_url=None)

    @other.get("/me")
    async def rs_me(_: Request, who=Depends(principal)):
        return describe(who)

    with TestClient(other) as oc:
        r = oc.get("/me", headers=bearer(signed))
        check(r.status_code == 200 and r.json()["subject"] == "r1",
              f"an RS256 token answered {r.status_code} {r.text}")
        # The public key used as an HMAC secret: the classic confusion attack.
        # Built by hand, since PyJWT refuses to sign it.
        import hashlib
        import hmac

        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=")
        payload = base64.urlsafe_b64encode(json.dumps(
            {"sub": "r1", "aud": "notes", "exp": int(time.time()) + 60}).encode()).rstrip(b"=")
        signature = base64.urlsafe_b64encode(
            hmac.new(public, header + b"." + payload, hashlib.sha256).digest()).rstrip(b"=")
        forged = (header + b"." + payload + b"." + signature).decode()
        r = oc.get("/me", headers=bearer(forged))
        check(r.status_code == 401,
              f"a token signed with the public key as an HMAC secret answered {r.status_code}")

    # What cannot be declared.
    for label, build in (
        ("the none algorithm", lambda: JWT(key=SECRET, algorithms=["none"], audience="a")),
        ("mixed families", lambda: JWT(key=SECRET, algorithms=["HS256", "RS256"], audience="a")),
        ("a PEM key as an HMAC secret", lambda: JWT(key=public, algorithms=["HS256"],
                                                    audience="a")),
        ("a short secret", lambda: JWT(key="short", algorithms=["HS256"], audience="a")),
        ("algorithms as a string", lambda: JWT(key=SECRET, algorithms="HS256", audience="a")),
        ("no algorithms", lambda: JWT(key=SECRET, algorithms=[], audience="a")),
    ):
        try:
            build()
            failures.append(f"JWT accepted {label}")
        except (TypeError, ValueError):
            pass
    try:
        JWT(key=SECRET, algorithms=["HS256"])  # type: ignore[call-arg]
        failures.append("JWT accepted no audience without it being written out")
    except TypeError:
        pass


def query_tokens_are_not_looked_for(c: TestClient) -> None:
    for query in (f"access_token={token()}", f"api_key={KEY}", f"x-api-key={KEY}"):
        r = c.get(f"/me?{query}")
        check(r.status_code == 401, f"a credential in the query string answered {r.status_code}")


def basic_needs_tls(c: TestClient) -> None:
    good = basic("ada", PASSWORD)
    r = c.get("/staff", headers=good)
    check(r.status_code == 200 and r.json()["subject"] == "ada",
          f"Basic on loopback answered {r.status_code} {r.text}")
    r = c.get("/staff", headers={**good, "host": "api.example.com"})
    check(r.status_code == 401 and "plain HTTP" in r.json().get("detail", ""),
          f"Basic over plain HTTP from afar answered {r.status_code} {r.text}")
    r = c.get("/staff", headers={**good, "host": "api.example.com",
                                 "x-forwarded-proto": "https"})
    check(r.status_code == 200, f"Basic behind a TLS proxy answered {r.status_code}")
    r = c.get("/staff", headers=basic("ada", "wrong"))
    check(r.status_code == 401 and r.headers.get("www-authenticate")
          == 'Basic realm="api", charset="UTF-8"',
          f"a wrong password answered {r.status_code} {r.headers.get('www-authenticate')!r}")
    for bad in ("Basic !!!notbase64", "Basic " + base64.b64encode(b"no-colon").decode()):
        r = c.get("/staff", headers={"authorization": bad})
        check(r.status_code == 401, f"malformed Basic {bad!r} answered {r.status_code}")

    lax = Basic(verify=check_password, allow_http=True)
    other = App(auth=lax, openapi_url=None, docs_url=None, mcp_url=None)

    @other.get("/x")
    async def x(_: Request):
        return {}

    with TestClient(other) as oc:
        r = oc.get("/x", headers={**good, "host": "api.example.com"})
        check(r.status_code == 200, f"Basic(allow_http=True) answered {r.status_code}")


def sessions_authenticate(c: TestClient) -> None:
    with httpx.Client(base_url=c.base_url) as browser:
        r = browser.get("/session")
        check(r.status_code == 401, f"no session answered {r.status_code}")
        browser.post("/login", json={"user_id": "7"})
        r = browser.get("/session")
        check(r.status_code == 200 and r.json() == {"subject": "7", "user": {"name": "Grace"}},
              f"a logged-in session answered {r.status_code} {r.text}")
    # A stale or tampered cookie is no session: the person holding it cannot
    # remove it, and refusing it would lock them out of anonymous pages.
    stale = {"cookie": "oxbrook_session=eyJ0YW1wZXJlZCI6dHJ1ZX0.bad"}
    r = c.get("/session-optional", headers=stale)
    check(r.status_code == 200 and r.json() == {"who": None},
          f"a tampered session on an optional route answered {r.status_code} {r.text}")
    r = c.get("/session", headers=stale)
    check(r.status_code == 401, f"a tampered session on a protected route answered {r.status_code}")
    gone = {"cookie": f"oxbrook_session={sessions.encode({'user_id': '99'})}"}
    r = c.get("/session-optional", headers=gone)
    check(r.json() == {"who": None}, f"a session for a deleted user gave {r.text}")


def failures_are_errors_not_leaks(c: TestClient) -> None:
    r = c.get("/broken", headers={"x-broken": "raise"})
    check(r.status_code == 500 and "RESPONSELEAK" not in r.text,
          f"a scheme that raised answered {r.status_code} {r.text}")
    r = c.get("/broken", headers={"x-broken": "wrong-type"})
    check(r.status_code == 500, f"a scheme returning a dict answered {r.status_code}")


def websockets_are_guarded(c: TestClient) -> None:
    async def receive(path, headers=None):
        async with c.websocket(path, headers=headers) as sock:
            return await asyncio.wait_for(sock.recv(), 5)

    got = asyncio.run(receive("/ws", {"x-api-key": KEY}))
    check(json.loads(got) == {"subject": "ci", "scheme": "api_key", "scopes": ["notes:read"]},
          f"an authenticated socket's handler saw {got}")
    got = asyncio.run(receive("/ws-open", {"x-api-key": KEY}))
    check(json.loads(got) is None, f"a public socket found a principal: {got}")

    upgrade = {
        "connection": "upgrade", "upgrade": "websocket", "sec-websocket-version": "13",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
    }
    # Refused before the handshake: a real 401 with its challenge, not a
    # socket that opens and closes.
    r = httpx.get(c.base_url + "/ws", headers=upgrade)
    check(r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer",
          f"an unauthenticated upgrade answered {r.status_code} "
          f"{r.headers.get('www-authenticate')!r}")
    check(r.headers.get("content-type") == "application/problem+json",
          f"the refused upgrade was {r.headers.get('content-type')}")
    r = httpx.get(c.base_url + "/ws-admin", headers={**upgrade, **bearer(token())})
    check(r.status_code == 403 and "insufficient_scope" in r.headers.get("www-authenticate", ""),
          f"an upgrade without the scope answered {r.status_code}")
    # A client naming a hand-off key of its own gets nothing for it.
    r = httpx.get(c.base_url + "/ws", headers={**upgrade, "x-oxbrook-handoff": "guess"})
    check(r.status_code == 401, f"a forged hand-off key answered {r.status_code}")
    # And the real one never reaches a client.
    with socket.create_connection((c.host, c.port), timeout=5) as sock:
        sock.sendall(
            b"GET /ws HTTP/1.1\r\nHost: x\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
            b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"x-api-key: " + KEY.encode() + b"\r\n\r\n"
        )
        head = sock.recv(4096)
    check(b" 101 " in status_of(head), f"an authenticated upgrade answered {status_of(head)!r}")
    check(b"handoff" not in head.lower(), f"the hand-off key reached the client: {head!r}")

    # authorize= runs after auth=, and sees who it found.
    seen_by_authorize.clear()
    got = asyncio.run(receive("/ws-authorized", bearer(token())))
    check(got == "u1", f"a socket past auth and authorize got {got!r}")
    r = httpx.get(c.base_url + "/ws-authorized", headers={**upgrade, "x-api-key": KEY})
    check(r.status_code == 403, f"authorize refusing after auth answered {r.status_code}")
    check(seen_by_authorize == ["u1", "ci"], f"authorize saw {seen_by_authorize}")
    r = httpx.get(c.base_url + "/ws-authorized", headers=upgrade)
    check(r.status_code == 401, f"no credential before authorize answered {r.status_code}")
    check(seen_by_authorize == ["u1", "ci"], "authorize ran for a caller auth refused")


def tool_calls_are_guarded(c: TestClient) -> None:
    # `/mcp` itself follows the app's declaration.
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    check(r.status_code == 401, f"an unauthenticated agent reached /mcp: {r.status_code}")

    def call(name, headers):
        return c.mcp("tools/call", {"name": name, "arguments": {}}, headers=headers)

    result = call("admin_tool", {"x-api-key": KEY})
    text = "".join(part.get("text", "") for part in result["content"])
    check(result["isError"] and json.loads(text)["status"] == 403,
          f"an admin tool called without the scope gave {result}")
    result = call("admin_tool", bearer(token(scope="admin")))
    text = "".join(part.get("text", "") for part in result["content"])
    check(result["isError"] and json.loads(text)["status"] == 401,
          f"an admin tool called with the wrong kind of credential gave {result}")
    result = call("admin_tool", {"x-api-key": ADMIN_KEY})
    check(not result["isError"] and "ops" in result["content"][0]["text"],
          f"an admin tool called with the scope gave {result}")
    # A public tool's principal is its route's answer — none — not the one
    # `/mcp` found for the request that carried it.
    result = call("public_tool", {"x-api-key": ADMIN_KEY})
    check(json.loads(result["content"][0]["text"]) == {"who": None},
          f"a public tool saw {result['content'][0]['text']}")
    check(observed.get("/mcp", (None, None))[1] == "ops",
          f"the /mcp request's own principal was overwritten: {observed.get('/mcp')}")


def openapi_describes_it(c: TestClient) -> None:
    from openapi_spec_validator import validate

    doc = c.get("/openapi.json").json()
    try:
        validate(doc)
    except Exception as exc:
        failures.append(f"the OpenAPI document is invalid: {exc}")
    schemes = doc.get("components", {}).get("securitySchemes", {})
    check(schemes.get("jwt") == {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
          f"jwt was described as {schemes.get('jwt')}")
    check(schemes.get("api_key") == {"type": "apiKey", "in": "header", "name": "x-api-key"},
          f"api_key was described as {schemes.get('api_key')}")
    check(schemes.get("basic") == {"type": "http", "scheme": "basic"},
          f"basic was described as {schemes.get('basic')}")
    check(schemes.get("session") == {"type": "apiKey", "in": "cookie", "name": "oxbrook_session"},
          f"session was described as {schemes.get('session')}")
    check("signature" not in schemes, "a scheme with no openapi() was described")
    paths = doc["paths"]
    for path, method, want in (
        ("/me", "get", [{"jwt": []}, {"api_key": []}]),
        ("/health", "get", []),
        ("/feed", "get", [{"jwt": []}, {"api_key": []}, {}]),
        ("/notes", "post", [{"jwt": ["notes:write"]}]),
        ("/own-or-key", "get", [{"jwt": ["admin"]}, {"api_key": []}]),
        ("/both-admin", "get", [{"jwt": ["admin"]}, {"api_key": ["admin"]}]),
        ("/any-of", "get", [{"jwt": ["admin"]}, {"jwt": ["notes:write"]}]),
        ("/api/admin/panel", "get", [{"api_key": ["admin"]}]),
    ):
        got = paths.get(path, {}).get(method, {}).get("security")
        check(got == want, f"{path} security was {got}, expected {want}")
    check("401" in paths["/me"]["get"]["responses"], "a protected route documents no 401")
    check("403" in paths["/notes"]["post"]["responses"], "a route with a scope documents no 403")
    check("403" not in paths["/me"]["get"]["responses"], "a route with no requirement has a 403")
    check("401" not in paths["/health"]["get"]["responses"], "a public route documents a 401")

    # A route in an app with no default says nothing, rather than `[]`.
    plain = App(openapi_url=None, docs_url=None, mcp_url=None)

    @plain.get("/x")
    async def x(_: Request):
        return {}

    check("security" not in plain.openapi()["paths"]["/x"]["get"],
          "a route in an app without auth declared security")

    # Two different schemes under one name would make the document lie.
    clash = App(openapi_url=None, docs_url=None, mcp_url=None)
    other_keys = APIKey(header="x-other", verify=lookup_key)

    @clash.get("/a", auth=keys)
    async def a(_: Request):
        return {}

    @clash.get("/b", auth=other_keys)
    async def b(_: Request):
        return {}

    try:
        clash.openapi()
        failures.append("two schemes named api_key were both described")
    except ValueError as exc:
        check("name=" in str(exc), f"the clash did not name the fix: {exc}")


def the_example_works() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "examples" / "auth.py"
    spec = importlib.util.spec_from_file_location("auth_example", path)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)

    with TestClient(example.app) as c:
        check(c.get("/health").status_code == 200, "the example's health check is not public")
        check(c.get("/notes").status_code == 401, "the example's notes are not protected")
        r = c.post("/token", auth=("bob", "battery-staple"))
        check(r.status_code == 200, f"the example's token exchange answered {r.status_code}")
        bob = bearer(r.json()["access_token"])
        r = c.post("/token", auth=("ada", "correct-horse"))
        ada = bearer(r.json()["access_token"])
        check(c.post("/token", auth=("ada", "wrong")).status_code == 401,
              "the example issued a token for a wrong password")
        r = c.post("/notes", json={"title": "first"}, headers=ada)
        check(r.status_code == 200 and r.json()["owner"] == "ada",
              f"ada's note answered {r.status_code} {r.text}")
        check(c.post("/notes", json={"title": "x"}, headers=bob).status_code == 403,
              "bob wrote without notes:write")
        check(c.get("/notes", headers={"x-api-key": "ox_demo_key"}).json()[0]["title"] == "first",
              "the example's API key did not read the notes")
        check(c.post("/notes", json={"title": "x"},
                     headers={"x-api-key": "ox_demo_key"}).status_code == 401,
              "a key wrote where only a token may")
        check(c.delete("/notes/1", headers=bob).status_code == 403,
              "bob deleted ada's note")
        body = json.dumps({"note_id": 1, "amount": 500}).encode()
        signature = hmac_hex(example.WEBHOOK_SECRET, body)
        r = c.post("/webhooks/payments", content=body,
                   headers={"x-signature": signature, "content-type": "application/json"})
        check(r.status_code == 200, f"a signed webhook answered {r.status_code} {r.text}")
        r = c.post("/webhooks/payments", content=body, headers={"x-signature": "0" * 64})
        check(r.status_code == 401, f"an unsigned webhook answered {r.status_code}")
        check(c.delete("/notes/1", headers=ada).status_code == 204, "ada could not delete")


def hmac_hex(secret: bytes, body: bytes) -> str:
    import hashlib
    import hmac

    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def declarations_are_checked_where_written() -> None:
    class Sync:
        def authenticate(self, request):
            return None

    for label, build in (
        ("a string", lambda: App(auth="bearer")),
        ("a sync authenticate", lambda: App(auth=Sync())),
        ("optional(optional())", lambda: optional(optional(keys))),
        ("optional() on one side of |", lambda: optional(keys) | tokens),
        ("requires() with nothing", lambda: keys.requires()),
        ("requires() with a str any_of", lambda: keys.requires(any_of="admin")),
        ("a route's auth=", lambda: App().get("/x", auth=42)),
        ("a router's auth=", lambda: Router(auth=object())),
        ("scopes as one string", lambda: Principal(subject="x", scopes="admin")),
        ("a numeric subject", lambda: Principal(subject=7)),
        ("APIKey with both places", lambda: APIKey(header="a", cookie="b", verify=lookup_key)),
        ("Bearer with no verify", lambda: Bearer()),
    ):
        try:
            build()
            failures.append(f"accepted {label}")
        except TypeError:
            pass
    # A duck-typed scheme is fine, and combines from either side.
    class Custom:
        name = "custom"

        async def authenticate(self, request):
            return None

    App(auth=keys | Custom())
    App(auth=Custom() | keys)
    App(auth=optional(Custom()))


def no_credential_reaches_a_log(c: TestClient, records: list[str]) -> None:
    # Everything logged across the whole run, formatted with every extra.
    leaked = [r for r in records if any(s in r for s in SENTINELS)]
    check(not leaked, f"a credential reached the log: {leaked[:2]}")


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = self.format(record) + repr(record.__dict__)
        if record.exc_info:
            text += logging.Formatter().formatException(record.exc_info)
        self.records.append(text)


def main() -> None:
    # The server's own logs, at every level. Not the root logger: httpx logs
    # each URL this suite requests, including the ones that put a key in the
    # query string on purpose, and that is the client's log, not the server's.
    capture = Capture()
    server_log = logging.getLogger("oxbrook")
    server_log.addHandler(capture)
    server_log.setLevel(logging.DEBUG)
    server_log.propagate = False

    for step in (declarations_are_checked_where_written, the_example_works):
        try:
            step()
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            failures.append(f"{step.__name__} raised {exc!r}")

    with TestClient(app) as c:
        responses: list[str] = []
        for step in (
            no_credential_is_401_with_challenges,
            credentials_are_accepted,
            wrong_credential_never_falls_through,
            requirements_belong_to_their_scheme,
            nearest_declaration_wins,
            auth_runs_before_the_body,
            authenticated_bodies_arrive_whole,
            middleware_runs_around_it,
            a_scheme_may_read_the_body,
            api_keys_are_looked_up_by_digest,
            jwt_rules,
            query_tokens_are_not_looked_for,
            basic_needs_tls,
            sessions_authenticate,
            failures_are_errors_not_leaks,
            websockets_are_guarded,
            tool_calls_are_guarded,
            openapi_describes_it,
        ):
            try:
                step(c)
                print(f"  {step.__name__}: ok")
            except Exception as exc:
                import traceback

                failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}\n"
                                + traceback.format_exc())
                print(f"  {step.__name__}: ERROR")
        # Response bodies from the refusals above, checked for credentials.
        for path, headers in (("/me", {"x-api-key": "sk-live-wrong-SENTINEL"}),
                              ("/me", bearer("SENTINEL.SENTINEL.SENTINEL")),
                              ("/staff", basic("ada", "wrong-SENTINEL"))):
            r = c.get(path, headers=headers)
            responses.append(r.text + repr(dict(r.headers)))
        check(not any("SENTINEL" in text for text in responses),
              f"a credential came back in a response: {responses}")
    no_credential_reaches_a_log(c, capture.records)
    print("  no_credential_reaches_a_log: ok")

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
