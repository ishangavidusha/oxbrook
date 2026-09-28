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
import hmac
import inspect
import itertools
import logging
import secrets
import time
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
    empty. `scopes` are what the credential grants the client, and `roles`
    what the caller is, as an identity provider assigns them; `requires(...)`
    checks both. `claims` is anything else the credential carried, and `user`
    whatever a verify function looked up, if anything.

    Scopes and roles are kept apart because providers issue them separately,
    and a role that shares a name with a scope must not grant it.
    """

    subject: str
    scheme: str = ""
    scopes: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    claims: Mapping[str, Any] = field(default_factory=dict)
    user: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not self.subject:
            raise TypeError(
                f"a Principal's subject is a non-empty str, got {self.subject!r}; "
                f"pass str(user.id) for a numeric id"
            )
        # Unrolled rather than a loop over the two: this runs on every
        # authenticated request.
        if type(self.scopes) is not frozenset:
            object.__setattr__(self, "scopes", _names_of(self.scopes, "scopes"))
        if type(self.roles) is not frozenset:
            object.__setattr__(self, "roles", _names_of(self.roles, "roles"))


def _names_of(value: Any, kind: str) -> frozenset[str]:
    if isinstance(value, str):
        # frozenset("admin") is five one-letter names, which would pass a
        # requirement for "a" and fail one for "admin" without a word.
        raise TypeError(
            f"a Principal's {kind} are a collection of names, not one string; "
            f"split a space-separated claim first"
        )
    return frozenset(value)


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
        roles: Iterable[str] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(403, detail, headers, **kwargs)
        #: The scopes the caller lacked, when that is why.
        self.scopes = tuple(scopes)
        #: The roles the caller lacked, when that is why.
        self.roles = tuple(roles)


# ---- declaring --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Requirement:
    """What `.requires(...)` asks of a principal."""

    all_of: frozenset[str] = frozenset()
    any_of: frozenset[str] = frozenset()
    check: Callable[..., Any] | None = None
    roles: frozenset[str] = frozenset()

    async def met(self, found: Principal, request: Any) -> bool:
        if not self.all_of <= found.scopes:
            return False
        if self.any_of and not self.any_of & found.scopes:
            return False
        if not self.roles <= found.roles:
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

    def missing_roles(self, found: Principal) -> list[str]:
        return sorted(self.roles - found.roles)

    def __repr__(self) -> str:
        parts = [repr(s) for s in sorted(self.all_of)]
        if self.any_of:
            parts.append(f"any_of={tuple(sorted(self.any_of))!r}")
        if self.roles:
            parts.append(f"roles={tuple(sorted(self.roles))!r}")
        if self.check is not None:
            parts.append(f"check={getattr(self.check, '__name__', self.check)}")
        return ", ".join(parts)


def _requirement(
    scopes: tuple, any_of: Iterable[str], roles: Iterable[str], check: Any
) -> Requirement:
    if isinstance(any_of, str):
        raise TypeError("any_of is a collection of scopes, not one string: any_of=('a', 'b')")
    if isinstance(roles, str):
        raise TypeError("roles is a collection of roles, not one string: roles=('admin',)")
    any_of = tuple(any_of)
    roles = tuple(roles)
    for scope in (*scopes, *any_of):
        if not isinstance(scope, str) or not scope:
            raise TypeError(f"a scope is a non-empty str, got {scope!r}")
    for role in roles:
        if not isinstance(role, str) or not role:
            raise TypeError(f"a role is a non-empty str, got {role!r}")
    if check is not None and not callable(check):
        raise TypeError(f"check= needs a callable, got {type(check).__name__}")
    if not scopes and not any_of and not roles and check is None:
        raise TypeError("requires() needs scopes, any_of=, roles= or check=")
    return Requirement(frozenset(scopes), frozenset(any_of), check, frozenset(roles))


class _Combinable:
    """`|` and `.requires(...)`, for schemes and for what they combine into."""

    def requires(
        self,
        *scopes: str,
        any_of: Iterable[str] = (),
        roles: Iterable[str] = (),
        check: Any = None,
    ) -> "_Combinable":
        """This, plus a requirement on the principal it finds.

            tokens.requires("notes:write")                 # every one of these scopes
            tokens.requires(any_of=("notes:read", "notes:admin"))   # at least one
            tokens.requires(roles=("editor",))             # every one of these roles
            tokens.requires(check=is_staff)                # async (principal, request) -> bool

        A principal that fails it is refused with `403`. On `a | b` it applies
        to both; on one side of a `|`, to that side alone. Either of two roles
        is two requirements on one scheme, which is tried as one credential:
        `tokens.requires(roles=("admin",)) | tokens.requires(roles=("support",))`.
        """
        return _Required(self, _requirement(scopes, any_of, roles, check))

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

#: Verified tokens a scheme remembers, by default. A few thousand callers with
#: live tokens at once; past that the oldest quarter is dropped and verified
#: again when next seen.
_CACHE_SIZE = 4096


def _pyjwt(what: str) -> Any:
    try:
        import jwt
    except ModuleNotFoundError:  # pragma: no cover - depends on the environment
        raise ModuleNotFoundError(
            f"{what} needs PyJWT, with cryptography for public keys: "
            f"pip install 'oxbrook[auth]'"
        ) from None
    return jwt


def _claim_path(value: Any, what: str) -> tuple[str, ...] | None:
    """A claim name, or a tuple of names into nested objects."""
    if value is None:
        return None
    path = (value,) if isinstance(value, str) else tuple(value)
    if not path or not all(isinstance(step, str) and step for step in path):
        raise TypeError(
            f"{what} is a claim name, or a tuple of names for a nested claim such as "
            f"('realm_access', 'roles'); got {value!r}"
        )
    return path


def _names(claims: Mapping[str, Any], path: tuple[str, ...] | None) -> frozenset[str]:
    """The names at `path`: a space-separated string or a list of strings."""
    if path is None:
        return frozenset()
    value: Any = claims
    for step in path:
        if not isinstance(value, Mapping):
            return frozenset()
        value = value.get(step)
    if isinstance(value, str):
        return frozenset(value.split())
    if isinstance(value, (list, tuple)):
        return frozenset(v for v in value if isinstance(v, str) and v)
    return frozenset()


def _expectations(claims: Mapping[str, Any] | None) -> dict[str, Any]:
    if claims is None:
        return {}
    if not isinstance(claims, Mapping):
        raise TypeError("claims= maps a claim name to the value it must have")
    expected = {}
    for name, value in claims.items():
        if not isinstance(name, str) or not name:
            raise TypeError(f"claims= keys are claim names, got {name!r}")
        if isinstance(value, (list, tuple, set, frozenset)):
            value = frozenset(value)
            if not value:
                raise ValueError(f"claims={{{name!r}: ...}} lists no acceptable value")
        expected[name] = value
    return expected


def _frozen(value: Any) -> Any:
    """Claims made read-only all the way down: a remembered principal is
    shared by every request that carries its token, so a handler that changed
    a nested list would change it for the caller's later requests too."""
    if isinstance(value, dict):
        return types.MappingProxyType({k: _frozen(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(v) for v in value)
    return value


class _Signed(Bearer):
    """What `JWT` and `OIDC` share: claims into a principal, and a memory of
    tokens already verified.

    The memory is the reason a public-key signature is not checked again on
    every request: a token that verified once is the same token until it
    expires, so a hit costs a dictionary lookup and an `exp` comparison. Only
    a token that verified is kept, so a caller cannot fill it with forgeries,
    and `OIDC` empties it whenever the provider's keys change, so a withdrawn
    key stops working at the next refresh rather than at each token's expiry.
    """

    def _configure(
        self,
        *,
        audience: Any,
        issuer: Any,
        leeway: float,
        scopes_claim: Any,
        roles_claim: Any,
        subject_claim: str,
        claims: Mapping[str, Any] | None,
        cache_size: int,
    ) -> None:
        if audience is not None and not isinstance(audience, str):
            audience = list(audience)
        if not isinstance(cache_size, int) or isinstance(cache_size, bool) or cache_size < 0:
            raise ValueError("cache_size is a count of tokens, 0 to turn the cache off")
        self.audience = audience
        self.issuer = issuer
        self._issuers = issuer
        self.leeway = leeway
        self.scopes_claim = scopes_claim
        self.roles_claim = roles_claim
        self.subject_claim = subject_claim
        self._scopes = _claim_path(scopes_claim, "scopes_claim")
        self._roles = _claim_path(roles_claim, "roles_claim")
        self.expected = _expectations(claims)
        self.cache_size = cache_size
        self._verified: dict[str, tuple[float, Principal]] = {}
        self.require = ["exp", subject_claim] + (["iss"] if issuer else [])

    async def _claims(self, token: str) -> dict[str, Any]:
        raise NotImplementedError

    def _fresh(self) -> None:
        """Called on every remembered token: where keys can go stale, look."""

    async def check(self, token: str) -> Principal:
        remembered = self._verified.get(token)
        if remembered is not None:
            if time.time() <= remembered[0]:
                self._fresh()
                return remembered[1]
            self._verified.pop(token, None)
        claims = await self._claims(token)
        found = self._principal(claims)
        if self.cache_size:
            self._remember(token, float(claims["exp"]) + self.leeway, found)
        return found

    def _remember(self, token: str, until: float, found: Principal) -> None:
        verified = self._verified
        if len(verified) >= self.cache_size:
            # The oldest quarter, in one pass: popping them one at a time from
            # the front is quadratic in a dict.
            try:
                for stale in list(itertools.islice(verified, max(1, self.cache_size // 4))):
                    verified.pop(stale, None)
            except RuntimeError:
                # Another worker loop changed it mid-pass; a later insert trims.
                pass
        verified[token] = (until, found)

    def _decode(self, token: str, key: Any, algorithms: list[str]) -> dict[str, Any]:
        jwt = self._jwt
        try:
            return jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience=self.audience,
                issuer=self._issuers,
                leeway=self.leeway,
                options={"require": self.require, "verify_aud": self.audience is not None},
            )
        except jwt.ExpiredSignatureError:
            logger.info("jwt refused: expired", extra={"scheme": self.name})
            raise Unauthenticated("token expired") from None
        except jwt.PyJWTError as exc:
            # The reason is for the log. The client learns only that the token
            # is invalid: which check failed is a map for forging the next one.
            logger.info("jwt refused: %s", type(exc).__name__, extra={"scheme": self.name})
            raise Unauthenticated("token invalid") from None

    def _principal(self, claims: dict[str, Any]) -> Principal:
        for name, wanted in self.expected.items():
            value = claims.get(name)
            if isinstance(value, (dict, list)):
                ok = False
            elif isinstance(wanted, frozenset):
                ok = value in wanted
            else:
                ok = value == wanted
            if not ok:
                logger.info("jwt refused: claim %s", name, extra={"scheme": self.name})
                raise Unauthenticated("token invalid")
        subject = claims.get(self.subject_claim)
        if isinstance(subject, int) and not isinstance(subject, bool):
            subject = str(subject)
        if not isinstance(subject, str) or not subject:
            logger.info("jwt refused: no usable subject", extra={"scheme": self.name})
            raise Unauthenticated("token invalid")
        return Principal(
            subject=subject,
            scheme=self.name,
            scopes=_names(claims, self._scopes),
            roles=_names(claims, self._roles),
            claims=_frozen(claims),
        )


class JWT(_Signed):
    """A JSON Web Token this app can verify itself.

        tokens = JWT(key=os.environ["JWT_SECRET"], algorithms=["HS256"], audience="notes")
        tokens = JWT(key=public_key_pem, algorithms=["RS256"], audience="notes",
                     issuer="https://login.example.com/")

    Checked every time: the signature, `exp` with `leeway` seconds of clock
    skew, `nbf`, `aud` against `audience`, and `iss` against `issuer` when one
    is given. The principal's subject is the `sub` claim and its scopes the
    `scope` claim, a space-separated string or a list; `scopes_claim` names a
    different claim, such as `"scp"` or `"permissions"`, and `roles_claim`
    one to read roles from. Either may be a tuple naming a nested claim:
    `roles_claim=("realm_access", "roles")`. `claims=` names claims that must
    have a given value, or one of a list: `claims={"token_use": "access"}`.

    `algorithms` is required, and cannot mix families: an HMAC secret with
    `HS*`, or a public key with the asymmetric ones. Accepting both is how a
    public key ends up used as an HMAC secret to forge a token, and `none` is
    never accepted. An HMAC secret must be at least as long as the hash.

    `audience=None` accepts a token for any audience, and has to be written
    out: a token issued for another service is otherwise accepted by this one.

    A token that verified is remembered until it expires, up to `cache_size`
    of them, so a caller's next request skips the signature check; `0` turns
    that off.

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
        scopes_claim: str | tuple[str, ...] = "scope",
        roles_claim: str | tuple[str, ...] | None = None,
        subject_claim: str = "sub",
        claims: Mapping[str, Any] | None = None,
        cache_size: int = _CACHE_SIZE,
        realm: str | None = None,
        name: str = "jwt",
    ) -> None:
        jwt = _pyjwt("JWT")
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
        self.key = key
        self._key = key if hmac_algorithms else self._prepared(jwt, key, algorithms)
        self.algorithms = algorithms
        self._configure(
            audience=audience, issuer=issuer, leeway=leeway, scopes_claim=scopes_claim,
            roles_claim=roles_claim, subject_claim=subject_claim, claims=claims,
            cache_size=cache_size,
        )

    @staticmethod
    def _prepared(jwt: Any, key: Any, algorithms: list[str]) -> Any:
        """A PEM key parsed once, here, rather than by PyJWT on every token:
        parsing is most of an uncached RS256 check. Also finds a key that
        suits none of the algorithms now rather than on the first request."""
        for name in algorithms:
            try:
                return jwt.get_algorithm_by_name(name).prepare_key(key)
            except (jwt.PyJWTError, ValueError, TypeError):
                continue
        raise ValueError(f"the key is not a public key for {', '.join(algorithms)}")

    async def _claims(self, token: str) -> dict[str, Any]:
        return self._decode(token, self._key, self.algorithms)


class OIDC(_Signed):
    """Tokens from an OpenID Connect provider, checked against its published keys.

        users = OIDC("https://login.example.com/realms/acme", audience="notes-api")
        users = OIDC.auth0("acme.eu.auth0.com", audience="https://notes.example.com")

    The provider's discovery document, `/.well-known/openid-configuration`
    under the issuer, says where its signing keys are. They are fetched on
    the first token, once for the whole process however many worker loops
    ask, and again every `keys_max_age` seconds so that a key the provider
    withdraws stops being accepted. A token signed with a key id not yet seen
    fetches them again at once — how a provider's key rotation arrives — but
    at most once a minute, so tokens with made-up key ids cannot turn into a
    stream of requests to the provider. Call `await scheme.load()` from a
    lifespan to fetch them at startup instead, and fail there if the provider
    is unreachable.

    Only public-key algorithms are accepted, those the provider advertises,
    and each only with a key of its own type: never `none`, never HMAC, so a
    provider's public key cannot be used as a shared secret to forge a token.
    `algorithms=` narrows the list further. Every token is checked for its
    signature, `iss`, `aud`, `exp` and `nbf`, with `leeway` seconds of clock
    skew; claims are read as in `JWT`, including `roles_claim` and `claims=`.

    When the provider cannot be reached before any keys were fetched, the
    answer is `503` rather than `401`: the token may well be good, and a `401`
    tells a client to throw it away.

    The issuer must be HTTPS, since keys fetched over plain HTTP can be
    replaced in transit; `http://localhost` is allowed for development, and
    `allow_http=True` for a provider on a private network.

    The class methods set up known providers: `keycloak`, `auth0`, `entra`,
    `okta`, `cognito`, `google` and `firebase`. Each takes the same keyword
    arguments as `OIDC` itself, to override what it sets.
    """


    def __init__(
        self,
        issuer: str,
        *,
        audience: str | Iterable[str] | None,
        algorithms: Iterable[str] | None = None,
        scopes_claim: str | tuple[str, ...] = "scope",
        roles_claim: str | tuple[str, ...] | None = None,
        subject_claim: str = "sub",
        claims: Mapping[str, Any] | None = None,
        leeway: float = 60,
        discovery_url: str | None = None,
        keys_max_age: float = 300,
        allow_http: bool = False,
        timeout: float = 10,
        cache_size: int = _CACHE_SIZE,
        realm: str | None = None,
        name: str = "oidc",
    ) -> None:
        jwt = _pyjwt("OIDC")
        from . import _jwks

        super().__init__(realm=realm, name=name, bearer_format="JWT")
        self._jwt = jwt
        if not isinstance(issuer, str) or not issuer:
            raise TypeError("OIDC needs the provider's issuer URL")
        _jwks.check_url(issuer, allow_http, "issuer")
        if discovery_url is None:
            discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        _jwks.check_url(discovery_url, allow_http, "discovery_url")
        if algorithms is not None:
            if isinstance(algorithms, str):
                raise TypeError("algorithms is a list: algorithms=['RS256']")
            algorithms = frozenset(algorithms)
            refused = [a for a in algorithms if a not in _ASYMMETRIC]
            if refused or not algorithms:
                raise ValueError(
                    f"OIDC accepts only public-key algorithms ({', '.join(sorted(_ASYMMETRIC))}); "
                    f"a provider's tokens are never HMAC or unsigned. Refused: {refused}"
                )
        if keys_max_age <= 0:
            raise ValueError("keys_max_age is seconds, more than 0")
        self.discovery_url = discovery_url
        self._configure(
            audience=audience, issuer=issuer, leeway=leeway, scopes_claim=scopes_claim,
            roles_claim=roles_claim, subject_claim=subject_claim, claims=claims,
            cache_size=cache_size,
        )
        self.keys = _jwks.KeySet(
            discovery_url,
            issuer,
            algorithms=algorithms,
            allow_http=allow_http,
            timeout=timeout,
            max_age=keys_max_age,
            changed=self._verified.clear,
        )

    def _fresh(self) -> None:
        # Without this, a caller whose token is remembered never reaches the
        # key set, and a withdrawn key would work until each token expired.
        self.keys.refresh_if_stale()

    async def load(self) -> None:
        """Fetch the discovery document and keys now, raising if that fails."""
        from . import _jwks

        try:
            await self.keys.load()
        except _jwks.Unavailable as exc:
            raise RuntimeError(f"{self.name}: {exc}") from None

    async def _claims(self, token: str) -> dict[str, Any]:
        from . import _jwks

        jwt = self._jwt
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            logger.info("jwt refused: malformed", extra={"scheme": self.name})
            raise Unauthenticated("token invalid") from None
        alg, kid = header.get("alg"), header.get("kid")
        if not isinstance(alg, str) or not (kid is None or isinstance(kid, str)):
            logger.info("jwt refused: malformed header", extra={"scheme": self.name})
            raise Unauthenticated("token invalid")
        try:
            key = await self.keys.find(kid, alg)
        except _jwks.Unavailable:
            raise HTTPError(
                503, "the identity provider could not be reached", {"retry-after": "5"}
            ) from None
        except _jwks.Refused as exc:
            logger.info("jwt refused: %s", exc, extra={"scheme": self.name})
            raise Unauthenticated("token invalid") from None
        return self._decode(token, key, [alg])

    def openapi(self) -> dict[str, Any]:
        return {"type": "openIdConnect", "openIdConnectUrl": self.discovery_url}

    def __repr__(self) -> str:
        return self.name

    # ---- providers ------------------------------------------------------------

    @classmethod
    def _preset(cls, issuer: str, defaults: dict[str, Any], options: dict[str, Any]) -> "OIDC":
        return cls(issuer, **{**defaults, **options})

    @classmethod
    def keycloak(cls, url: str, realm: str, *, audience: Any, **options: Any) -> "OIDC":
        """Keycloak: `OIDC.keycloak("https://sso.example.com", "acme", audience="notes-api")`.

        Scopes from `scope`, roles from the realm roles in `realm_access`. For
        a client's own roles, `roles_claim=("resource_access", client, "roles")`.
        Keycloak puts a client in `aud` only through an audience mapper, which
        the API's clients need.
        """
        issuer = f"{_bare(url, 'url', scheme=True)}/realms/{_segment(realm, 'realm')}"
        defaults = {"audience": audience, "roles_claim": ("realm_access", "roles")}
        return cls._preset(issuer, defaults, options)

    @classmethod
    def auth0(cls, domain: str, *, audience: Any, **options: Any) -> "OIDC":
        """Auth0: `OIDC.auth0("acme.eu.auth0.com", audience="https://notes.example.com")`.

        `audience` is the API's identifier. Scopes from `scope`; with RBAC's
        "Add Permissions in the Access Token", `scopes_claim="permissions"`.
        """
        issuer = f"https://{_bare(domain, 'domain')}/"
        return cls._preset(issuer, {"audience": audience}, options)

    @classmethod
    def entra(
        cls, tenant_id: str, *, audience: Any, version: int = 2, **options: Any
    ) -> "OIDC":
        """Microsoft Entra ID: `OIDC.entra(tenant_id, audience=api_client_id)`.

        `tenant_id` is the directory's GUID; `common` and `organizations`
        name no single issuer and are refused. Scopes from `scp`, app roles
        from `roles`. An API's access tokens are version 1 unless its manifest
        sets `requestedAccessTokenVersion` to 2, and the two have different
        issuers: pass `version=1` for those.
        """
        tenant = _segment(tenant_id, "tenant_id")
        if tenant.lower() in {"common", "organizations", "consumers"}:
            raise ValueError(
                f"{tenant!r} is not one tenant, so it names no issuer to check; pass the "
                f"tenant id"
            )
        defaults: dict[str, Any] = {
            "audience": audience, "scopes_claim": "scp", "roles_claim": "roles",
        }
        if version == 2:
            issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
        elif version == 1:
            issuer = f"https://sts.windows.net/{tenant}/"
            defaults["discovery_url"] = (
                f"https://login.microsoftonline.com/{tenant}/.well-known/openid-configuration"
            )
        else:
            raise ValueError("Entra access tokens are version 1 or 2")
        return cls._preset(issuer, defaults, options)

    @classmethod
    def okta(
        cls, domain: str, *, audience: Any, server: str = "default", **options: Any
    ) -> "OIDC":
        """Okta: `OIDC.okta("acme.okta.com", audience="api://default")`.

        A custom authorization server, `default` unless `server` names
        another; the org server's access tokens are for Okta alone to verify.
        Scopes from `scp`.
        """
        issuer = f"https://{_bare(domain, 'domain')}/oauth2/{_segment(server, 'server')}"
        return cls._preset(issuer, {"audience": audience, "scopes_claim": "scp"}, options)

    @classmethod
    def cognito(
        cls, region: str, user_pool_id: str, *, client_id: str | Iterable[str], **options: Any
    ) -> "OIDC":
        """Amazon Cognito: `OIDC.cognito("eu-west-1", "eu-west-1_AbC123", client_id="...")`.

        Cognito access tokens carry no `aud`: the app client is in
        `client_id`, which is checked instead, along with `token_use` being
        `access` so an ID token is not accepted in its place. Scopes from
        `scope`, groups as roles from `cognito:groups`.
        """
        issuer = (
            f"https://cognito-idp.{_segment(region, 'region')}.amazonaws.com/"
            f"{_segment(user_pool_id, 'user_pool_id')}"
        )
        clients = [client_id] if isinstance(client_id, str) else list(client_id)
        defaults = {
            "audience": None,
            "claims": {"token_use": "access", "client_id": clients},
            "roles_claim": "cognito:groups",
        }
        return cls._preset(issuer, defaults, options)

    @classmethod
    def google(cls, client_id: str | Iterable[str], **options: Any) -> "OIDC":
        """Google ID tokens, from Sign in with Google: `OIDC.google(client_id)`.

        Identity only: Google's tokens carry no scopes for an API. `aud` is
        the app's OAuth client id, and `iss` either of the two forms Google
        uses.
        """
        scheme = cls._preset("https://accounts.google.com", {"audience": client_id}, options)
        scheme._issuers = ["https://accounts.google.com", "accounts.google.com"]
        return scheme

    @classmethod
    def firebase(cls, project_id: str, **options: Any) -> "OIDC":
        """Firebase Authentication ID tokens: `OIDC.firebase("acme-app")`.

        `aud` is the project id. Custom claims set through the Admin SDK are
        top-level claims, so `roles_claim="roles"` reads a `roles` list.
        """
        project = _segment(project_id, "project_id")
        return cls._preset(
            f"https://securetoken.google.com/{project}", {"audience": project}, options
        )


def _segment(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or "?" in value or "#" in value:
        raise ValueError(f"{what} is one name, without slashes; got {value!r}")
    return value


def _bare(value: Any, what: str, scheme: bool = False) -> str:
    """A host (`scheme=False`) or a base URL (`scheme=True`), without a trailing slash."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} is required")
    if scheme:
        if "://" not in value:
            raise ValueError(f"{what} is a URL with its scheme: 'https://sso.example.com'")
        return value.rstrip("/")
    if "://" in value or "/" in value.rstrip("/"):
        raise ValueError(f"{what} is a host name, such as 'acme.eu.auth0.com'; got {value!r}")
    return value.rstrip("/")


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


#: Methods that must not change state, so a request forged across sites with
#: one of them gains the attacker nothing.
_SAFE = frozenset({"GET", "HEAD", "OPTIONS"})

#: Where `SessionAuth` keeps the session's CSRF token, and the header a
#: request that needs it sends it in.
CSRF_KEY = "_csrf"
CSRF_HEADER = "x-csrf-token"


def _authority(value: str, scheme: str | None) -> str:
    """`host[:port]`, lowercased, without the port its scheme implies."""
    value = value.strip().lower()
    for default, port in (("http", ":80"), ("https", ":443")):
        if (scheme is None or scheme == default) and value.endswith(port):
            return value[: -len(port)]
    return value


def _same_origin(origin: str, request: Any) -> bool:
    """Whether `Origin` names this server, as the client addressed it.

    As the WebSocket origin check does: the `Host` header, or behind a proxy
    that rewrote it, `X-Forwarded-Host`, which a page cannot set on a request
    it forges without a preflight this server would refuse.
    """
    scheme, sep, authority = origin.strip().lower().partition("://")
    if not sep or not authority:
        return False  # `null`, from sandboxed frames and files
    authority = _authority(authority, scheme)
    for header in ("host", "x-forwarded-host"):
        value = request.header(header)
        if value and _authority(value.split(",")[0], None) == authority:
            return True
    return False


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

    **Cross-site requests are refused.** A browser sends the session cookie
    with a request another site's page makes, so a request that changes state
    — anything but GET, HEAD and OPTIONS — must show it came from this site:

    1. `Sec-Fetch-Site: same-origin` or `none`, which browsers set themselves;
    2. otherwise an `Origin` naming this server, or one of `trusted_origins`;
    3. with neither header, an `X-CSRF-Token` header equal to the session's
       token, from `csrf_token(session)`.

    Anything else is `403`. `trusted_origins` defaults to the app's CORS
    origins: a page allowed to call the API with credentials is trusted to.
    `csrf=False` turns the check off, for an app that does its own.
    """


    def __init__(
        self,
        sessions: Any,
        *,
        key: str = "user_id",
        load: Callable[[Any], Any] | None = None,
        name: str = "session",
        csrf: bool = True,
        trusted_origins: Iterable[str] | None = None,
    ) -> None:
        if not callable(getattr(sessions, "decode", None)):
            raise TypeError("SessionAuth needs the app's Sessions(...)")
        if load is not None and not callable(load):
            raise TypeError(f"load= needs a function, got {type(load).__name__}")
        if trusted_origins is not None:
            from ._cors import check_origin

            if isinstance(trusted_origins, str):
                raise TypeError("trusted_origins is a list of origins, not one string")
            trusted_origins = frozenset(
                check_origin(o).lower() for o in trusted_origins if o != "*"
            )
        self.sessions = sessions
        self.key = key
        self.load = load
        self.name = name
        self.csrf = csrf
        self.trusted_origins = trusted_origins

    @staticmethod
    def csrf_token(session: Any) -> str:
        """The session's CSRF token, made on first use.

        For a request that carries neither `Sec-Fetch-Site` nor `Origin` —
        an old browser, mostly — to send as `X-CSRF-Token`. Render it into the
        page, or return it from an endpoint the page's script reads.
        """
        token = session.get(CSRF_KEY)
        if not isinstance(token, str) or not token:
            token = secrets.token_urlsafe(32)
            session[CSRF_KEY] = token
        return token

    def _trusted(self, request: Any) -> frozenset[str]:
        if self.trusted_origins is not None:
            return self.trusted_origins
        cors = getattr(request.app, "cors", None)
        if cors is None:
            return frozenset()
        return frozenset(o.lower() for o in cors.allow_origins if o != "*")

    def _forged(self, request: Any, data: dict) -> str | None:
        """Why this request might be forged by another site, or None."""
        if request.method in _SAFE:
            return None
        site = (request.header("sec-fetch-site") or "").strip().lower()
        if site in ("same-origin", "none"):
            return None
        origin = request.header("origin")
        if origin is not None:
            if _same_origin(origin, request) or origin.strip().lower() in self._trusted(request):
                return None
            return "cross-origin request"
        if site:
            # A browser that says cross-site and sends no Origin: nothing to
            # compare, and no reason to believe it.
            return f"{site} request without an origin"
        expected = data.get(CSRF_KEY)
        offered = request.header(CSRF_HEADER)
        if (isinstance(expected, str) and expected and offered is not None
                and hmac.compare_digest(offered.encode(), expected.encode())):
            return None
        return "no origin, and no CSRF token"

    async def authenticate(self, request: Any) -> Principal | None:
        raw = request.cookies.get(self.sessions.cookie)
        if not raw:
            return None
        data = self.sessions.decode(raw)
        value = data.get(self.key)
        if value is None:
            return None
        if self.csrf:
            # Before `load`: a forged request should not cost a lookup.
            reason = self._forged(request, data)
            if reason is not None:
                logger.warning("session request refused: %s", reason,
                               extra={"scheme": self.name, "path": request.path})
                raise Forbidden("cross-site request refused")
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


class Tickets(Scheme):
    """Short-lived tickets for opening a WebSocket from a browser.

        tickets = Tickets()

        @app.post("/ws-ticket", auth=users)
        async def ws_ticket(_: Request, who: Principal = Depends(principal)):
            return {"ticket": tickets.issue(who)}

        @app.websocket("/feed", auth=tickets)
        async def feed(_: Request, ws, who: Principal = Depends(principal)): ...

    A browser cannot set `Authorization` on a WebSocket, and a token in the
    URL ends up in proxy logs and history. A ticket goes in the URL instead —
    `new WebSocket("wss://.../feed?ticket=...")` — and is safe there because
    it is good for one connection, within `ttl` seconds (30 by default), and
    for nothing else. The socket's principal is the one it was issued for.

    Only a WebSocket upgrade reads one: on any other request a ticket in the
    query string is ignored, and no other scheme reads the query string.

    Tickets are kept in the process that issued them. Behind a load balancer
    with several processes, the socket must reach the process the ticket
    came from.
    """

    def __init__(
        self, *, ttl: float = 30, param: str = "ticket", name: str = "ticket",
        limit: int = 100_000,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl is seconds, more than 0")
        self.ttl = ttl
        self.param = param
        self.name = name
        self.limit = limit
        self._waiting: dict[str, tuple[float, Principal]] = {}

    def issue(self, who: Principal) -> str:
        """A new ticket for `who`, good for one socket within `ttl` seconds."""
        if not isinstance(who, Principal):
            raise TypeError("issue() takes the Principal the socket should have")
        now = time.monotonic()
        waiting = self._waiting
        if len(waiting) >= self.limit:
            try:
                for key in [k for k, (until, _) in list(waiting.items()) if until < now]:
                    waiting.pop(key, None)
                # Still full of live ones: the oldest go first. A ticket is
                # meant to be used within a second or two of being issued.
                for key in list(itertools.islice(waiting, max(1, len(waiting) - self.limit + 1))):
                    waiting.pop(key, None)
            except RuntimeError:
                pass  # another loop changed it mid-pass; the next issue trims
        ticket = secrets.token_urlsafe(32)
        waiting[ticket] = (now + self.ttl, who)
        return ticket

    async def authenticate(self, request: Any) -> Principal | None:
        if (request.header("upgrade") or "").strip().lower() != "websocket":
            return None
        query = request.query
        if not query:
            return None
        from urllib.parse import parse_qsl

        offered = [v for k, v in parse_qsl(query, keep_blank_values=True) if k == self.param]
        if not offered:
            return None
        # Popped, whatever happens next: single use means a second attempt
        # with the same ticket fails even if the first was refused later.
        entry = self._waiting.pop(offered[0], None) if len(offered) == 1 else None
        if entry is None or entry[0] < time.monotonic():
            logger.info("websocket ticket refused", extra={"scheme": self.name})
            raise Unauthenticated("ticket invalid")
        return entry[1]


__all__ = [
    "JWT",
    "OIDC",
    "PRINCIPAL",
    "APIKey",
    "Basic",
    "Bearer",
    "Forbidden",
    "Principal",
    "Requirement",
    "Scheme",
    "SessionAuth",
    "Tickets",
    "Unauthenticated",
    "optional",
    "principal",
]
