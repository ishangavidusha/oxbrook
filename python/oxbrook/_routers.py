"""Routers: routes declared in one module and mounted into an app elsewhere.

    # users.py
    from oxbrook import Router

    router = Router(prefix="/users")

    @router.get("/{user_id}")
    async def get_user(request, user_id: int): ...

    # main.py
    app = App()
    app.include(users.router, prefix="/api/v1")    # GET /api/v1/users/{user_id}

A router has the same decorators as an app, plus its own middleware, which
runs inside the app's and applies only to the router's routes. Routers nest:
`router.include(other)` works the same way and prefixes accumulate.

Routes are flattened into the app when it includes the router, so a router
costs nothing per request. Two consequences follow. A handler is validated
when it is decorated, against the router's own path, and again at `include`,
against the full path, which may add path parameters from an outer prefix. And
a router cannot change after it has been included: a route added later would
never be served, so adding one raises instead.

Paths join literally. `Router(prefix="/users")` with `@router.get("")` serves
`/users`, and with `@router.get("/")` serves `/users/`; a trailing slash is a
different route here, as it is everywhere else.
"""

from dataclasses import dataclass
from typing import Any

from . import _auth, _limits
from ._auth import UNSET
from ._errors import register
from ._files import build_mount
from ._middleware import make_gate
from ._routing import RouteInfo, build_route, route_shape


def check_prefix(prefix: str, what: str = "prefix") -> str:
    if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
        raise ValueError(
            f"{what} {prefix!r} must start with '/' and not end with one, "
            f"like '/users'; use '' for none"
        )
    return prefix


@dataclass(slots=True)
class _Declared:
    method: str
    path: str
    fn: Any
    tool: bool
    websocket: bool
    authorize: Any
    cancel_on_disconnect: bool = True
    blocking: bool = False
    auth: Any = UNSET
    rate_limit: Any = UNSET
    recheck: float | None = None


def _join(base: str, prefix: str) -> str:
    """A mount's prefix under a router's: `"/"` is the router's prefix itself."""
    if prefix == "/":
        return base or "/"
    return base + prefix


