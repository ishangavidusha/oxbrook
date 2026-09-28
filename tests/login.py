#!/usr/bin/env python3
"""Logging people in: OAuthLogin, against a scripted provider and Keycloak.

The first half drives the whole redirect dance — app, provider, callback —
with a small "browser" that keeps cookies per host, against a provider this
suite serves itself, because the attacks need one that misbehaves on request:

* `state` ties a callback to the browser that started the login: a callback
  with none, an unknown one, a replayed one, another provider's, one from a
  browser that never started a login, or one too old is refused.
* PKCE: a code stolen from one person's login and played into another's is
  refused by the provider, since the verifier is the other login's.
* The ID token must verify, be for this client, and carry this login's
  `nonce`; an `iss` on the callback must name the provider (the mix-up
  defence), and must be there when the provider promises it.
* `next` is followed only to a path on this site.
* The session after a login holds the login and nothing that was in it
  before; logout empties it and refuses a request forged by another site.
* GitHub (no ID token: the user from its API) and Microsoft's multi-tenant
  endpoints (an issuer per tenant, checked against `tid`), both scripted.
* No code, token, verifier, state or client secret reaches a log.

The second half logs Ada in through Keycloak's real login form, and shows that
Keycloak itself refuses a code played into another login.

With OXBROOK_REQUIRE_KEYCLOAK set an unreachable Keycloak is a failure rather
than a SKIP of that half; `make verify` and CI both set it.
"""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from oxbrook import App, Depends, Request, Sessions
from oxbrook.auth import Login, OAuthLogin, Principal, SessionAuth, principal
from oxbrook.testing import TestClient, free_port

KEYCLOAK = os.environ.get("OXBROOK_TEST_KEYCLOAK", "http://localhost:8199")
REALM = "oxbrook"
SECRET = "login-suite-secret-" + "x" * 16

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ---- a provider that does as it is told ----------------------------------------

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {**RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "kid": "a", "alg": "RS256",
       "use": "sig"}
TENANT_A = str(uuid.UUID(int=0xA))
TENANT_B = str(uuid.UUID(int=0xB))
CONSUMERS = "9188040d-6c67-4c5b-b112-36a304b66dad"


