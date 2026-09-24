"""How an `auth=` declaration is enforced and described.

The public half — schemes, `Principal`, `requires`, `optional` — is
`oxbrook.auth`. This is the other half: turning a declaration into the check
that runs on every surface a route is reachable from, and into its OpenAPI
form. One `Gate` per route does the checking for HTTP, for a WebSocket's
upgrade and for an MCP tool call alike, which is what keeps them from
disagreeing (invariant 10).
"""

import secrets
import time
from typing import Any

from ._middleware import Reply
from ._response import Response
from .auth import (
    PRINCIPAL,
    Forbidden,
    Principal,
    Unauthenticated,
    _checked,
    _Combinable,
    _Either,
    _Optional,
    _Required,
)


class _Unset:
    """`auth=` not given, so the next declaration out applies."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Any = _Unset()


def check(value: Any, where: str) -> Any:
    """Validate an `auth=` argument where it is written, not on first request."""
    if value is UNSET or value is None or isinstance(value, _Optional):
        return value
    try:
        return _checked(value, "auth=")
    except TypeError as exc:
        raise TypeError(f"{where}: {exc}") from None


def resolve(*declarations: Any) -> Any:
    """The nearest declaration, innermost first: a route's, then its routers',
    then the app's. None — public — when none of them said anything."""
    for declared in declarations:
        if declared is not UNSET:
            return declared
    return None


def _alternatives(policy: Any) -> list[tuple[Any, tuple]]:
    """(scheme, requirements) for each way through a declaration, in order."""
    if isinstance(policy, _Either):
        return [alt for member in policy.members for alt in _alternatives(member)]
    if isinstance(policy, _Required):
        return [(s, (*reqs, policy.requirement)) for s, reqs in _alternatives(policy.inner)]
    return [(policy, ())]


def _name(scheme: Any) -> str:
    return getattr(scheme, "name", None) or type(scheme).__name__


def plan(policy: Any) -> tuple[list[tuple[Any, list[tuple]]], bool]:
    """Each distinct scheme once, in first-written order, with every set of
    requirements it may satisfy, and whether anonymous callers are let in.

    Grouped by scheme so that `a.requires("x") | a.requires("y")` is one
    credential that may satisfy either, rather than a first try that fails on
    "x" and stops before "y" is looked at.
    """
    anonymous = isinstance(policy, _Optional)
    if anonymous:
        policy = policy.inner
    grouped: dict[int, tuple[Any, list[tuple]]] = {}
    for scheme, requirements in _alternatives(policy):
        grouped.setdefault(id(scheme), (scheme, []))[1].append(requirements)
    return list(grouped.values()), anonymous


class Gate:
    """One route's declaration, ready to run against a request."""

    __slots__ = ("anonymous", "policy", "schemes")

    def __init__(self, policy: Any) -> None:
        self.policy = policy
        self.schemes, self.anonymous = plan(policy)

    async def __call__(self, request: Any) -> Principal | None:
        for scheme, options in self.schemes:
            try:
                found = await scheme.authenticate(request)
            except Unauthenticated as exc:
                # A credential of this kind, and a wrong one. The search ends
                # here: trying the next scheme, or letting an optional route
                # through as anonymous, is how "expired token" quietly becomes
                # "no token".
                raise self.unauthenticated(scheme, exc) from None
            if found is None:
                continue
            if not isinstance(found, Principal):
                raise TypeError(
                    f"{_name(scheme)}.authenticate returned {type(found).__name__}; "
                    f"return a Principal, None for no credential of its kind, or "
                    f"raise Unauthenticated for a wrong one"
                )
            if not found.scheme:
                found = Principal(found.subject, _name(scheme), found.scopes,
                                  found.claims, found.user)
            for requirements in options:
                for requirement in requirements:
                    if not await requirement.met(found, request):
                        break
                else:
                    return found
            raise self.forbidden(scheme, options[0], found)
        if self.anonymous:
            return None
        raise self.unauthenticated(None, None)

    def unauthenticated(self, raised_by: Any, error: Unauthenticated | None) -> Unauthenticated:
        """The 401, with a challenge from every scheme that has one.

        One header per challenge rather than one header holding them all:
        both are allowed, and far more clients parse the first.
        """
        challenges = []
        for scheme, _ in self.schemes:
            challenge = getattr(scheme, "challenge", None)
            if challenge is None:
                continue
            value = challenge(error if scheme is raised_by else None)
            if value:
                challenges.append(value)
        headers = dict(error.headers) if error is not None else {}
        if challenges:
            headers["www-authenticate"] = challenges
        refused = Unauthenticated(
            None if error is None else error.detail,
            headers,
            type="about:blank" if error is None else error.type,
            extensions=None if error is None else error.extensions,
        )
        return refused

    def forbidden(self, scheme: Any, requirements: tuple, found: Principal) -> Forbidden:
        missing: list[str] = []
        for requirement in requirements:
            missing.extend(s for s in requirement.missing(found) if s not in missing)
        refused = Forbidden(
            f"requires scope {' '.join(missing)}" if missing else None, scopes=missing
        )
        challenge = getattr(scheme, "challenge", None)
        value = challenge(refused) if challenge is not None else None
        if value:
            refused.headers["www-authenticate"] = value
        return refused

    # ---- where it runs ------------------------------------------------------

    async def admit(self, request: Any) -> None:
        """Authenticate, keep the principal, then read a body that waited."""
        request.locals[PRINCIPAL] = await self(request)
        if request._unread:
            # Deferred by the server until now, so that a refused caller never
            # made it read what they sent.
            await request.read()

    def wrap(self, target: Any) -> Any:
        """The check in front of a handler, for a route with no middleware."""
        admit = self.admit

        async def authenticated(request, **params):
            await admit(request)
            return await target(request, **params)

        authenticated.__name__ = getattr(target, "__name__", "handler")
        authenticated.__qualname__ = getattr(target, "__qualname__", "handler")
        return authenticated

    def middleware(self) -> Any:
        """The check as a link in a middleware chain: inside the app's
        middleware, so the access log sees a refusal's status, and outside the
        routers', so theirs never runs for a caller who was refused."""
        admit = self.admit

        async def authenticate(request, call_next):
            await admit(request)
            return await call_next(request)

        return authenticate

    def socket_middleware(self) -> Any:
        """The check before a WebSocket upgrade.

        The upgrade is decided on one request and the handler runs on
        another, so an accepted upgrade leaves its principal behind under a
        key only the server hands on. Authenticating a second time instead
        would run a scheme twice for one connection, which a single-use
        credential cannot survive.
        """
        admit = self.admit

        async def authenticate(request, call_next):
            await admit(request)
            reply = await call_next(request)
            if _accepted(reply):
                reply.headers[HANDOFF] = _leave(request.locals.get(PRINCIPAL))
            return reply

        return authenticate


