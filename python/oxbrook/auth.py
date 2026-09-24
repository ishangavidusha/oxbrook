"""Authentication: who is calling, and whether they may.

    from oxbrook import App, Depends, Request, Router
    from oxbrook.auth import APIKey, JWT, Principal, optional, principal

    tokens = JWT(key=SECRET, algorithms=["HS256"], audience="notes")
    keys = APIKey(header="x-api-key", verify=lookup_key)

    app = App(auth=tokens | keys)                          # every route, by default

    @app.get("/health", auth=None)                         # public, on purpose
    async def health(_: Request): ...

    @app.post("/notes", auth=tokens.requires("notes:write"))
    async def create(_: Request, note: NoteIn, who: Principal = Depends(principal)): ...

Three ideas. A **scheme** finds a credential in a request and checks it. A
**principal** is who it found. A **requirement** is what a route demands of
that principal. `auth=` on an app, a router, a route or a WebSocket declares
all three at once, and the nearest declaration wins.

**The rules the framework enforces.** Schemes are tried in the order written,
and the first one that finds a credential of its kind decides: a credential
that is present and wrong answers `401` and never falls through to the next
scheme or to anonymous access. Missing or wrong credentials are `401`, with a
`WWW-Authenticate` challenge for each scheme that has one; a principal that
fails a requirement is `403`. Authentication runs before the request body is
read and before any router middleware or handler, so a refused caller never
makes the server take in what they sent.

A scheme is any object with `async authenticate(request)` returning a
`Principal`, returning None when the request carries no credential of its kind,
and raising `Unauthenticated` when it carries one that is wrong. Subclass
`Scheme` to get `|` and `.requires(...)` as well.
"""

import base64
import binascii
import hashlib
import inspect
import logging
import secrets
import types
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ._errors import HTTPError

logger = logging.getLogger("oxbrook.auth")

#: Where the principal is kept in `request.locals`.
PRINCIPAL = "principal"


@dataclass(frozen=True, slots=True)
class Principal:
    """Who a scheme found: the same shape whatever the credential was.

    `subject` identifies the caller — a user id, a key id, a client id — and
    is a string, so `str(user.id)` rather than the id itself. `scheme` is the
    name of the scheme that found it, filled in by the framework when left
    empty. `scopes` are what the credential grants, which `requires(...)`
    checks. `claims` is anything else the credential carried, and `user`
    whatever a verify function looked up, if anything.
    """

    subject: str
    scheme: str = ""
    scopes: frozenset[str] = frozenset()
    claims: Mapping[str, Any] = field(default_factory=dict)
    user: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not self.subject:
            raise TypeError(
                f"a Principal's subject is a non-empty str, got {self.subject!r}; "
                f"pass str(user.id) for a numeric id"
            )
        if isinstance(self.scopes, str):
            # frozenset("admin") is five one-letter scopes, which would pass a
            # requirement for "a" and fail one for "admin" without a word.
            raise TypeError(
                "a Principal's scopes are a collection of names, not one string; "
                "split a space-separated scope claim first"
            )
        if not isinstance(self.scopes, frozenset):
            object.__setattr__(self, "scopes", frozenset(self.scopes))