class Provider:
    """An OpenID provider, a GitHub and a Microsoft, from one loopback server.

    Everything it hands out is recorded, so the suite can look for it in the
    logs afterwards, and everything it checks can be told to go wrong.
    """

    def __init__(self) -> None:
        self.reset()
        self.issued: list[str] = []  # every code, token and secret-shaped value
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=None, location=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                if location:
                    self.send_header("location", location)
                if body is not None:
                    self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path, _, query = self.path.partition("?")
                params = dict(urllib.parse.parse_qsl(query))
                if path == "/.well-known/openid-configuration":
                    self.reply(200, provider.discovery(""))
                elif path in ("/ms/common/v2.0/.well-known/openid-configuration",
                              "/ms/organizations/v2.0/.well-known/openid-configuration"):
                    self.reply(200, provider.discovery("/ms/" + path.split("/")[2]))
                elif path == "/jwks":
                    self.reply(200, {"keys": [JWK]})
                elif path in ("/authorize", "/ms/common/oauth2/v2.0/authorize",
                              "/ms/organizations/oauth2/v2.0/authorize",
                              "/gh/login/oauth/authorize"):
                    self.reply(302, location=provider.authorize(path, params))
                elif path == "/gh/api/user":
                    if self.headers.get("authorization") not in provider.github_tokens:
                        return self.reply(401, {"message": "Bad credentials"})
                    self.reply(provider.github_user_status, provider.github_user)
                elif path == "/gh/api/user/emails":
                    if self.headers.get("authorization") not in provider.github_tokens:
                        return self.reply(401, {"message": "Bad credentials"})
                    self.reply(200, provider.github_emails)
                else:
                    self.reply(404, {})

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
                status, body = provider.token(self.path, form, self.headers.get("authorization"))
                self.reply(status, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self) -> None:
        self.user = {"sub": "u-1", "email": "ada@example.com", "email_verified": True,
                     "name": "Ada Lovelace"}
        self.promise_iss = True
        self.send_iss = True
        self.iss_value: str | None = None
        self.error: str | None = None
        self.id_overrides: dict = {}
        self.no_id_token = False
        self.token_status: int | None = None
        self.codes: dict[str, dict] = {}
        self.authorize_seen: list[dict] = []
        self.token_seen: list[tuple[dict, str | None]] = []
        self.ms_tid = TENANT_A
        self.ms_iss_tid: str | None = None
        self.github_user = {"id": 42, "login": "ada-l", "name": "Ada Lovelace"}
        self.github_user_status = 200
        self.github_emails = [
            {"email": "old@example.com", "primary": False, "verified": True},
            {"email": "ada@example.com", "primary": True, "verified": True},
        ]
        self.github_tokens: set[str] = set()
        self.github_error_in_200 = False

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def discovery(self, base: str) -> dict:
        issuer = self.url if not base else f"{self.url}/ms/{{tenantid}}/v2.0"
        document = {
            "issuer": issuer,
            "authorization_endpoint": (f"{self.url}{base}/oauth2/v2.0/authorize" if base
                                       else f"{self.url}/authorize"),
            "token_endpoint": (f"{self.url}{base}/oauth2/v2.0/token" if base
                               else f"{self.url}/token"),
            "jwks_uri": f"{self.url}/jwks",
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        if not base and self.promise_iss:
            document["authorization_response_iss_parameter_supported"] = True
        return document

    def authorize(self, path: str, params: dict) -> str:
        self.authorize_seen.append(dict(params))
        back = {"state": params.get("state", "")}
        if self.error:
            back["error"] = self.error
        else:
            code = secrets.token_urlsafe(16)
            self.issued.append(code)
            self.codes[code] = {"path": path, **params}
            back["code"] = code
        if path == "/authorize" and self.send_iss:
            back["iss"] = self.iss_value or self.url
        return params["redirect_uri"] + "?" + urllib.parse.urlencode(back)

    def token(self, path: str, form: dict, authorization: str | None) -> tuple[int, dict]:
        self.token_seen.append((dict(form), authorization))
        if self.token_status is not None:
            return self.token_status, {"error": "server_error"}
        github = path == "/gh/login/oauth/access_token"
        if github:
            client = (form.get("client_id"), form.get("client_secret"))
        elif authorization and authorization.startswith("Basic "):
            pair = base64.b64decode(authorization[6:]).decode()
            client = tuple(urllib.parse.unquote_plus(p) for p in pair.split(":", 1))
        else:
            client = (form.get("client_id"), None)
        grant = self.codes.pop(form.get("code", ""), None)
        refused = (
            client != ("web", "web-secret") or grant is None
            or form.get("grant_type") != "authorization_code"
            or grant["redirect_uri"] != form.get("redirect_uri")
            or grant.get("code_challenge_method") != "S256"
            or s256(form.get("code_verifier", "")) != grant.get("code_challenge")
        )
        if refused:
            # GitHub reports a bad code with a 200.
            return (200 if github else 400), {"error": "invalid_grant"}
        access = "at-" + secrets.token_urlsafe(16)
        self.issued.append(access)
        if github:
            if self.github_error_in_200:
                return 200, {"error": "bad_verification_code"}
            self.github_tokens.add(f"Bearer {access}")
            return 200, {"access_token": access, "token_type": "bearer",
                         "scope": "read:user,user:email"}
        now = int(time.time())
        if grant["path"].startswith("/ms/"):
            iss = f"{self.url}/ms/{self.ms_iss_tid or self.ms_tid}/v2.0"
            claims = {"iss": iss, "tid": self.ms_tid, "sub": "ms-1", "email": "ada@contoso.com"}
        else:
            claims = {"iss": self.url, **self.user}
        claims.update({"aud": grant["client_id"], "iat": now, "exp": now + 300,
                       "nonce": grant.get("nonce")})
        claims.update(self.id_overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        id_token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "a"})
        self.issued.append(id_token.split(".")[-1])
        body = {"access_token": access, "token_type": "Bearer", "expires_in": 300,
                "refresh_token": "rt-" + secrets.token_urlsafe(8)}
        self.issued.append(body["refresh_token"])
        if not self.no_id_token:
            body["id_token"] = id_token
        return 200, body


class Browser:
    """Cookies per host, sent over plain HTTP whatever their `Secure` says —
    as a browser does for localhost — and redirects followed only when asked."""

    def __init__(self) -> None:
        self.jar: dict[str, dict[str, str]] = {}

    def go(self, method: str, url: str, **kwargs) -> httpx.Response:
        host = urllib.parse.urlsplit(url).netloc
        jar = self.jar.setdefault(host, {})
        headers = dict(kwargs.pop("headers", {}))
        if jar:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        r = httpx.request(method, url, headers=headers, timeout=15, **kwargs)
        for name, value in r.headers.multi_items():
            if name == "set-cookie":
                key, _, rest = value.partition("=")
                jar[key] = rest.split(";")[0]
        return r

    def cookie(self, url: str, name: str = "oxbrook_session") -> str | None:
        return self.jar.get(urllib.parse.urlsplit(url).netloc, {}).get(name)


provider = Provider()
PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"

sessions = Sessions(secret=SECRET, secure=False)
web = SessionAuth(sessions, key="user_id")
logins: list[tuple[str, Login]] = []


async def on_login(request, who: Login):
    logins.append((request.path, who))
    if who.subject == "banned":
        return None
    return f"user:{who.subject}"


def on_login_sync(_, who: Login):
    return f"sync:{who.subject}"


oidc = OAuthLogin(provider.url, client_id="web", client_secret="web-secret", sessions=sessions,
                  on_login=on_login, name="acme")
