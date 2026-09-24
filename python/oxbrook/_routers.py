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

from . import _auth
from ._auth import UNSET
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


class Router:
    """A group of routes with a shared prefix, middleware and `auth=`."""

    def __init__(self, prefix: str = "", *, auth: Any = UNSET) -> None:
        """`auth` applies to every route on the router and on routers it
        includes, unless one declares its own; a route's `auth=None` makes it
        public even here. See `oxbrook.auth`."""
        self.prefix = check_prefix(prefix)
        self.auth = _auth.check(auth, f"Router({prefix!r})")
        self._declared: list[_Declared] = []
        self._middleware: list[Any] = []
        self._children: list[tuple[Router, str]] = []
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
    ):
        """Register a route. See `App.route`."""
        method = method.upper()
        auth = _auth.check(auth, f"{method} {self.prefix}{path}")

        def decorator(fn):
            self._declare(
                _Declared(method, path, fn, tool, False, None, cancel_on_disconnect, blocking,
                          auth)
            )
            return fn

        return decorator

    def get(self, path: str, tool: bool = False, *, cancel_on_disconnect: bool = True,
            blocking: bool = False, auth: Any = UNSET):
        return self.route("GET", path, tool=tool,
                          cancel_on_disconnect=cancel_on_disconnect, blocking=blocking,
                          auth=auth)

    def post(self, path: str, tool: bool = False, *, cancel_on_disconnect: bool = True,
            blocking: bool = False, auth: Any = UNSET):
        return self.route("POST", path, tool=tool,
                          cancel_on_disconnect=cancel_on_disconnect, blocking=blocking,
                          auth=auth)

    def put(self, path: str, tool: bool = False, *, cancel_on_disconnect: bool = True,
            blocking: bool = False, auth: Any = UNSET):
        return self.route("PUT", path, tool=tool,
                          cancel_on_disconnect=cancel_on_disconnect, blocking=blocking,
                          auth=auth)

    def patch(self, path: str, tool: bool = False, *, cancel_on_disconnect: bool = True,
            blocking: bool = False, auth: Any = UNSET):
        return self.route("PATCH", path, tool=tool,
                          cancel_on_disconnect=cancel_on_disconnect, blocking=blocking,
                          auth=auth)

    def delete(self, path: str, tool: bool = False, *, cancel_on_disconnect: bool = True,
            blocking: bool = False, auth: Any = UNSET):
        return self.route("DELETE", path, tool=tool,
                          cancel_on_disconnect=cancel_on_disconnect, blocking=blocking,
                          auth=auth)

    def websocket(self, path: str, authorize: Any = None, *, blocking: bool = False,
                  auth: Any = UNSET):
        """Register a WebSocket endpoint. See `App.websocket`."""
        auth = _auth.check(auth, f"WEBSOCKET {self.prefix}{path}")

        def decorator(fn):
            self._declare(
                _Declared("GET", path, fn, False, True, authorize, True, blocking, auth)
            )
            return fn

        return decorator

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

    def _flatten(
        self, outer: str, middleware: list[Any], seen: tuple = (), auth: Any = UNSET
    ) -> list[RouteInfo]:
        if self in seen:
            raise ValueError(f"{self!r} includes itself through another router")
        self._included = True
        base = outer + self.prefix
        chain = middleware + self._middleware
        # Nearest wins: this router's declaration over the one it inherited.
        auth = self.auth if self.auth is not UNSET else auth

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
            routes.append(route)
        for child, child_prefix in self._children:
            routes.extend(child._flatten(base + child_prefix, chain, (*seen, self), auth))
        return routes