async def forget(request: Any, call_next: Any) -> Any:
    """For a tool call on a public route: the principal is the route's answer,
    and a public route has none, whatever the `/mcp` request's was."""
    request.locals.pop(PRINCIPAL, None)
    return await call_next(request)


# ---- WebSocket hand-off -----------------------------------------------------

#: The reply header the gate names its key in. Mirrored in src/server.rs,
#: which reads it and never sends it: the 101 is built there.
HANDOFF = "x-oxbrook-handoff"

#: Seconds a principal waits for its socket. The handler is queued the moment
#: the gate answers, so this is only reached by a socket that never ran.
_HANDOFF_TTL = 30.0

_waiting: dict[str, tuple[float, Principal | None]] = {}


def _accepted(reply: Any) -> bool:
    if not isinstance(reply, Reply):
        return False
    if reply.status is not None:
        return reply.status == 101
    return isinstance(reply.value, Response) and reply.value.status == 101


def _leave(found: Principal | None) -> str:
    now = time.monotonic()
    for key, (expires, _) in list(_waiting.items()):
        if expires < now:
            _waiting.pop(key, None)
    key = secrets.token_urlsafe(16)
    _waiting[key] = (now + _HANDOFF_TTL, found)
    return key


def receive(target: Any) -> Any:
    """A socket handler that starts with the principal its gate found."""

    async def socket(request, ws, **params):
        key = request._handoff
        entry = _waiting.pop(key, None) if key else None
        if entry is None:
            # Only a server bug gets here: a gated route's socket always has
            # a key. Refusing is the one safe answer.
            raise RuntimeError("websocket reached its handler without authentication")
        request.locals[PRINCIPAL] = entry[1]
        return await target(request, ws, **params)

    socket.__name__ = getattr(target, "__name__", "handler")
    socket.__qualname__ = getattr(target, "__qualname__", "handler")
    return socket


async def accept(_request: Any) -> None:
    """The authorizer for a socket that has `auth=` and no authorizer."""
    return None


# ---- description ------------------------------------------------------------


def describe(policy: Any, default: Any) -> str:
    """For `oxbrook routes`."""
    if policy is None:
        return "public" if default is not None else ""
    return repr(policy)


def security(policy: Any) -> tuple[list[dict[str, list[str]]], dict[str, Any]]:
    """An operation's OpenAPI `security` and the schemes it names.

    Alternatives map onto alternatives. Scopes a requirement names are listed;
    `any_of` becomes one alternative per scope, which is what OpenAPI can say;
    a `check=` function cannot be said at all and is left out. A scheme with
    no `openapi()` is left out too. `optional(...)` adds `{}`, OpenAPI's way
    of saying no credential is also accepted.
    """
    schemes, anonymous = plan(policy)
    listed: list[dict[str, list[str]]] = []
    used: dict[str, Any] = {}
    for scheme, options in schemes:
        entry = getattr(scheme, "openapi", None)
        entry = entry() if entry is not None else None
        if entry is None:
            continue
        name = _name(scheme)
        used[name] = (scheme, entry)
        for requirements in options:
            scopes: list[str] = []
            choices: list[list[str]] = [[]]
            for requirement in requirements:
                scopes.extend(s for s in sorted(requirement.all_of) if s not in scopes)
                if requirement.any_of:
                    choices = [c + [s] for c in choices for s in sorted(requirement.any_of)]
            for choice in choices:
                alternative = {name: scopes + [s for s in choice if s not in scopes]}
                if alternative not in listed:
                    listed.append(alternative)
    if anonymous:
        listed.append({})
    return listed, used


__all__ = ["UNSET", "Gate", "_Combinable", "accept", "check", "describe", "forget",
           "receive", "resolve", "security"]