other = OAuthLogin(provider.url, client_id="web", client_secret="web-secret", sessions=sessions,
                   on_login=on_login_sync, name="other")
fixed = OAuthLogin(provider.url, client_id="web", client_secret="web-secret", sessions=sessions,
                   redirect_uri=f"{BASE}/auth/fixed/back", name="fixed")
github = OAuthLogin.github(client_id="web", client_secret="web-secret", sessions=sessions,
                           server=provider.url + "/gh", api=provider.url + "/gh/api")
microsoft = OAuthLogin.microsoft(client_id="web", client_secret="web-secret", sessions=sessions,
                                 authority=provider.url + "/ms")
organizations = OAuthLogin.microsoft(client_id="web", client_secret="web-secret",
                                     sessions=sessions, authority=provider.url + "/ms",
                                     tenant="organizations", name="org")
unreachable = OAuthLogin("http://127.0.0.1:9", client_id="web", client_secret="web-secret",
                         sessions=sessions, name="down", timeout=2)

# Every route is the session's unless it says otherwise: the login routes
# must be reachable without one anyway.
app = App(auth=web)
app.middleware(sessions.middleware)
app.include(oidc.routes(prefix="/auth/acme"))
app.include(other.routes(prefix="/auth/other"))
app.include(fixed.routes(prefix="/auth/fixed", callback="/back"))
app.include(github.routes(prefix="/auth/github"))
app.include(microsoft.routes(prefix="/auth/ms"))
app.include(organizations.routes(prefix="/auth/org"))
app.include(unreachable.routes(prefix="/auth/down"))


@app.get("/me")
async def me(_: Request, who: Principal = Depends(principal)):
    return {"subject": who.subject}


@app.post("/notes")
async def notes(_: Request):
    return {"ok": True}


@app.get("/cart", auth=None)
async def cart(_: Request, session=Depends(sessions.load)):
    session["cart"] = ["tea"]
    session["user_id"] = "planted"
    return {"csrf": SessionAuth.csrf_token(session)}


def session_of(browser: Browser) -> dict:
    raw = browser.cookie(BASE)
    return sessions.decode(raw) if raw else {}


def start(browser: Browser, prefix: str, query: str = "") -> httpx.Response:
    """The login link, and the provider's answer: the redirect back."""
    r = browser.go("GET", f"{BASE}{prefix}/login{query}")
    check(r.status_code == 302, f"{prefix}/login answered {r.status_code} {r.text[:200]}")
    return browser.go("GET", r.headers["location"])


def log_in(browser: Browser, prefix: str = "/auth/acme", query: str = "") -> httpx.Response:
    """The whole dance: login link, provider, callback. Returns the callback's answer."""
    back = start(browser, prefix, query)
    return browser.go("GET", back.headers["location"])


def refused(r: httpx.Response, status: int, what: str) -> None:
    check(r.status_code == status and r.headers.get("content-type") == "application/problem+json",
          f"{what}: {r.status_code} {r.text[:200]}")


# ---- cases: construction --------------------------------------------------------


def construction_rules() -> None:
    def raises(kind, fn, what):
        try:
            fn()
        except kind:
            return
        except Exception as exc:
            failures.append(f"{what} raised {type(exc).__name__}: {exc}")
            return
        failures.append(f"{what} was accepted")

    base = {"client_id": "web", "client_secret": "s", "sessions": sessions}
    strict = Sessions(secret=SECRET, same_site="Strict")
    raises(ValueError, lambda: OAuthLogin(provider.url, **{**base, "sessions": strict}),
           "a SameSite=Strict session")
    raises(TypeError, lambda: OAuthLogin(provider.url, **{**base, "sessions": object()}),
           "no Sessions")
    raises(ValueError, lambda: OAuthLogin(provider.url, **base, scopes=["email"]),
           "an OpenID login without the openid scope")
    raises(TypeError, lambda: OAuthLogin(provider.url, **base, scopes="openid email"),
           "scopes as one string")
    raises(ValueError, lambda: OAuthLogin(provider.url, **base, params={"state": "x"}),
           "params= setting the state")
    raises(ValueError, lambda: OAuthLogin(provider.url, **base, after_login="//evil.example"),
           "after_login on another site")
    raises(ValueError, lambda: OAuthLogin(provider.url, **base,
                                          redirect_uri="http://app.example.com/cb"),
           "a plain-HTTP redirect_uri off loopback")
    raises(ValueError, lambda: OAuthLogin("http://idp.example.com", **base),
           "a plain-HTTP issuer off loopback")
    raises(TypeError, lambda: OAuthLogin(provider.url, **{**base, "client_id": ""}),
           "an empty client_id")
    raises(ValueError, lambda: OAuthLogin.microsoft(**base, tenant="contoso.com"),
           "a Microsoft tenant given as a domain")
    raises(ValueError, lambda: OAuthLogin.github(**base, server="http://github.example.com"),
           "a plain-HTTP GitHub server")
    raises(TypeError, lambda: OAuthLogin.github(**base, audience="x"), "an unknown option")
    raises(ValueError, lambda: oidc.routes(login="/in/{who}"), "a login route with a parameter")
    check(OAuthLogin.google(**base).verifier._issuers
          == ["https://accounts.google.com", "accounts.google.com"],
          "google's ID tokens name their issuer two ways, and both must be accepted")
    check(OAuthLogin.microsoft(**base, tenant="consumers").issuer
          == f"https://login.microsoftonline.com/{CONSUMERS}/v2.0",
          "consumers has one issuer, the personal-account tenant's")
    check("SECRET" not in repr(Login(provider="p", subject="s", tokens={"access_token": "SECRET"})),
          "a Login's repr shows its tokens")


