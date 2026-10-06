"""Logging people in through an identity provider: OAuth 2's authorization code.

`OAuthLogin` and `Login` are public through `oxbrook.auth`. The flow is the
one RFC 9700 recommends for an app with a server: authorization code with
PKCE, `state` tied to the browser that started the login, and for OpenID
Connect a `nonce` tied to the ID token. All three are kept in the app's signed
session between the redirect out and the callback back, so there is nothing
server-side to store and every worker loop and process agrees.

What the flow leaves behind is an ordinary session holding one value, which
`SessionAuth` then reads like any other. The provider's tokens are handed to
the app once, at login, and are not kept.
"""

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import _jwks
from ._errors import HTTPError
from ._response import Response
from .auth import (
    OIDC,
    Forbidden,
    SessionAuth,
    Unauthenticated,
    _bare,
    _call,
    _frozen,
    _segment,
    logger,
)

#: Where logins in progress are kept in the session: state -> what the
#: callback needs to finish that login.
PENDING_KEY = "_oauth"

#: Logins one browser may have in progress at once, one per tab, say. The
#: oldest is dropped beyond this, so the cookie cannot grow without bound.
PENDING_LIMIT = 4

#: Seconds between being sent to the provider and coming back. Long enough to
#: type a password and pass a second factor; a login older than this is over.
PENDING_TTL = 600

#: Authorization parameters the flow sets itself, which `params=` may not.
_RESERVED = frozenset({
    "response_type", "client_id", "redirect_uri", "scope", "state", "nonce",
    "code_challenge", "code_challenge_method",
})

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: Microsoft's tenant for personal accounts: an `organizations` login refuses it.
_CONSUMERS = "9188040d-6c67-4c5b-b112-36a304b66dad"


class LoginFailed(HTTPError):
    """A login that could not be completed.

    `400` for a callback that does not belong to a login this browser started
    (or one already finished, or too old), `403` when the provider or the
    app's `on_login` refused the person, `502` or `503` when the provider
    could not be reached or answered nonsense. `detail` is written for the
    person; why, exactly, goes to the `oxbrook.auth` log. Register an
    exception handler for it to show a page rather than problem details.
    """

    def __init__(self, status: int, detail: str, **kwargs: Any) -> None:
        super().__init__(status, detail, {"cache-control": "no-store"}, **kwargs)


@dataclass(frozen=True, slots=True)
class Login:
    """Who the provider says logged in, as `on_login` receives it.

    `subject` is the provider's stable id for the person: key accounts on
    `(provider, subject)`, never on the email address, which a person can
    change and some providers do not verify. `email_verified` is True only
    when the provider said so. `claims` is the verified ID token's claims, or
    for GitHub the user as its API describes them. `tokens` is the provider's
    token response — the access token, and a refresh token if one was asked
    for — for an app that calls the provider's API later; it is not kept
    anywhere unless the app keeps it, and it is left out of `repr`.
    """

    provider: str
    subject: str
    email: str | None = None
    email_verified: bool = False
    name: str | None = None
    claims: Mapping[str, Any] = field(default_factory=dict)
    tokens: Mapping[str, Any] = field(default_factory=dict, repr=False)


def _local(value: Any) -> str | None:
    """`value` if it is a path on this site, else None.

    Where to go after logging in arrives in the query string, so anyone can
    write it into a link. Only a path is followed: `//evil.example` and
    `/\\evil.example` are other sites to a browser, and a scheme is one too.
    """
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or len(value) > 2048):
        return None
    if any(c == "\\" or not "!" <= c <= "~" for c in value):
        return None
    return value


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _check_redirect(url: str, allow_http: bool) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.fragment:
        raise ValueError(f"redirect_uri must be an absolute http(s) URL, got {url!r}")
    if parsed.scheme == "http" and not allow_http and parsed.hostname not in _LOOPBACK:
        raise ValueError(
            f"redirect_uri {url!r} is plain HTTP: the authorization code would cross the "
            f"network readable. Use HTTPS, or allow_http=True on a private network"
        )
    return url