class Router:
    """A group of routes with a shared prefix, middleware, exception handlers,
    tags, `auth=` and `rate_limit=`."""

    def __init__(self, prefix: str = "", *, auth: Any = UNSET,
                 rate_limit: Any = UNSET, tags: Any = None) -> None:
        """`auth` applies to every route on the router and on routers it
        includes, unless one declares its own; a route's `auth=None` makes it
        public even here. See `oxbrook.auth`.

        `rate_limit` is likewise the `RateLimit` of every route that does not
        declare its own. It is one budget, shared by all of those routes.

        `tags` group the router's operations in the OpenAPI document and the
        docs page, and add to those of a router that includes this one."""
        self.prefix = check_prefix(prefix)
        self.auth = _auth.check(auth, f"Router({prefix!r})")
        self.rate_limit = _limits.check(rate_limit, f"Router({prefix!r})")
        if isinstance(tags, str):
            raise TypeError(f"Router({prefix!r}): tags is a list of names, not a string")
        self.tags: list[str] = list(tags or ())
        for tag in self.tags:
            if not isinstance(tag, str) or not tag:
                raise TypeError(f"Router({prefix!r}): each tag is a non-empty string, "
                                f"not {tag!r}")
        self._handlers: dict[type, Any] = {}
        self._declared: list[_Declared] = []
        self._middleware: list[Any] = []
        self._children: list[tuple[Router, str]] = []
        #: Static mounts, as (prefix on this router, directory, options).
        self._statics: list[tuple[str, Any, dict[str, Any]]] = []
        self._included = False

    def __repr__(self) -> str:
        return f"<Router prefix={self.prefix!r} routes={len(self._declared)}>"

    # ---- registration -------------------------------------------------------

    def _mutable(self, what: str) -> None:
        if self._included:
            raise RuntimeError(
                f"cannot add {what} to {self!r}: it has already been included, so "
                f"the addition would never be served. Include the router after "
                f"declaring everything on it"
            )

    def _declare(self, declared: _Declared) -> None:
        self._mutable(f"{declared.method} {declared.path}")
        full = self.prefix + declared.path
        if not full.startswith("/"):
            raise ValueError(
                f"{declared.method} {declared.path!r} on {self!r}: a path must start "
                f"with '/', or be '' on a router that has a prefix"
            )
        # Fails at the decorator, like a route on the app, rather than at the
        # distant `include` call.
        build_route(declared.fn, declared.method, full, websocket=declared.websocket,
                    tool=declared.tool, blocking=declared.blocking)
        shape = route_shape(full)
        for other in self._declared:
            if other.method == declared.method and route_shape(self.prefix + other.path) == shape:
                raise ValueError(
                    f"{declared.method} {full} conflicts with "
                    f"{other.method} {self.prefix + other.path} on the same router"
                )
        self._declared.append(declared)

    def route(
        self,
        method: str,
        path: str,
        tool: bool = False,
        *,
        cancel_on_disconnect: bool = True,
        blocking: bool = False,
        auth: Any = UNSET,
        rate_limit: Any = UNSET,
    ):
        """Register a route. See `App.route`."""
        method = method.upper()
        auth = _auth.check(auth, f"{method} {self.prefix}{path}")
        rate_limit = _limits.check(rate_limit, f"{method} {self.prefix}{path}")

        def decorator(fn):
            self._declare(
                _Declared(method, path, fn, tool, False, None, cancel_on_disconnect, blocking,
                          auth, rate_limit)
            )
            return fn

        return decorator

    def get(self, path: str, tool: bool = False, *, cancel_on_disconnect: bool = True,
            blocking: bool = False, auth: Any = UNSET, rate_limit: Any = UNSET):
        """Register a GET route. The arguments are `route`'s."""
        return self.route("GET", path, tool=tool,
                          cancel_on_disconnect=cancel_on_disconnect, blocking=blocking,
                          auth=auth, rate_limit=rate_limit)

    def post(self, path: str, tool: bool = False, *, cancel_on_disconnect: bool = True,
            blocking: bool = False, auth: Any = UNSET, rate_limit: Any = UNSET):
        """Register a POST route. The arguments are `route`'s."""
        return self.route("POST", path, tool=tool,
                          cancel_on_disconnect=cancel_on_disconnect, blocking=blocking,
                          auth=auth, rate_limit=rate_limit)

    def put(self, path: str, tool: bool = False, *, cancel_on_disconnect: bool = True,
            blocking: bool = False, auth: Any = UNSET, rate_limit: Any = UNSET):
        """Register a PUT route. The arguments are `route`'s."""
        return self.route("PUT", path, tool=tool,
                          cancel_on_disconnect=cancel_on_disconnect, blocking=blocking,
                          auth=auth, rate_limit=rate_limit)

    def patch(self, path: str, tool: bool = False, *, cancel_on_disconnect: bool = True,
            blocking: bool = False, auth: Any = UNSET, rate_limit: Any = UNSET):
        """Register a PATCH route. The arguments are `route`'s."""
        return self.route("PATCH", path, tool=tool,
                          cancel_on_disconnect=cancel_on_disconnect, blocking=blocking,
                          auth=auth, rate_limit=rate_limit)

    def delete(self, path: str, tool: bool = False, *, cancel_on_disconnect: bool = True,
            blocking: bool = False, auth: Any = UNSET, rate_limit: Any = UNSET):
        """Register a DELETE route. The arguments are `route`'s."""
        return self.route("DELETE", path, tool=tool,
                          cancel_on_disconnect=cancel_on_disconnect, blocking=blocking,
                          auth=auth, rate_limit=rate_limit)

    def websocket(self, path: str, authorize: Any = None, *, blocking: bool = False,
                  auth: Any = UNSET, rate_limit: Any = UNSET, recheck: float | None = None):
        """Register a WebSocket endpoint. See `App.websocket`."""
        auth = _auth.check(auth, f"WEBSOCKET {self.prefix}{path}")
        rate_limit = _limits.check(rate_limit, f"WEBSOCKET {self.prefix}{path}")
        recheck = _auth.interval(recheck, f"WEBSOCKET {self.prefix}{path}")

        def decorator(fn):
            self._declare(
                _Declared("GET", path, fn, False, True, authorize, True, blocking, auth,
                          rate_limit, recheck)
            )
            return fn

        return decorator

    def static(
        self,
        prefix: str,
        directory: Any,
        *,
        index: str | None = "index.html",
        fallback: str | None = None,
        dotfiles: bool = False,
        cache_control: str | None = None,
        precompressed: bool = False,
    ) -> None:
        """Serve the files in `directory` under `prefix` on this router. See
        `App.static`; `prefix` is joined to the router's own, and `"/"` means
        the router's prefix itself.

        Files are served before middleware and authentication, here as on the
        app. So a router with an `auth=` declaration, its own or one inherited
        from a router that includes it, cannot mount files: `include` refuses
        it rather than serve publicly what looks protected. Mount public files
        on a router without one, and serve protected files from a route.
        """
        self._mutable(f"static files at {prefix}")
        options = {"index": index, "fallback": fallback, "dotfiles": dotfiles,
                   "cache_control": cache_control, "precompressed": precompressed}
        # Checked here, at the call that is wrong, rather than at `include`.
        build_mount(_join(self.prefix, prefix), directory, **options)
        self._statics.append((prefix, directory, options))

    def exception_handler(self, exc_class: type):
        """Map an exception to a response for this router's routes, and the
        routes of routers it includes. See `App.exception_handler`.

        For those routes it takes precedence over the app's handler for the
        same class, and an included router's over this one's: the nearest
        declaration wins, as for `auth=`.
        """
        self._mutable(f"an exception handler for {getattr(exc_class, '__name__', exc_class)}")
        return register(self._handlers, exc_class, "Router.exception_handler()")

    def middleware(self, fn):
        """Middleware for this router's routes only, inside the app's own.

        Covers routes on routers included into this one as well. Like app
        middleware it wraps a socket's authorizer, not the socket handler.
        """
        self._mutable("middleware")
        self._middleware.append(fn)
        return fn

    def include(self, router: "Router", prefix: str = "") -> None:
        """Mount another router under this one."""
        self._mutable(f"router {router!r}")
        if router is self:
            raise ValueError("a router cannot include itself")
        self._children.append((router, check_prefix(prefix)))

    # ---- flattening ---------------------------------------------------------

    def _mounts(self, outer: str, seen: tuple = (), auth: Any = UNSET) -> list[tuple]:
        """Every static mount on this router and the ones it includes, as
        (full prefix, directory, options), refusing one that `auth=` covers."""
        if self in seen:
            raise ValueError(f"{self!r} includes itself through another router")
        base = outer + self.prefix
        auth = self.auth if self.auth is not UNSET else auth
        mounts = []
        for prefix, directory, options in self._statics:
            full = _join(base, prefix)
            if auth is not UNSET and auth is not None:
                raise ValueError(
                    f"static files at {full} are on a router with auth=, but files are "
                    f"served before authentication and would be public. Mount them on a "
                    f"router without auth=, or serve them from a route"
                )
            mounts.append((full, directory, options))
        for child, child_prefix in self._children:
            mounts.extend(child._mounts(base + child_prefix, (*seen, self), auth))
        return mounts

    def _flatten(
        self, outer: str, middleware: list[Any], seen: tuple = (), auth: Any = UNSET,
        rate_limit: Any = UNSET, tags: tuple = (), handlers: dict | None = None,
    ) -> list[RouteInfo]:
        if self in seen:
            raise ValueError(f"{self!r} includes itself through another router")
        self._included = True
        base = outer + self.prefix
        chain = middleware + self._middleware
        # Nearest wins: this router's declaration over the one it inherited.
        auth = self.auth if self.auth is not UNSET else auth
        rate_limit = self.rate_limit if self.rate_limit is not UNSET else rate_limit
        tags = (*tags, *(t for t in self.tags if t not in tags))
        handlers = {**(handlers or {}), **self._handlers}

        routes: list[RouteInfo] = []
        for declared in self._declared:
            route = build_route(
                declared.fn,
                declared.method,
                base + declared.path,
                websocket=declared.websocket,
                tool=declared.tool,
                cancel_on_disconnect=declared.cancel_on_disconnect,
                blocking=declared.blocking,
            )
            if declared.authorize is not None:
                route.authorizer = make_gate(declared.authorize)
            route.middleware = list(chain)
            route.auth = declared.auth if declared.auth is not UNSET else auth
            limit = declared.rate_limit if declared.rate_limit is not UNSET else rate_limit
            route.rate_limit = None if limit is UNSET else limit
            route.tags = list(tags)
            route.handlers = dict(handlers)
            route.recheck = declared.recheck
            routes.append(route)
        for child, child_prefix in self._children:
            routes.extend(child._flatten(base + child_prefix, chain, (*seen, self), auth,
                                         rate_limit, tags, handlers))
        return routes