# ---- cases: the scripted provider -----------------------------------------------


def a_person_logs_in(c: TestClient) -> None:
    provider.reset()
    logins.clear()
    b = Browser()
    check(b.go("GET", f"{BASE}/me").status_code == 401, "/me answered before any login")
    r = b.go("GET", f"{BASE}/auth/acme/login?next=/me")
    check(r.status_code == 302 and r.headers.get("cache-control") == "no-store",
          f"the login link answered {r.status_code} {dict(r.headers)}")
    sent = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))
    check(sent.get("code_challenge_method") == "S256" and len(sent.get("code_challenge", "")) == 43,
          f"no S256 challenge in {sent}")
    check(sent.get("scope") == "openid email profile" and sent.get("client_id") == "web"
          and sent.get("response_type") == "code", f"the authorization request was {sent}")
    check(len(sent.get("state", "")) >= 43 and len(sent.get("nonce", "")) >= 43,
          "state or nonce is short")
    check(sent.get("redirect_uri") == f"{BASE}/auth/acme/callback",
          f"the redirect URI was {sent.get('redirect_uri')}")
    back = b.go("GET", r.headers["location"])
    r = b.go("GET", back.headers["location"])
    check(r.status_code == 303 and r.headers.get("location") == "/me",
          f"the callback answered {r.status_code} {r.headers.get('location')} {r.text[:200]}")
    r = b.go("GET", f"{BASE}/me")
    check(r.status_code == 200 and r.json() == {"subject": "user:u-1"},
          f"after logging in /me gave {r.status_code} {r.text}")
    check(set(session_of(b)) == {"user_id"}, f"the session after login held {session_of(b)}")

    form, authorization = provider.token_seen[-1]
    check(authorization == "Basic " + base64.b64encode(b"web:web-secret").decode()
          and "client_secret" not in form, "the client did not authenticate with HTTP Basic")
    check(s256(form["code_verifier"]) == sent["code_challenge"],
          "the token request's verifier is not the challenge's")

    path, who = logins[-1]
    check(path == "/auth/acme/callback", f"on_login saw the request for {path}")
    check((who.provider, who.subject, who.email, who.email_verified, who.name)
          == ("acme", "u-1", "ada@example.com", True, "Ada Lovelace"), f"the Login was {who}")
    check(who.claims["nonce"] == sent["nonce"] and "access_token" in who.tokens
          and "refresh_token" in who.tokens, "the Login lacks the claims or the tokens")

    # A second provider with a plain function for on_login.
    r = log_in(b, "/auth/other")
    check(r.status_code == 303 and session_of(b).get("user_id") == "sync:u-1",
          f"a synchronous on_login gave {r.status_code} {session_of(b)}")
    # No next: after_login.
    check(log_in(Browser()).headers.get("location") == "/", "with no next, not sent to /")
    # A redirect URI given rather than worked out, on a renamed callback.
    b2 = Browser()
    r = b2.go("GET", f"{BASE}/auth/fixed/login")
    check(f"redirect_uri={urllib.parse.quote(BASE + '/auth/fixed/back', safe='')}"
          in r.headers["location"], "the configured redirect URI was not sent")
    back = b2.go("GET", r.headers["location"])
    r = b2.go("GET", back.headers["location"])
    check(r.status_code == 303 and session_of(b2).get("user_id") == "fixed:u-1",
          f"the configured callback gave {r.status_code} {r.text[:200]}")


def good_callback(state: str) -> str:
    """A callback for the last login started, with a code the provider will
    exchange: only the state can make it fail."""
    sent = provider.authorize_seen[-1]
    code = secrets.token_urlsafe(16)
    provider.issued.append(code)
    provider.codes[code] = {"path": "/authorize", **sent}
    return (f"{BASE}/auth/acme/callback?state={state}&code={code}&iss="
            + urllib.parse.quote(provider.url))