def _pending(session: Any) -> dict[str, list]:
    """Logins in progress, with anything malformed or expired left out."""
    found = session.get(PENDING_KEY)
    if not isinstance(found, dict):
        return {}
    now = time.time()
    return {
        state: entry for state, entry in found.items()
        if isinstance(entry, list) and len(entry) == 6 and isinstance(entry[5], int)
        and now - entry[5] <= PENDING_TTL
    }


class OAuthLogin:
    """Logging people in through an identity provider.

        sessions = Sessions(secret=os.environ["SECRET_KEY"])
        google = OAuthLogin.google(
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            sessions=sessions,
            on_login=find_or_create_user,
        )
        app.include(google.routes(prefix="/auth"))  # /auth/login, /auth/callback, /auth/logout
        web = SessionAuth(sessions, key="user_id", load=load_user)

    A link to `/auth/login?next=/notes` sends the person to the provider;
    the provider sends them back to `/auth/callback`, which finishes the login
    and redirects to `/notes`. `next` is followed only when it is a path on
    this site. `POST /auth/logout` empties the session.

    `on_login(request, login)` is where the app decides who that is: it
    receives a `Login`, and returns the value to keep in the session under
    `key` — usually the app's own user id, after finding or creating the user.
    Returning None refuses the login with `403`. It may be async, and
    receives the request so it can reach a pool from `request.state`. With no
    `on_login` the value kept is `"<name>:<subject>"`, such as
    `"google:1161..."`.

    **What the flow checks.** `state` must match a login this browser started,
    within ten minutes, and is good once. PKCE (`S256`) binds the code to that
    login too, so a code intercepted on its way back is useless elsewhere. For
    OpenID Connect providers the ID token is checked like any `OIDC` token —
    signature, issuer, audience (the client id), expiry — and its `nonce` must
    be this login's. An `iss` parameter on the callback must name the
    provider, which stops a callback from one provider being passed to
    another's endpoint. The session is emptied before the new value goes in,
    so nothing written into it before the login survives it.

    **The redirect URI** must be registered with the provider exactly. It is
    worked out from the request unless `redirect_uri=` gives it; give it
    behind a proxy that changes the host or the scheme.

    The session cookie must be `SameSite=Lax` or `None`: the callback arrives
    as a navigation from the provider's site, which a `Strict` cookie is not
    sent with.

    `OAuthLogin(issuer, ...)` works with any OpenID Connect provider that
    serves discovery. The class methods set up `google`, `microsoft`,
    `github` and `keycloak`.
    """

    def __init__(
        self,
        issuer: str,
        *,
        client_id: str,
        client_secret: str | None,
        sessions: Any,
        scopes: Iterable[str] = ("openid", "email", "profile"),
        on_login: Callable[[Any, Login], Any] | None = None,
        key: str = "user_id",
        redirect_uri: str | None = None,
        after_login: str = "/",
        after_logout: str = "/",
        params: Mapping[str, str] | None = None,
        trusted_origins: Iterable[str] | None = None,
        discovery_url: str | None = None,
        allow_http: bool = False,
        timeout: float = 10,
        name: str = "oidc",
    ) -> None:
        self._setup(
            client_id=client_id, client_secret=client_secret, sessions=sessions, scopes=scopes,
            on_login=on_login, key=key, redirect_uri=redirect_uri, after_login=after_login,
            after_logout=after_logout, params=params, trusted_origins=trusted_origins,
            allow_http=allow_http, timeout=timeout, name=name,
        )
        self._openid(OIDC(
            issuer, audience=client_id, discovery_url=discovery_url, allow_http=allow_http,
            timeout=timeout, cache_size=0, name=name,
        ))

    def _setup(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        sessions: Any,
        scopes: Iterable[str],
        on_login: Callable[[Any, Login], Any] | None,
        key: str,
        redirect_uri: str | None,
        after_login: str,
        after_logout: str,
        params: Mapping[str, str] | None,
        trusted_origins: Iterable[str] | None,
        allow_http: bool,
        timeout: float,
        name: str,
    ) -> None:
        if not callable(getattr(sessions, "decode", None)):
            raise TypeError("OAuthLogin needs the app's Sessions(...)")
        if str(sessions.same_site).lower() == "strict":
            raise ValueError(
                "the session cookie is SameSite=Strict, so a browser will not send it "
                "with the provider's redirect back to the callback, and no login can "
                "finish. Use Sessions(same_site='Lax'), the default"
            )
        if not isinstance(client_id, str) or not client_id:
            raise TypeError("client_id is the id the provider gave this app")
        if client_secret is not None and (not isinstance(client_secret, str) or not client_secret):
            raise TypeError("client_secret is a str, or None for a public client")
        if isinstance(scopes, str):
            raise TypeError("scopes is a list: scopes=['openid', 'email']")
        scopes = tuple(scopes)
        if any(not isinstance(s, str) or not s or " " in s for s in scopes):
            raise ValueError(f"each scope is one name without spaces, got {scopes!r}")
        if on_login is not None and not callable(on_login):
            raise TypeError(f"on_login= needs a function, got {type(on_login).__name__}")
        if not isinstance(key, str) or not key:
            raise TypeError("key is the session key the login is kept under")
        if redirect_uri is not None:
            _check_redirect(redirect_uri, allow_http)
        for what, value in (("after_login", after_login), ("after_logout", after_logout)):
            if _local(value) is None:
                raise ValueError(f"{what} is a path on this site, such as '/', got {value!r}")
        params = dict(params or {})
        clash = sorted(_RESERVED & set(params))
        if clash:
            raise ValueError(f"params= cannot set {clash}: the login flow sets them itself")
        if not all(isinstance(v, str) for v in params.values()):
            raise TypeError("params= values are strings")
        _segment(name, "name")
        self.client_id = client_id
        self.client_secret = client_secret
        self.sessions = sessions
        self.scopes = scopes
        self.on_login = on_login
        self.key = key
        self.redirect_uri = redirect_uri
        self.after_login = after_login
        self.after_logout = after_logout
        self.params = params
        self.allow_http = allow_http
        self.timeout = timeout
        self.name = name
        #: How the token endpoint is told who is asking: HTTP Basic, which
        #: every provider must accept, or the form body, which GitHub wants.
        self.token_auth = "basic"
        # Logout is refused across sites by the rule a session-protected POST
        # is, so another site's page cannot log a person out.
        self._guard = SessionAuth(sessions, key=key, trusted_origins=trusted_origins, name=name)
        self.verifier: OIDC | None = None
        self._authorize_url: str | None = None
        self._token_url: str | None = None

    def _openid(self, verifier: OIDC) -> None:
        if "openid" not in self.scopes:
            raise ValueError(
                "an OpenID Connect login asks for the 'openid' scope: without it there "
                "is no ID token to say who logged in"
            )
        self.verifier = verifier
        self.issuer = verifier.issuer

    def __repr__(self) -> str:
        return f"OAuthLogin({self.name})"

    async def load(self) -> None:
        """Fetch the provider's discovery document and keys now, raising if
        that fails: from a lifespan, to find an unreachable provider at startup."""
        if self.verifier is not None:
            await self.verifier.load()

    # ---- providers ----------------------------------------------------------------

    @classmethod
    def google(cls, *, client_id: str, client_secret: str, sessions: Any,
               **options: Any) -> "OAuthLogin":
        """Sign in with Google.

        The OAuth client comes from the Google Cloud console, as a "Web
        application" with the callback URL as an authorized redirect URI.
        `claims["hd"]` is the Workspace domain, for an app that admits one
        organisation's accounts only. `params={"access_type": "offline"}`
        asks for a refresh token.
        """
        options.setdefault("name", "google")
        flow = cls("https://accounts.google.com", client_id=client_id,
                   client_secret=client_secret, sessions=sessions, **options)
        # Google's ID tokens name their issuer in either form.
        flow.verifier._issuers = ["https://accounts.google.com", "accounts.google.com"]
        return flow

    @classmethod
    def microsoft(
        cls,
        *,
        client_id: str,
        client_secret: str,
        sessions: Any,
        tenant: str = "common",
        authority: str = "https://login.microsoftonline.com",
        **options: Any,
    ) -> "OAuthLogin":
        """Sign in with Microsoft: work and school accounts, personal ones, or both.

        `tenant` says whose accounts may log in: `common` (the default) any
        Microsoft account, `organizations` any work or school account,
        `consumers` personal accounts only, or a directory's tenant id for
        that organisation alone. For the first two the ID token's issuer
        names the person's own tenant, and is checked against its `tid`
        claim. `authority` is for a national cloud.

        Microsoft does not verify the `email` claim, so `email_verified` is
        False: key accounts on `subject`, and `claims["tid"]` with it for a
        multi-tenant app.
        """
        options.setdefault("name", "microsoft")
        authority = _bare(authority, "authority", scheme=True)
        tenant = _segment(tenant, "tenant").lower()
        verifier_options = {
            "allow_http": options.get("allow_http", False),
            "timeout": options.get("timeout", 10),
            "cache_size": 0,
            "name": options["name"],
        }
        discovery = f"{authority}/{tenant}/v2.0/.well-known/openid-configuration"
        if tenant in ("common", "organizations"):
            verifier: OIDC = _AnyTenant(
                authority, discovery, refuse_consumers=tenant == "organizations",
                audience=client_id, **verifier_options,
            )
        else:
            if tenant != "consumers" and not _GUID.fullmatch(tenant):
                raise ValueError(
                    f"tenant is 'common', 'organizations', 'consumers' or a tenant id "
                    f"(a GUID); got {tenant!r}. A domain name does not name one issuer"
                )
            tid = _CONSUMERS if tenant == "consumers" else tenant
            verifier = OIDC(f"{authority}/{tid}/v2.0", audience=client_id,
                            discovery_url=discovery, **verifier_options)
        flow = cls.__new__(cls)
        flow._setup(
            client_id=client_id, client_secret=client_secret, sessions=sessions,
            **{"scopes": ("openid", "email", "profile"), **_common(options)},
        )
        flow._openid(verifier)
        return flow

    @classmethod
    def keycloak(cls, url: str, realm: str, *, client_id: str, client_secret: str | None,
                 sessions: Any, **options: Any) -> "OAuthLogin":
        """A Keycloak realm: `OAuthLogin.keycloak("https://sso.example.com", "acme", ...)`.

        The client needs the standard flow enabled, the callback URL among its
        valid redirect URIs, and PKCE's `S256` method allowed.
        """
        options.setdefault("name", "keycloak")
        issuer = f"{_bare(url, 'url', scheme=True)}/realms/{_segment(realm, 'realm')}"
        return cls(issuer, client_id=client_id, client_secret=client_secret,
                   sessions=sessions, **options)

    @classmethod
    def github(
        cls,
        *,
        client_id: str,
        client_secret: str,
        sessions: Any,
        server: str = "https://github.com",
        api: str = "https://api.github.com",
        **options: Any,
    ) -> "OAuthLogin":
        """Sign in with GitHub, through an OAuth app.

        GitHub is OAuth 2 without OpenID Connect: there is no ID token, so
        who logged in is read from its API with the access token instead.
        `subject` is the account's numeric id as a string — the login name
        can be changed and then taken by someone else. The email is the
        primary verified address when the `user:email` scope is granted.

        For GitHub Enterprise Server, `server="https://github.example.com"`
        and `api="https://github.example.com/api/v3"`.
        """
        options.setdefault("name", "github")
        allow_http = options.get("allow_http", False)
        server = _bare(server, "server", scheme=True)
        api = _bare(api, "api", scheme=True)
        _jwks.check_url(server, allow_http, "server")
        _jwks.check_url(api, allow_http, "api")
        flow = cls.__new__(_GitHub)
        flow._setup(
            client_id=client_id, client_secret=client_secret, sessions=sessions,
            **{"scopes": ("read:user", "user:email"), **_common(options)},
        )
        flow.token_auth = "post"
        flow._authorize_url = f"{server}/login/oauth/authorize"
        flow._token_url = f"{server}/login/oauth/access_token"
        flow.api = api
        flow.issuer = None
        return flow

    # ---- routes -------------------------------------------------------------------

    def routes(
        self,
        prefix: str = "",
        *,
        login: str = "/login",
        callback: str = "/callback",
        logout: str = "/logout",
    ) -> Any:
        """A router with the three routes, all public: include it in the app.

        `GET login` starts a login, `GET callback` is where the provider sends
        the person back, and `POST logout` ends the session. Two providers
        need two prefixes, such as `/auth/google` and `/auth/github`.
        """
        from ._routers import Router

        for path in (login, callback, logout):
            if not isinstance(path, str) or not path.startswith("/") or "{" in path:
                raise ValueError(f"a login route is a fixed path starting with '/', got {path!r}")
        # Public whatever the app's default: nobody is logged in yet.
        router = Router(prefix=prefix, auth=None)
        # The routes write the session, so they carry its middleware: an app
        # that installed it too is not written twice, since each layer
        # writes only what its own handlers changed.
        router.middleware(self.sessions.middleware)
        flow = self

        async def begin(request):
            return await flow._begin(request, login, callback)

        async def finish(request):
            return await flow._finish(request)

        async def end(request):
            return await flow._logout(request)

        # Named after the provider: a handler's name is its OpenAPI
        # operationId, and `google_login` reads better in a generated client
        # than the `login_get_auth_google_login` two same-named ones would get.
        for fn, action in ((begin, "login"), (finish, "callback"), (end, "logout")):
            fn.__name__ = fn.__qualname__ = f"{self.name}_{action}"
        router.get(login)(begin)
        router.get(callback)(finish)
        router.post(logout)(end)
        return router

    # ---- the flow -----------------------------------------------------------------

    async def _endpoints(self) -> tuple[str, str, bool]:
        """Where to send the person, where to exchange the code, and whether
        the provider promises an `iss` parameter on its callback."""
        if self.verifier is None:
            return self._authorize_url, self._token_url, False
        try:
            document = await self.verifier.keys.discovered()
        except _jwks.Unavailable as exc:
            logger.warning("login: provider unavailable: %s", exc, extra={"scheme": self.name})
            raise LoginFailed(503, "the identity provider could not be reached") from None
        if self._token_url is None:
            authorize, token = (document.get("authorization_endpoint"),
                                document.get("token_endpoint"))
            if not isinstance(authorize, str) or not isinstance(token, str):
                logger.warning("login: discovery has no authorization or token endpoint",
                               extra={"scheme": self.name})
                raise LoginFailed(502, "the identity provider is misconfigured")
            try:
                _jwks.check_url(authorize, self.allow_http, "authorization_endpoint")
                _jwks.check_url(token, self.allow_http, "token_endpoint")
            except ValueError as exc:
                logger.warning("login: %s", exc, extra={"scheme": self.name})
                raise LoginFailed(502, "the identity provider is misconfigured") from None
            self._authorize_url, self._token_url = authorize, token
        promised = document.get("authorization_response_iss_parameter_supported") is True
        return self._authorize_url, self._token_url, promised

    def _redirect(self, request: Any, login: str, callback: str) -> str:
        if self.redirect_uri is not None:
            return self.redirect_uri
        from ._auth import public_base

        base = public_base(request)
        path = request.path
        if base is None or not path.endswith(login):
            raise LoginFailed(400, "the login address could not be worked out")
        return base + path[: len(path) - len(login)] + callback

    def _target(self, request: Any) -> str:
        wanted = [v for k, v in urllib.parse.parse_qsl(request.query or "") if k == "next"]
        return (_local(wanted[0]) if len(wanted) == 1 else None) or self.after_login

    async def _begin(self, request: Any, login: str, callback: str) -> Response:
        target = self._target(request)
        redirect_uri = self._redirect(request, login, callback)
        authorize, _, _ = await self._endpoints()
        session = self.sessions.load(request)
        pending = _pending(session)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)  # 64 characters; RFC 7636 wants 43 to 128
        nonce = secrets.token_urlsafe(32) if self.verifier is not None else None
        pending[state] = [self.name, verifier, nonce, target, redirect_uri, int(time.time())]
        while len(pending) > PENDING_LIMIT:
            oldest = min(pending, key=lambda s: pending[s][5])
            del pending[oldest]
        session[PENDING_KEY] = pending
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            **({"nonce": nonce} if nonce is not None else {}),
            **self.params,
        }
        joiner = "&" if "?" in authorize else "?"
        return Response(status=302, content_type="text/plain", headers={
            "location": f"{authorize}{joiner}{urllib.parse.urlencode(query)}",
            "cache-control": "no-store",
        })

    def _refuse(self, status: int, detail: str, reason: str) -> LoginFailed:
        logger.info("login refused: %s", reason, extra={"scheme": self.name})
        return LoginFailed(status, detail)

    def _issuer_ok(self, value: str) -> bool:
        accepts = getattr(self.verifier, "accepts_issuer", None)
        if accepts is not None:
            return accepts(value)
        issuers = self.verifier._issuers
        return value in issuers if isinstance(issuers, list) else value == issuers

    async def _finish(self, request: Any) -> Response:
        params: dict[str, str] = {}
        for name, value in urllib.parse.parse_qsl(request.query or "", keep_blank_values=True):
            if name in params:
                raise self._refuse(400, "this login could not be completed",
                                   f"callback repeats {name!r}")
            params[name] = value
        session = self.sessions.load(request)
        pending = _pending(session)
        state = params.get("state")
        # Popped whatever happens next: a callback is good once, even one that
        # then fails, so a replay finds nothing.
        entry = pending.pop(state, None) if state else None
        if entry is not None:
            session[PENDING_KEY] = pending
        if entry is None or entry[0] != self.name:
            raise self._refuse(
                400, "this login was not started here, has expired, or was already used",
                "state unknown" if state else "no state",
            )
        _, verifier, nonce, target, redirect_uri, _ = entry
        _, token_url, promised = await self._endpoints()
        if self.verifier is not None:
            iss = params.get("iss")
            if (iss is not None or promised) and (iss is None or not self._issuer_ok(iss)):
                raise self._refuse(400, "this login could not be completed",
                                   f"callback from issuer {iss!r}")
        if "error" in params:
            error = params["error"]
            logger.info("login refused by the provider: %.64s", error, extra={"scheme": self.name})
            if error == "access_denied":
                raise LoginFailed(403, "the login was cancelled or refused")
            raise LoginFailed(400, "the identity provider did not complete the login")
        code = params.get("code")
        if not code:
            raise self._refuse(400, "this login could not be completed", "callback has no code")
        tokens = await self._exchange(token_url, code, verifier, redirect_uri)
        who = await self._identify(tokens, nonce)
        if self.on_login is None:
            value: Any = f"{self.name}:{who.subject}"
        else:
            value = await _call(self.on_login, request, who)
        if value is None:
            raise self._refuse(403, "this account may not log in here",
                               "on_login returned None")
        # A fresh session: whatever was in it before — put there by this
        # browser, or planted in it — does not carry over into the login.
        session.clear()
        session[self.key] = value
        logger.info("login", extra={"scheme": self.name})
        return Response(status=303, content_type="text/plain",
                        headers={"location": target, "cache-control": "no-store"})

    async def _post(self, url: str, form: dict[str, str], headers: dict[str, str]) -> tuple:
        try:
            return await asyncio.to_thread(_jwks.call_json, url, form=form, headers=headers,
                                           timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - unreachable, slow or oversized are all one here
            logger.warning("login: %s unreachable: %s", url, type(exc).__name__,
                           extra={"scheme": self.name})
            raise LoginFailed(502, "the identity provider could not be reached") from None

    async def _exchange(self, url: str, code: str, verifier: str, redirect_uri: str) -> dict:
        form = {"grant_type": "authorization_code", "code": code,
                "redirect_uri": redirect_uri, "code_verifier": verifier}
        headers: dict[str, str] = {}
        if self.token_auth == "basic" and self.client_secret is not None:
            # RFC 6749 2.3.1: each half form-encoded before they are joined.
            pair = (f"{urllib.parse.quote_plus(self.client_id)}:"
                    f"{urllib.parse.quote_plus(self.client_secret)}")
            headers["authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        else:
            form["client_id"] = self.client_id
            if self.client_secret is not None:
                form["client_secret"] = self.client_secret
        status, document = await self._post(url, form, headers)
        # GitHub answers a bad code with 200 and an `error` member.
        if (status != 200 or not isinstance(document, dict) or "error" in document
                or not isinstance(document.get("access_token"), str)):
            error = document.get("error") if isinstance(document, dict) else None
            # The provider's fault is 502; a code it refused is this login's.
            raise self._refuse(
                502 if status >= 500 or not (error or 400 <= status < 500) else 400,
                "this login could not be completed",
                f"token endpoint answered {status} {str(error)[:64] if error else ''}".rstrip(),
            )
        return document

    async def _identify(self, tokens: dict, nonce: str | None) -> Login:
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise self._refuse(502, "the identity provider did not say who logged in",
                               "no id_token in the token response")
        try:
            found = await self.verifier.check(id_token)
        except Unauthenticated:
            # The verifier has logged why.
            raise self._refuse(400, "this login could not be completed",
                               "id_token did not verify") from None
        except HTTPError:
            raise LoginFailed(503, "the identity provider could not be reached") from None
        claims = found.claims
        offered = claims.get("nonce")
        if not isinstance(offered, str) or not hmac.compare_digest(offered, nonce or ""):
            raise self._refuse(400, "this login could not be completed",
                               "id_token nonce is not this login's")
        audience = claims.get("aud")
        if (isinstance(audience, (list, tuple)) and len(audience) > 1
                and claims.get("azp") != self.client_id):
            raise self._refuse(400, "this login could not be completed",
                               "id_token for several audiences, and not authorized for this one")
        email = claims.get("email")
        name = claims.get("name") or claims.get("preferred_username")
        return Login(
            provider=self.name,
            subject=found.subject,
            email=email if isinstance(email, str) else None,
            email_verified=claims.get("email_verified") is True,
            name=name if isinstance(name, str) else None,
            claims=claims,
            tokens=_frozen(tokens),
        )

    async def _logout(self, request: Any) -> Response:
        raw = request.cookies.get(self.sessions.cookie)
        data = self.sessions.decode(raw) if raw else {}
        if data:
            reason = self._guard._forged(request, data)
            if reason is not None:
                logger.warning("logout refused: %s", reason,
                               extra={"scheme": self.name, "path": request.path})
                raise Forbidden("cross-site request refused")
            self.sessions.load(request).clear()
        return Response(status=303, content_type="text/plain",
                        headers={"location": self.after_logout, "cache-control": "no-store"})


def _common(options: dict[str, Any]) -> dict[str, Any]:
    """The options every provider takes, from a preset's keyword arguments."""
    allowed = {
        "scopes", "on_login", "key", "redirect_uri", "after_login", "after_logout", "params",
        "trusted_origins", "allow_http", "timeout", "name",
    }
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise TypeError(f"unexpected keyword argument(s) {unknown}")
    defaults = {"on_login": None, "key": "user_id", "redirect_uri": None, "after_login": "/",
                "after_logout": "/", "params": None, "trusted_origins": None,
                "allow_http": False, "timeout": 10}
    return {**defaults, **options}


class _GitHub(OAuthLogin):
    """GitHub: the user from its API, since there is no ID token."""

    api: str

    async def _identify(self, tokens: dict, nonce: str | None) -> Login:
        headers = {
            "authorization": f"Bearer {tokens['access_token']}",
            "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
        }
        try:
            status, user = await asyncio.to_thread(
                _jwks.call_json, f"{self.api}/user", headers=headers, timeout=self.timeout
            )
        except Exception as exc:  # noqa: BLE001 - as in _post
            logger.warning("login: github api unreachable: %s", type(exc).__name__,
                           extra={"scheme": self.name})
            raise LoginFailed(502, "the identity provider could not be reached") from None
        uid = user.get("id") if isinstance(user, dict) else None
        if status != 200 or not isinstance(uid, int) or isinstance(uid, bool):
            raise self._refuse(502, "the identity provider did not say who logged in",
                               f"github /user answered {status}")
        email, verified = None, False
        try:
            status, emails = await asyncio.to_thread(
                _jwks.call_json, f"{self.api}/user/emails", headers=headers, timeout=self.timeout
            )
        except Exception:  # noqa: BLE001 - the email is optional; the login is not
            status, emails = 0, None
        if status == 200 and isinstance(emails, list):
            usable = [e for e in emails if isinstance(e, dict) and e.get("verified") is True
                      and isinstance(e.get("email"), str)]
            usable.sort(key=lambda e: e.get("primary") is not True)
            if usable:
                email, verified = usable[0]["email"], True
        name = user.get("name") or user.get("login")
        return Login(
            provider=self.name,
            subject=str(uid),
            email=email,
            email_verified=verified,
            name=name if isinstance(name, str) else None,
            claims=_frozen(user),
            tokens=_frozen(tokens),
        )


class _AnyTenant(OIDC):
    """Microsoft's `common` and `organizations`: one discovery document, whose
    issuer is a template, for ID tokens that each name their own tenant."""

    def __init__(self, authority: str, discovery_url: str, *, refuse_consumers: bool,
                 **options: Any) -> None:
        self.template = f"{authority}/{{tenantid}}/v2.0"
        super().__init__(self.template, discovery_url=discovery_url, **options)
        self.refuse_consumers = refuse_consumers
        # Checked per token below, against the token's own tenant.
        self._issuers = None

    def accepts_issuer(self, value: str) -> bool:
        prefix, _, rest = self.template.partition("{tenantid}")
        if not value.startswith(prefix) or not value.endswith(rest):
            return False
        tid = value[len(prefix): len(value) - len(rest)]
        return bool(_GUID.fullmatch(tid)) and not (self.refuse_consumers and tid == _CONSUMERS)

    def _principal(self, claims: dict[str, Any]) -> Any:
        tid = claims.get("tid")
        iss = claims.get("iss")
        if (not isinstance(tid, str) or not isinstance(iss, str) or not self.accepts_issuer(iss)
                or iss != self.template.replace("{tenantid}", tid)):
            logger.info("jwt refused: issuer does not match tenant", extra={"scheme": self.name})
            raise Unauthenticated("token invalid")
        return super()._principal(claims)


__all__ = ["Login", "LoginFailed", "OAuthLogin"]