class Unauthenticated(HTTPError):
    """`401`: the request carries no acceptable credential.

    Raised by a scheme for a credential that is present and wrong, which stops
    the search: the next scheme is not tried. `detail` reaches the client, so
    say what kind of wrong it is — expired, invalid — and log the rest.
    """

    def __init__(
        self, detail: str | None = None, headers: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(401, detail, headers, **kwargs)


class Forbidden(HTTPError):
    """`403`: the caller is known, and may not do this.

    Raised by the framework when a principal fails a requirement, and the way
    a handler refuses a rule about a particular record: `raise Forbidden()`
    when the note belongs to someone else. Retrying with the same credential
    will not help, which is what separates it from `401`.
    """

    def __init__(
        self,
        detail: str | None = None,
        headers: dict[str, Any] | None = None,
        *,
        scopes: Iterable[str] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(403, detail, headers, **kwargs)
        #: The scopes the caller lacked, when that is why.
        self.scopes = tuple(scopes)


# ---- declaring --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Requirement:
    """What `.requires(...)` asks of a principal."""

    all_of: frozenset[str] = frozenset()
    any_of: frozenset[str] = frozenset()
    check: Callable[..., Any] | None = None

    async def met(self, found: Principal, request: Any) -> bool:
        if not self.all_of <= found.scopes:
            return False
        if self.any_of and not self.any_of & found.scopes:
            return False
        if self.check is not None:
            return bool(await _call(self.check, found, request))
        return True

    def missing(self, found: Principal) -> list[str]:
        """The scopes that would have satisfied it, for the challenge."""
        lacking = sorted(self.all_of - found.scopes)
        if self.any_of and not self.any_of & found.scopes:
            lacking.extend(sorted(self.any_of))
        return lacking

    def __repr__(self) -> str:
        parts = [repr(s) for s in sorted(self.all_of)]
        if self.any_of:
            parts.append(f"any_of={tuple(sorted(self.any_of))!r}")
        if self.check is not None:
            parts.append(f"check={getattr(self.check, '__name__', self.check)}")
        return ", ".join(parts)


def _requirement(scopes: tuple, any_of: Iterable[str], check: Any) -> Requirement:
    if isinstance(any_of, str):
        raise TypeError("any_of is a collection of scopes, not one string: any_of=('a', 'b')")
    any_of = tuple(any_of)
    for scope in (*scopes, *any_of):
        if not isinstance(scope, str) or not scope:
            raise TypeError(f"a scope is a non-empty str, got {scope!r}")
    if check is not None and not callable(check):
        raise TypeError(f"check= needs a callable, got {type(check).__name__}")
    if not scopes and not any_of and check is None:
        raise TypeError("requires() needs scopes, any_of=, or check=")
    return Requirement(frozenset(scopes), frozenset(any_of), check)


class _Combinable:
    """`|` and `.requires(...)`, for schemes and for what they combine into."""

    def requires(
        self, *scopes: str, any_of: Iterable[str] = (), check: Any = None
    ) -> "_Combinable":
        """This, plus a requirement on the principal it finds.

            tokens.requires("notes:write")                 # every one of these
            tokens.requires(any_of=("admin", "support"))   # at least one
            tokens.requires(check=is_staff)                # async (principal, request) -> bool

        A principal that fails it is refused with `403`. On `a | b` it applies
        to both; on one side of a `|`, to that side alone.
        """
        return _Required(self, _requirement(scopes, any_of, check))

    def __or__(self, other: Any) -> "_Combinable":
        return _Either((*_members(self), *_members(_checked(other, "|"))))

    def __ror__(self, other: Any) -> "_Combinable":
        return _Either((*_members(_checked(other, "|")), *_members(self)))


class Scheme(_Combinable):
    """A way of finding and checking a credential.

    Subclass it and write `authenticate`:

        class Signature(Scheme):
            name = "signature"

            async def authenticate(self, request):
                signature = request.header("x-signature")
                if signature is None:
                    return None                       # not mine: try the next scheme
                if not valid(signature, await request.read()):
                    raise Unauthenticated("bad signature")   # mine, and wrong
                return Principal(subject="webhook")

    `challenge` gives the `WWW-Authenticate` header sent with a refusal, and
    `openapi` the entry in the OpenAPI document's `securitySchemes`; both are
    optional.
    """

    #: Names the scheme in a principal and in OpenAPI.
    name: str

    async def authenticate(self, request: Any) -> Principal | None:
        raise NotImplementedError

    def challenge(self, error: HTTPError | None) -> str | None:
        """The `WWW-Authenticate` value for a refusal, or None for none.

        `error` is the `Unauthenticated` this scheme raised, the `Forbidden`
        its principal earned, or None when no credential of its kind was sent.
        """
        return None

    def openapi(self) -> dict[str, Any] | None:
        """This scheme's OpenAPI `securitySchemes` entry, or None."""
        return None

    def __repr__(self) -> str:
        return getattr(self, "name", type(self).__name__)


class _Either(_Combinable):
    def __init__(self, members: tuple) -> None:
        self.members = members

    def __repr__(self) -> str:
        return " | ".join(repr(m) for m in self.members)


class _Required(_Combinable):
    def __init__(self, inner: Any, requirement: Requirement) -> None:
        self.inner = inner
        self.requirement = requirement

    def __repr__(self) -> str:
        inner = f"({self.inner!r})" if isinstance(self.inner, _Either) else repr(self.inner)
        return f"{inner}.requires({self.requirement!r})"


class _Optional:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __repr__(self) -> str:
        return f"optional({self.inner!r})"


def optional(auth: Any) -> _Optional:
    """Accept anonymous callers too.

        @app.get("/feed", auth=optional(tokens))
        async def feed(_: Request, who=Depends(principal)): ...   # None if anonymous

    Only a request with no credential is anonymous. One carrying a wrong
    credential is still refused: an expired token is not the same as none, and
    treating it so would hide from the client that it needs a new one.
    """
    if isinstance(auth, _Optional):
        raise TypeError("optional() of optional() is the same thing; use one")
    return _Optional(_checked(auth, "optional()"))


def _checked(value: Any, where: str) -> Any:
    if isinstance(value, _Combinable):
        return value
    if isinstance(value, _Optional):
        raise TypeError(
            f"{where} cannot take optional(...): it admits anonymous callers, which "
            f"only makes sense for the whole declaration. Write optional(a | b)"
        )
    authenticate = getattr(value, "authenticate", None)
    if not inspect.iscoroutinefunction(authenticate):
        raise TypeError(
            f"{where} needs a scheme: an object with `async def authenticate(request)`, "
            f"got {type(value).__name__}"
        )
    return value


def _members(value: Any) -> tuple:
    return value.members if isinstance(value, _Either) else (value,)


def principal(request: Any) -> Principal | None:
    """Who authentication found for this request, or None.

    A dependency, `who: Principal = Depends(principal)`, and a plain function
    for middleware. None on a route with no `auth=`, and for an anonymous
    caller on one declared `optional(...)`.
    """
    return request.locals.get(PRINCIPAL)


# ---- shipped schemes --------------------------------------------------------


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    result = fn(*args)
    if inspect.isawaitable(result):
        result = await result
    return result


def _verifier(fn: Any, what: str) -> Any:
    if not callable(fn):
        raise TypeError(f"{what} needs verify=, a function; got {type(fn).__name__}")
    return fn


def _found(value: Any, scheme: Any) -> Principal:
    if not isinstance(value, Principal):
        raise TypeError(
            f"{scheme!r}: verify returned {type(value).__name__}; return a Principal, "
            f"or None for a credential that is not valid"
        )
    return value


def _authorization(request: Any, kind: str) -> str | None:
    """The credentials after `kind` in `Authorization`, None if another kind.

    Only the header: a token in the query string ends up in proxy logs and
    browser history, so it is not looked for, and a request that puts one
    there is simply unauthenticated.
    """
    header = request.header("authorization")
    if header is None:
        return None
    scheme, _, credentials = header.strip().partition(" ")
    if scheme.lower() != kind:
        return None
    return credentials.strip()


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Bearer(Scheme):
    """A token in `Authorization: Bearer <token>`, checked by `verify`.

        async def check_token(token):          # -> Principal, or None if invalid
            record = await tokens.lookup(token)
            return None if record is None else Principal(subject=record.user_id)

        bearer = Bearer(verify=check_token)

    For tokens this app can check itself, `JWT` does the verifying. `verify` is
    for everything else: a provider SDK, an opaque token looked up in a
    database or introspected at the issuer.

    Refusals carry an RFC 6750 challenge: `invalid_token` for a token that did
    not verify, `insufficient_scope` with the scopes needed for a `403`.
    """


    def __init__(
        self,
        verify: Callable[[str], Any] | None = None,
        *,
        realm: str | None = None,
        name: str = "bearer",
        bearer_format: str | None = None,
    ) -> None:
        if verify is None and type(self) is Bearer:
            raise TypeError("Bearer needs verify=, a function from token to Principal or None")
        self.verify = verify
        self.realm = realm
        self.name = name
        self.bearer_format = bearer_format

    async def authenticate(self, request: Any) -> Principal | None:
        token = _authorization(request, "bearer")
        if token is None:
            return None
        if not token:
            raise Unauthenticated("token invalid")
        return await self.check(token)

    async def check(self, token: str) -> Principal:
        found = await _call(self.verify, token)
        if found is None:
            logger.info("bearer token refused by verify", extra={"scheme": self.name})
            raise Unauthenticated("token invalid")
        return _found(found, self)

    def challenge(self, error: HTTPError | None) -> str:
        params = []
        if self.realm is not None:
            params.append(f"realm={_quoted(self.realm)}")
        if isinstance(error, Forbidden):
            params.append('error="insufficient_scope"')
            if error.scopes:
                params.append(f"scope={_quoted(' '.join(error.scopes))}")
        elif error is not None:
            params.append('error="invalid_token"')
            if error.detail:
                params.append(f"error_description={_quoted(error.detail)}")
        return "Bearer" + (" " + ", ".join(params) if params else "")

    def openapi(self) -> dict[str, Any]:
        entry = {"type": "http", "scheme": "bearer"}
        if self.bearer_format:
            entry["bearerFormat"] = self.bearer_format
        return entry


_HMAC = {"HS256": 32, "HS384": 48, "HS512": 64}
_ASYMMETRIC = {
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
    "ES256", "ES256K", "ES384", "ES512", "EdDSA",
}


class JWT(Bearer):
    """A JSON Web Token this app can verify itself.

        tokens = JWT(key=os.environ["JWT_SECRET"], algorithms=["HS256"], audience="notes")
        tokens = JWT(key=public_key_pem, algorithms=["RS256"], audience="notes",
                     issuer="https://login.example.com/")

    Checked every time: the signature, `exp` with `leeway` seconds of clock
    skew, `nbf`, `aud` against `audience`, and `iss` against `issuer` when one
    is given. The principal's subject is the `sub` claim and its scopes the
    `scope` claim, a space-separated string or a list; `scopes_claim` names a
    different claim, such as `"scp"` or `"permissions"`.

    `algorithms` is required, and cannot mix families: an HMAC secret with
    `HS*`, or a public key with the asymmetric ones. Accepting both is how a
    public key ends up used as an HMAC secret to forge a token, and `none` is
    never accepted. An HMAC secret must be at least as long as the hash.

    `audience=None` accepts a token for any audience, and has to be written
    out: a token issued for another service is otherwise accepted by this one.

    Needs PyJWT: `pip install 'oxbrook[auth]'`.
    """


    def __init__(
        self,
        key: Any,
        *,
        algorithms: Iterable[str],
        audience: str | Iterable[str] | None,
        issuer: str | None = None,
        leeway: float = 60,
        scopes_claim: str = "scope",
        subject_claim: str = "sub",
        realm: str | None = None,
        name: str = "jwt",
    ) -> None:
        try:
            import jwt
        except ModuleNotFoundError:  # pragma: no cover - depends on the environment
            raise ModuleNotFoundError(
                "JWT needs PyJWT, with cryptography for public keys: "
                "pip install 'oxbrook[auth]'"
            ) from None
        super().__init__(realm=realm, name=name, bearer_format="JWT")
        self._jwt = jwt
        if isinstance(algorithms, str):
            raise TypeError("algorithms is a list: algorithms=['HS256']")
        algorithms = list(algorithms)
        if not algorithms:
            raise ValueError("JWT needs at least one algorithm")
        if any(a.lower() == "none" for a in algorithms):
            raise ValueError("the 'none' algorithm means an unsigned token, and is never accepted")
        unknown = [a for a in algorithms if a not in _HMAC and a not in _ASYMMETRIC]
        if unknown:
            raise ValueError(f"unknown JWT algorithm(s) {unknown}")
        hmac_algorithms = [a for a in algorithms if a in _HMAC]
        if hmac_algorithms and len(hmac_algorithms) != len(algorithms):
            raise ValueError(
                "algorithms cannot mix HMAC (HS*) with public-key algorithms: a public "
                "key accepted as an HMAC secret lets anyone who has it forge a token"
            )
        if hmac_algorithms:
            if not isinstance(key, (str, bytes)):
                raise TypeError("an HS* algorithm needs the shared secret, as str or bytes")
            secret = key.encode() if isinstance(key, str) else key
            if secret.lstrip().startswith(b"-----BEGIN"):
                raise ValueError(
                    "that key is a PEM key, not a shared secret; use an RS*, PS*, ES* "
                    "or EdDSA algorithm with it"
                )
            needed = max(_HMAC[a] for a in hmac_algorithms)
            if len(secret) < needed:
                raise ValueError(
                    f"an HMAC secret for {'/'.join(hmac_algorithms)} must be at least "
                    f"{needed} bytes; this one is {len(secret)}. "
                    f"secrets.token_urlsafe({needed}) makes one"
                )
        if audience is not None and not isinstance(audience, str):
            audience = list(audience)
        self.key = key
        self.algorithms = algorithms
        self.audience = audience
        self.issuer = issuer
        self.leeway = leeway
        self.scopes_claim = scopes_claim
        self.subject_claim = subject_claim
        self.require = ["exp", subject_claim] + (["iss"] if issuer else [])

    async def check(self, token: str) -> Principal:
        jwt = self._jwt
        try:
            claims = jwt.decode(
                token,
                self.key,
                algorithms=self.algorithms,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": self.require, "verify_aud": self.audience is not None},
            )
        except jwt.ExpiredSignatureError:
            logger.info("jwt refused: expired", extra={"scheme": self.name})
            raise Unauthenticated("token expired") from None
        except jwt.InvalidTokenError as exc:
            # The reason is for the log. The client learns only that the token
            # is invalid: which check failed is a map for forging the next one.
            logger.info("jwt refused: %s", type(exc).__name__, extra={"scheme": self.name})
            raise Unauthenticated("token invalid") from None

        subject = claims.get(self.subject_claim)
        if isinstance(subject, int) and not isinstance(subject, bool):
            subject = str(subject)
        if not isinstance(subject, str) or not subject:
            logger.info("jwt refused: no usable subject", extra={"scheme": self.name})
            raise Unauthenticated("token invalid")
        granted = claims.get(self.scopes_claim) or ()
        if isinstance(granted, str):
            granted = granted.split()
        scopes = frozenset(s for s in granted if isinstance(s, str))
        return Principal(
            subject=subject,
            scheme=self.name,
            scopes=scopes,
            claims=types.MappingProxyType(claims),
        )


class APIKey(Scheme):
    """A key in a header or a cookie, looked up by its hash.

        async def lookup_key(digest):        # -> Principal, or None if unknown
            row = await db.fetchrow("select * from api_keys where digest = $1", digest)
            return None if row is None else Principal(
                subject=row["id"], scopes=row["scopes"]
            )

        keys = APIKey(header="x-api-key", verify=lookup_key)

    `verify` receives the key's SHA-256 digest, never the key, so what is
    stored is the digest: a leaked table is not a list of working keys, and a
    lookup by digest cannot be timed to recover a key byte by byte. Store
    `APIKey.digest(key)` when issuing one; `APIKey.generate()` makes a key long
    enough that an unsalted hash is the right tool.

    Not the query string, which ends up in logs.
    """


    def __init__(
        self,
        *,
        header: str | None = None,
        cookie: str | None = None,
        verify: Callable[[str], Any],
        name: str = "api_key",
    ) -> None:
        if (header is None) == (cookie is None):
            raise TypeError("APIKey needs exactly one of header= or cookie=")
        self.header = header.lower() if header is not None else None
        self.cookie = cookie
        self.verify = _verifier(verify, "APIKey")
        self.name = name

    @staticmethod
    def digest(key: str) -> str:
        """What to store for `key`, and what `verify` is given to look up."""
        return hashlib.sha256(key.encode()).hexdigest()

    @staticmethod
    def generate(prefix: str = "") -> str:
        """A new random key: 256 bits, URL-safe, after `prefix`."""
        return prefix + secrets.token_urlsafe(32)

    async def authenticate(self, request: Any) -> Principal | None:
        if self.header is not None:
            raw = request.header(self.header)
        else:
            raw = request.cookies.get(self.cookie)
        if raw is None:
            return None
        raw = raw.strip()
        if not raw:
            raise Unauthenticated("API key invalid")
        found = await _call(self.verify, self.digest(raw))
        if found is None:
            logger.info("api key refused: unknown", extra={"scheme": self.name})
            raise Unauthenticated("API key invalid")
        return _found(found, self)

    def openapi(self) -> dict[str, Any]:
        if self.header is not None:
            return {"type": "apiKey", "in": "header", "name": self.header}
        return {"type": "apiKey", "in": "cookie", "name": self.cookie}


_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _secure(request: Any) -> bool:
    """Whether a Basic password reached this server without crossing a network
    in clear text: over TLS, from a proxy that terminated TLS, or on loopback.

    The forwarded header and the host can be written by the client. That is
    acceptable for what this guards against — a deployment that takes
    passwords over plain HTTP by mistake — since a client lying about either
    exposes nothing but its own password.
    """
    app = request.app
    if app is not None and getattr(app, "_serving_tls", False):
        return True
    forwarded = (request.header("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if forwarded == "https":
        return True
    host = (request.header("host") or "").strip().lower()
    if host.startswith("["):
        host = host[1:].partition("]")[0]
    else:
        host = host.rpartition(":")[0] if host.count(":") == 1 else host
    return host in _LOOPBACK


class Basic(Scheme):
    """A username and password in `Authorization: Basic ...`.

        async def check_password(username, password):   # -> Principal, or None
            user = await users.find(username)
            if user is None or not hasher.verify(user.password_hash, password):
                return None
            return Principal(subject=str(user.id), user=user)

        staff = Basic(verify=check_password)

    Refused over plain HTTP, since the password crosses the network readable.
    HTTPS, a proxy that sets `X-Forwarded-Proto: https`, and loopback are all
    accepted; `allow_http=True` accepts anything.

    Compare the password against a slow hash — argon2, bcrypt, scrypt — in
    `verify`: a password is not a random key, and a fast hash of one is
    cracked offline.
    """


    def __init__(
        self,
        verify: Callable[[str, str], Any],
        *,
        realm: str = "api",
        name: str = "basic",
        allow_http: bool = False,
    ) -> None:
        self.verify = _verifier(verify, "Basic")
        self.realm = realm
        self.name = name
        self.allow_http = allow_http

    async def authenticate(self, request: Any) -> Principal | None:
        encoded = _authorization(request, "basic")
        if encoded is None:
            return None
        try:
            username, colon, password = (
                base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")
            )
        except (binascii.Error, UnicodeDecodeError):
            raise Unauthenticated("credentials malformed") from None
        if not colon:
            raise Unauthenticated("credentials malformed")
        if not self.allow_http and not _secure(request):
            logger.warning(
                "basic credentials refused over plain http; serve https, or pass "
                "Basic(allow_http=True)", extra={"scheme": self.name},
            )
            raise Unauthenticated("Basic credentials are refused over plain HTTP")
        found = await _call(self.verify, username, password)
        if found is None:
            logger.info("basic credentials refused by verify", extra={"scheme": self.name})
            raise Unauthenticated("credentials invalid")
        return _found(found, self)

    def challenge(self, error: HTTPError | None) -> str:
        return f'Basic realm={_quoted(self.realm)}, charset="UTF-8"'

    def openapi(self) -> dict[str, Any]:
        return {"type": "http", "scheme": "basic"}


class SessionAuth(Scheme):
    """The user a signed session cookie says is logged in.

        sessions = Sessions(secret=SECRET)

        @app.post("/login", auth=None)
        async def login(_: Request, form: Login = Form(), session=Depends(sessions.load)):
            user = await check(form)
            session["user_id"] = user.id

        web = SessionAuth(sessions, key="user_id", load=load_user)

    `load` turns the stored value into the user, and may return a `Principal`
    to set scopes. With no `load`, the value itself is the subject.

    A session cookie that does not verify, has expired, or names a user
    `load` cannot find counts as no session rather than a wrong credential. A
    browser sends the cookie on its own, and the person using it cannot remove
    a stale one; refusing it outright would lock them out of pages that accept
    anonymous visitors, until the cookie expired.
    """


    def __init__(
        self,
        sessions: Any,
        *,
        key: str = "user_id",
        load: Callable[[Any], Any] | None = None,
        name: str = "session",
    ) -> None:
        if not callable(getattr(sessions, "decode", None)):
            raise TypeError("SessionAuth needs the app's Sessions(...)")
        if load is not None and not callable(load):
            raise TypeError(f"load= needs a function, got {type(load).__name__}")
        self.sessions = sessions
        self.key = key
        self.load = load
        self.name = name

    async def authenticate(self, request: Any) -> Principal | None:
        raw = request.cookies.get(self.sessions.cookie)
        if not raw:
            return None
        value = self.sessions.decode(raw).get(self.key)
        if value is None:
            return None
        if self.load is None:
            return Principal(subject=str(value), scheme=self.name)
        user = await _call(self.load, value)
        if user is None:
            return None
        if isinstance(user, Principal):
            return user
        return Principal(subject=str(value), scheme=self.name, user=user)

    def openapi(self) -> dict[str, Any]:
        return {"type": "apiKey", "in": "cookie", "name": self.sessions.cookie}


__all__ = [
    "JWT",
    "PRINCIPAL",
    "APIKey",
    "Basic",
    "Bearer",
    "Forbidden",
    "Principal",
    "Requirement",
    "Scheme",
    "SessionAuth",
    "Unauthenticated",
    "optional",
    "principal",
]