def state_is_checked(c: TestClient) -> None:
    provider.reset()
    b = Browser()
    back = start(b, "/auth/acme")
    url = back.headers["location"]
    code_url = url.replace("state=", "state=x")  # an unknown state
    refused(b.go("GET", code_url), 400, "a callback with an unknown state")
    no_state = re.sub(r"state=[^&]*&?", "", url)
    refused(b.go("GET", no_state), 400, "a callback with no state")
    refused(Browser().go("GET", url), 400, "a callback in a browser that started no login")
    twice = url + "&state=" + urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    refused(b.go("GET", twice), 400, "a callback with the state twice")

    # Another provider's login, finished at this one's callback.
    b = Browser()
    back = start(b, "/auth/other")
    refused(b.go("GET", back.headers["location"].replace("/auth/other/", "/auth/acme/")), 400,
            "another provider's state")

    # Used once, even when it failed: a refused callback consumes its state.
    b = Browser()
    back = start(b, "/auth/acme")
    url = back.headers["location"]
    r = b.go("GET", url)
    check(r.status_code == 303, f"a good callback gave {r.status_code}")
    refused(b.go("GET", url), 400, "a replayed callback")
    b = Browser()
    provider.error = "access_denied"
    back = start(b, "/auth/acme")
    provider.error = None
    refused(b.go("GET", back.headers["location"]), 403, "a login the person cancelled")
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(back.headers["location"]).query)["state"]
    refused(b.go("GET", good_callback(state[0])), 400, "a state reused after a refusal")

    # Too old: a pending login ten minutes and more in the past.
    b = Browser()
    start(b, "/auth/acme")
    data = session_of(b)
    for entry in data["_oauth"].values():
        entry[5] -= 601
    b.jar[f"127.0.0.1:{PORT}"]["oxbrook_session"] = sessions.encode(data)
    state = next(iter(data["_oauth"]))
    refused(b.go("GET", good_callback(state)), 400, "a login older than ten minutes")
    # The control: the same kind of callback, on a login still pending, works.
    b = Browser()
    start(b, "/auth/acme")
    state = next(iter(session_of(b)["_oauth"]))
    check(b.go("GET", good_callback(state)).status_code == 303,
          "a callback with a good code and a pending state did not log in")

    # Logins in progress are bounded: the oldest go, the newest still works.
    b = Browser()
    backs = [start(b, "/auth/acme") for _ in range(7)]
    check(len(session_of(b)["_oauth"]) == 4,
          f"{len(session_of(b)['_oauth'])} logins in progress were kept")
    refused(b.go("GET", backs[0].headers["location"]), 400, "the oldest of seven logins")
    check(b.go("GET", backs[-1].headers["location"]).status_code == 303,
          "the newest of seven logins did not finish")


def a_stolen_code_is_useless(c: TestClient) -> None:
    provider.reset()
    victim, attacker = Browser(), Browser()
    stolen = urllib.parse.parse_qs(
        urllib.parse.urlsplit(start(victim, "/auth/acme").headers["location"]).query)["code"][0]
    mine = start(attacker, "/auth/acme").headers["location"]
    # The attacker's own state, the victim's code: the verifier sent is the
    # attacker's, which is not the challenge the victim's code was issued for.
    played = re.sub(r"code=[^&]*", f"code={stolen}", mine)
    refused(attacker.go("GET", played), 400, "a code played into another login")
    check("user_id" not in session_of(attacker), "the stolen code logged the attacker in")


def the_id_token_is_checked(c: TestClient) -> None:
    for overrides, what in (
        ({"nonce": "someone-elses"}, "another login's nonce"),
        ({"nonce": None}, "no nonce"),
        ({"aud": "another-client"}, "an ID token for another client"),
        ({"iss": "http://127.0.0.1:1"}, "an ID token from another issuer"),
        ({"exp": int(time.time()) - 3600}, "an expired ID token"),
        ({"aud": ["web", "another-client"]}, "several audiences and no azp"),
        ({"sub": None}, "an ID token with no subject"),
    ):
        provider.reset()
        provider.id_overrides = overrides
        b = Browser()
        refused(log_in(b), 400, what)
        check("user_id" not in session_of(b), f"{what} logged someone in")
    provider.reset()
    provider.id_overrides = {"aud": ["web", "another-client"], "azp": "web"}
    check(log_in(Browser()).status_code == 303, "several audiences with azp this client")
    provider.reset()
    provider.no_id_token = True
    refused(log_in(Browser()), 502, "a token response with no ID token")
    provider.reset()
    provider.user = {**provider.user, "sub": "banned"}
    b = Browser()
    refused(log_in(b), 403, "a person on_login refused")
    check("user_id" not in session_of(b), "a refused person was logged in")
    provider.reset()
    provider.user = {"sub": "u-2", "email": "x@example.com", "email_verified": "true"}
    log_in(Browser())
    check(logins[-1][1].email_verified is False, "email_verified taken from a string")


def the_callback_names_its_issuer(c: TestClient) -> None:
    provider.reset()
    provider.iss_value = "https://mix-up.example.com"
    refused(log_in(Browser()), 400, "a callback naming another issuer")
    provider.reset()
    provider.send_iss = False
    refused(log_in(Browser()), 400, "no iss from a provider that promised one")
    # Discovery is fetched once, so this login's provider forgets its promise
    # through a fresh OAuthLogin and a fresh app.
    provider.reset()
    provider.promise_iss = False
    provider.send_iss = False
    quiet = OAuthLogin(provider.url, client_id="web", client_secret="web-secret",
                       sessions=sessions, name="quiet")
    quiet_app = App()
    quiet_app.include(quiet.routes(prefix="/q"))
    with TestClient(quiet_app, port=free_port()) as q:
        b = Browser()
        r = b.go("GET", f"{q.base_url}/q/login")
        back = b.go("GET", r.headers["location"])
        r = b.go("GET", back.headers["location"])
        check(r.status_code == 303, f"no iss, none promised, gave {r.status_code} {r.text}")
    provider.reset()


def the_provider_can_fail(c: TestClient) -> None:
    provider.reset()
    provider.error = "server_error"
    refused(log_in(Browser()), 400, "the provider reporting an error")
    provider.reset()
    provider.token_status = 500
    refused(log_in(Browser()), 502, "a token endpoint answering 500")
    provider.reset()
    provider.token_status = 400
    refused(log_in(Browser()), 400, "a token endpoint refusing the code")
    r = Browser().go("GET", f"{BASE}/auth/down/login")
    refused(r, 503, "a provider that cannot be reached")


def next_stays_on_this_site(c: TestClient) -> None:
    provider.reset()
    for wanted, where in (("//evil.example/x", "/"), ("https://evil.example", "/"),
                          ("/\\evil.example", "/"),
                          ("/%0d%0aSet-Cookie:x=1", "/%0d%0aSet-Cookie:x=1"),
                          ("/notes?page=2", "/notes?page=2"), ("javascript:alert(1)", "/")):
        r = log_in(Browser(), query="?" + urllib.parse.urlencode({"next": wanted}))
        check(r.headers.get("location") == where,
              f"next={wanted!r} went to {r.headers.get('location')!r}, not {where!r}")
    r = log_in(Browser(), query="?next=/a&next=/b")
    check(r.headers.get("location") == "/", "two nexts, and one was followed")


def the_session_starts_fresh(c: TestClient) -> None:
    provider.reset()
    b = Browser()
    token = b.go("GET", f"{BASE}/cart").json()["csrf"]
    check(session_of(b).get("cart") == ["tea"], "the cart was not kept before logging in")
    log_in(b)
    data = session_of(b)
    check(data == {"user_id": "user:u-1"},
          f"what was in the session before the login survived it: {sorted(data)}")
    r = b.go("POST", f"{BASE}/notes", headers={"x-csrf-token": token})
    check(r.status_code == 403,
          f"the CSRF token from before the login still worked: {r.status_code}")


def logout_ends_the_session(c: TestClient) -> None:
    provider.reset()
    b = Browser()
    log_in(b)
    check(b.go("GET", f"{BASE}/me").status_code == 200, "not logged in before logout")
    r = b.go("POST", f"{BASE}/auth/acme/logout", headers={"origin": "https://evil.example"})
    refused(r, 403, "a logout forged by another site")
    check(b.go("GET", f"{BASE}/me").status_code == 200, "a forged logout logged the person out")
    check(b.go("GET", f"{BASE}/auth/acme/logout").status_code == 405, "logout by GET")
    r = b.go("POST", f"{BASE}/auth/acme/logout", headers={"sec-fetch-site": "same-origin"})
    check(r.status_code == 303 and r.headers.get("location") == "/",
          f"logout answered {r.status_code} {r.headers.get('location')}")
    check(b.go("GET", f"{BASE}/me").status_code == 401, "still logged in after logout")
    check(session_of(b) == {}, f"the session after logout held {session_of(b)}")
    r = Browser().go("POST", f"{BASE}/auth/acme/logout")
    check(r.status_code == 303, f"logging out with no session gave {r.status_code}")


def github_logs_in_without_an_id_token(c: TestClient) -> None:
    provider.reset()
    b = Browser()
    back = start(b, "/auth/github")
    sent = provider.authorize_seen[-1]
    check(sent.get("scope") == "read:user user:email" and "nonce" not in sent
          and sent.get("code_challenge_method") == "S256", f"github was asked {sent}")
    r = b.go("GET", back.headers["location"])
    check(r.status_code == 303 and session_of(b).get("user_id") == "github:42",
          f"github login gave {r.status_code} {r.text[:200]} {session_of(b)}")
    form, authorization = provider.token_seen[-1]
    check(authorization is None and form.get("client_secret") == "web-secret",
          "github's token request did not carry the client in the form")
    provider.reset()
    provider.github_emails = [
        {"email": "unverified@example.com", "primary": True, "verified": False},
        {"email": "second@example.com", "primary": False, "verified": True},
    ]
    seen = []
    github.on_login = lambda _, who: seen.append(who) or f"github:{who.subject}"
    try:
        log_in(Browser(), "/auth/github")
        check(seen and (seen[0].subject, seen[0].email, seen[0].email_verified, seen[0].name)
              == ("42", "second@example.com", True, "Ada Lovelace"),
              f"github's Login was {seen[:1]}")
        provider.reset()
        provider.github_emails = [{"email": "x@example.com", "primary": True, "verified": False}]
        seen.clear()
        log_in(Browser(), "/auth/github")
        check(seen and seen[0].email is None and not seen[0].email_verified,
              "an unverified GitHub email was used")
    finally:
        github.on_login = None
    provider.reset()
    provider.github_error_in_200 = True
    refused(log_in(Browser(), "/auth/github"), 400, "github's error in a 200")
    provider.reset()
    provider.github_user_status = 500
    refused(log_in(Browser(), "/auth/github"), 502, "github's /user failing")
    provider.reset()
    provider.github_user = {"id": "42", "login": "x"}
    refused(log_in(Browser(), "/auth/github"), 502, "a github user whose id is not a number")


def microsoft_checks_the_tenant(c: TestClient) -> None:
    provider.reset()
    b = Browser()
    r = log_in(b, "/auth/ms")
    check(r.status_code == 303 and session_of(b).get("user_id") == "microsoft:ms-1",
          f"a Microsoft login gave {r.status_code} {r.text[:200]}")
    provider.ms_iss_tid = TENANT_B
    refused(log_in(Browser(), "/auth/ms"), 400, "an issuer naming another tenant than tid")
    provider.reset()
    provider.ms_tid = "not-a-guid"
    refused(log_in(Browser(), "/auth/ms"), 400, "a tenant that is not a GUID")
    provider.reset()
    provider.ms_tid = CONSUMERS
    check(log_in(Browser(), "/auth/ms").status_code == 303, "common refused a personal account")
    refused(log_in(Browser(), "/auth/org"), 400, "organizations accepted a personal account")
    provider.reset()
    seen = []
    microsoft.on_login = lambda _, who: seen.append(who) or "ms"
    try:
        log_in(Browser(), "/auth/ms")
    finally:
        microsoft.on_login = None
    check(seen and seen[0].email == "ada@contoso.com" and seen[0].email_verified is False,
          f"Microsoft's unverified email was marked verified: {seen[:1]}")


def the_routes_are_documented_as_public(c: TestClient) -> None:
    from openapi_spec_validator import validate

    spec = c.get("/openapi.json").json()
    # Seven providers' routes in one document: operation ids stay unique.
    validate(spec)
    operation = spec["paths"]["/auth/acme/login"]["get"]
    check(operation.get("security") == [],
          f"the login route's security is {operation.get('security')}")


def no_secret_reaches_a_log(records: list[logging.LogRecord]) -> None:
    text = "\n".join(r.getMessage() + repr(r.__dict__) for r in records)
    leaked = [v[:12] for v in provider.issued if v in text]
    check(not leaked, f"a code or token reached a log line: {leaked}")
    for seen in provider.authorize_seen:
        for name in ("state", "nonce", "code_challenge"):
            value = seen.get(name)
            check(not value or value not in text, f"a login's {name} reached a log line")
    for form, _ in provider.token_seen:
        verifier = form.get("code_verifier")
        check(not verifier or verifier not in text, "a PKCE verifier reached a log line")
    check("web-secret" not in text, "the client secret reached a log line")
    check("login refused" in text, "refused logins left no log line")


# ---- cases: Keycloak ---------------------------------------------------------------


def keycloak_reachable() -> bool:
    try:
        with urllib.request.urlopen(
            f"{KEYCLOAK}/realms/{REALM}/.well-known/openid-configuration", timeout=3
        ) as r:
            return r.status == 200
    except Exception:
        return False


kc_seen: list[Login] = []
kc_sessions = Sessions(secret=SECRET, secure=False)
kc_login = OAuthLogin.keycloak(
    KEYCLOAK, REALM, client_id="notes-login", client_secret="notes-login-secret",
    sessions=kc_sessions,
    on_login=lambda _, who: kc_seen.append(who) or who.claims["preferred_username"],
)
kc_app = App(auth=SessionAuth(kc_sessions))
kc_app.include(kc_login.routes(prefix="/auth"))


@kc_app.get("/me")
async def kc_me(_: Request, who: Principal = Depends(principal)):
    return {"subject": who.subject}


def keycloak_form(browser: Browser, location: str, username: str, password: str) -> httpx.Response:
    page = browser.go("GET", location)
    form = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page.text)
    check(form is not None, f"no Keycloak login form: {page.status_code}")
    return browser.go("POST", form.group(1).replace("&amp;", "&"),
                      data={"username": username, "password": password})


def keycloak_logs_a_person_in(c: TestClient) -> None:
    kc_seen.clear()
    b = Browser()
    r = b.go("GET", f"{c.base_url}/auth/login?next=/me")
    wrong = keycloak_form(b, r.headers["location"], "ada", "not-her-password")
    check(wrong.status_code == 200 and "kc-form-login" in wrong.text,
          f"a wrong password did not return to the form: {wrong.status_code}")
    back = keycloak_form(b, r.headers["location"], "ada", "ada-password")
    check(back.status_code == 302 and back.headers["location"].startswith(c.base_url),
          f"keycloak did not send ada back: {back.status_code}")
    r = b.go("GET", back.headers["location"])
    check(r.status_code == 303 and r.headers.get("location") == "/me",
          f"the callback answered {r.status_code} {r.text[:300]}")
    r = b.go("GET", f"{c.base_url}/me")
    check(r.status_code == 200 and r.json() == {"subject": "ada"}, f"/me gave {r.text}")
    who = kc_seen[-1] if kc_seen else None
    check(who is not None and who.email == "ada@example.com" and who.email_verified
          and who.name == "Ada Lovelace" and who.provider == "keycloak",
          f"ada's Login was {who}")

    # Keycloak remembers her: a second login needs no password, and still
    # goes through state, PKCE and the ID token.
    r = b.go("GET", f"{c.base_url}/auth/login")
    back = b.go("GET", r.headers["location"])
    check(back.status_code == 302, f"keycloak's single sign-on answered {back.status_code}")
    check(b.go("GET", back.headers["location"]).status_code == 303, "the second login failed")


def keycloak_refuses_a_stolen_code(c: TestClient) -> None:
    victim, attacker = Browser(), Browser()
    r = victim.go("GET", f"{c.base_url}/auth/login")
    back = keycloak_form(victim, r.headers["location"], "bob", "bob-password")
    stolen = urllib.parse.parse_qs(urllib.parse.urlsplit(back.headers["location"]).query)["code"][0]
    r = attacker.go("GET", f"{c.base_url}/auth/login")
    mine = keycloak_form(attacker, r.headers["location"], "ada", "ada-password")
    played = re.sub(r"code=[^&]*", f"code={urllib.parse.quote(stolen)}", mine.headers["location"])
    refused(attacker.go("GET", played), 400, "keycloak accepting bob's code in ada's login")
    check("user_id" not in kc_sessions.decode(attacker.cookie(c.base_url) or ""),
          "the stolen code logged someone in")


def the_example_works(_: TestClient) -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "examples" / "login.py"
    spec = importlib.util.spec_from_file_location("login_example", path)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    with TestClient(example.app) as c:
        b = Browser()
        check(b.go("GET", f"{c.base_url}/").status_code == 200, "the example's home page")
        check(b.go("GET", f"{c.base_url}/notes").status_code == 401,
              "the example's notes are not protected")
        r = b.go("GET", f"{c.base_url}/auth/login?next=/notes")
        back = keycloak_form(b, r.headers["location"], "ada", "ada-password")
        r = b.go("GET", back.headers["location"])
        check(r.status_code == 303 and r.headers.get("location") == "/notes",
              f"the example's callback gave {r.status_code} {r.text[:300]}")
        r = b.go("GET", f"{c.base_url}/me")
        check(r.status_code == 200 and r.json().get("name") == "Ada Lovelace",
              f"the example's /me gave {r.status_code} {r.text}")
        r = b.go("POST", f"{c.base_url}/notes", json={"title": "from a browser"},
                 headers={"origin": c.base_url})
        check(r.status_code == 200, f"ada could not write a note: {r.status_code} {r.text}")
        r = b.go("POST", f"{c.base_url}/notes", json={"title": "forged"},
                 headers={"origin": "https://evil.example"})
        check(r.status_code == 403, f"a forged note was accepted: {r.status_code}")
        r = b.go("POST", f"{c.base_url}/auth/logout", headers={"origin": c.base_url})
        check(r.status_code == 303 and b.go("GET", f"{c.base_url}/me").status_code == 401,
              "the example's logout did not log ada out")


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

    print("provider under test control")
    run([construction_rules], None)
    with TestClient(app, port=PORT) as c:
        run([a_person_logs_in, state_is_checked, a_stolen_code_is_useless,
             the_id_token_is_checked, the_callback_names_its_issuer, the_provider_can_fail,
             next_stays_on_this_site, the_session_starts_fresh, logout_ends_the_session,
             github_logs_in_without_an_id_token, microsoft_checks_the_tenant,
             the_routes_are_documented_as_public], c)

    if reachable:
        print("keycloak")
        with TestClient(kc_app) as c:
            run([keycloak_logs_a_person_in, keycloak_refuses_a_stolen_code,
                 the_example_works], c)
    else:
        print("keycloak: SKIP")

    no_secret_reaches_a_log(capture.records)
    print("  no_secret_reaches_a_log: ok")
    provider.close()

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
