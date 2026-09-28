import inspect
import json
import os
import sys
from typing import Any

from . import _auth, _openapi
from ._auth import UNSET
from ._blocking import Pool as BlockingPool
from ._cors import CORS, check_origin
from ._errors import DEFAULT_HANDLERS, HTTPError
from ._errors import guard as guard_exceptions
from ._files import StaticMount, build_mount
from ._lifecycle import Lifecycle, ServerHandle, State, check_hook
from ._middleware import as_reply, make_gate
from ._middleware import wrap as wrap_middleware
from ._response import Response
from ._routers import Router, check_prefix
from ._routing import RouteInfo, build_route, route_shape
from ._streams import DROP_OLDEST, Topic
from ._workers import default_workers, gil_enabled

# Requests a single worker loop will accept at once, queued plus in-flight,
# before the server sheds load. Enough to absorb a burst of fast requests
# without letting a slow handler build a backlog that every client outlives.
DEFAULT_MAX_CONCURRENCY = 1024

#: Sockets held open at once. Separate from `max_concurrency`, which bounds
#: requests handed to a worker: an idle keep-alive connection costs a file
#: descriptor without ever reaching one.
DEFAULT_MAX_CONNECTIONS = 2048

#: Seconds to wait for a handler's first response before answering 504. Zero
#: disables it, for a service whose handlers are legitimately long-running.
DEFAULT_REQUEST_TIMEOUT = 30.0
#: Seconds to let in-flight requests finish after Ctrl-C before stopping.
DEFAULT_SHUTDOWN_GRACE = 10.0

#: Largest request body accepted, in bytes. Without a cap a single request can
#: grow the process by several times the payload before a handler sees it.
DEFAULT_MAX_BODY = 16 * 1024 * 1024

#: Largest single WebSocket message accepted, in bytes. Matches the body limit
#: rather than tungstenite's own 64 MiB, which was four times what the same
#: server would accept over HTTP and could not be changed.
DEFAULT_MAX_MESSAGE = 16 * 1024 * 1024


class App:
    def __init__(
        self,
        title: str = "Oxbrook",
        version: str = "0.1.0",
        description: str = "",
        openapi_url: str | None = "/openapi.json",
        docs_url: str | None = "/docs",
        mcp_url: str | None = "/mcp",
        debug: bool = False,
        access_log: bool = False,
        redis_url: str | None = None,
        lifespan: Any = None,
        worker_lifespan: Any = None,
        cors: CORS | None = None,
        websocket_origins: Any = None,
        auth: Any = None,
        mcp_auth: Any = _auth.UNSET,
    ) -> None:
        """`openapi_url` and `docs_url` can each be set to None to disable them.

        `debug` returns handler exception text in the 500 response. Leave it off
        outside development: exception messages routinely carry connection
        strings, file paths and user data.

        `access_log` adds one log line per request. Opt-in: it costs something
        per request, and many deployments already log at the proxy.

        `redis_url` enables durable topics. Nothing connects until the first
        durable topic is used.

        `mcp_url` is where agents reach the service over the Model Context
        Protocol. It exposes only routes marked `tool=True`, plus topics as
        readable resources. Set it to None to turn the endpoint off entirely.

        `lifespan` runs once around the server's life and `worker_lifespan` runs
        on every worker loop; what they yield is `request.state`. Two, because
        a connection pool belongs to the loop that made it and a server has
        several loops. See the lifespan guide.

        `cors` lets pages on other origins call the app from a browser. Applied
        in Rust, to every response including the ones no handler produced.

        `websocket_origins` lists the other origins whose pages may open a
        WebSocket. Browsers do not apply CORS to sockets and send cookies with
        the handshake, so without a check any website could open an
        authenticated socket as the user. A socket is accepted with no
        `Origin` (not a browser), from this server's own origin, or from a
        listed one; anything else is refused with `403` before an authorizer or
        handler runs. Left unset, the list is the CORS origins, excluding `*`.
        `["*"]` turns the check off.

        `auth` protects every route that does not declare its own, including
        WebSockets and MCP tool calls; a route that should stay public says
        `auth=None`. See `oxbrook.auth`. The OpenAPI document and the docs page
        stay public; turn them off with `openapi_url=None` and `docs_url=None`
        if the API's shape is not for everyone.

        `mcp_auth` guards the MCP endpoint itself, apart from `auth`: who may
        connect, list tools, and read topics as resources. Each tool is still
        guarded by its own route's declaration, and the tool list shows a
        caller only the tools it may call. Left unset, `/mcp` follows `auth`.
        When either names an `OIDC` provider, the endpoint also serves OAuth
        protected-resource metadata, and a `401` from it points there, so an
        MCP client can find where to log in.
        """
        self.routes: list[RouteInfo] = []
        self.mounts: list[StaticMount] = []
        self._middleware: list[Any] = []
        self._exception_handlers: dict[type, Any] = {}
        if access_log:
            from ._logging import access_middleware

            # First registered, so it wraps everything and sees the final status.
            self._middleware.append(access_middleware)
        self._topics: dict[str, Topic] = {}
        self.title = title
        self.version = version
        self.description = description
        self.openapi_url = openapi_url
        self.docs_url = docs_url
        self.mcp_url = mcp_url
        #: Set by _register_mcp, so shutdown can close live agent sessions.
        self._mcp: Any = None
        #: One threadpool for every blocking route on this app. Sized at run().
        self._blocking = BlockingPool()
        #: How many worker loops this app will run, known once a server is
        #: built and None before. `worker_lifespan` needs it to size anything
        #: it opens per loop — see `per_worker`.
        self.workers: int | None = None
        self.debug = debug
        self.redis_url = redis_url
        self._backend: Any = None
        if cors is not None and not isinstance(cors, CORS):
            raise TypeError(f"cors must be a CORS(...), got {type(cors).__name__}")
        self.cors = cors
        if websocket_origins is not None:
            if isinstance(websocket_origins, str):
                raise TypeError("websocket_origins is a list of origins, not a single string")
            websocket_origins = tuple(check_origin(o) for o in websocket_origins)
        self.websocket_origins = websocket_origins
        self.lifespan = check_hook(lifespan, "lifespan")
        self.worker_lifespan = check_hook(worker_lifespan, "worker_lifespan")
        #: What `lifespan` yielded, while a server is running. Empty otherwise.
        self.state = State()
        #: The declaration every route without its own gets; None for none.
        self.auth = _auth.check(auth, "App(auth=...)") or None
        #: The MCP endpoint's own declaration; UNSET follows `auth`.
        self.mcp_auth = _auth.check(mcp_auth, "App(mcp_auth=...)")
        #: The path of the endpoint's protected-resource metadata, when served.
        self._resource_metadata: str | None = None
        #: Whether the server built from this app speaks TLS; `Basic` asks.
        self._serving_tls = False

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
        """Register a route.

        `auth` declares who may call it, overriding its routers' and the app's;
        `auth=None` makes it public. See `oxbrook.auth`.

        `blocking=True` lets the handler be a plain `def` and runs it on a
        threadpool instead of its worker loop. A worker loop serves many
        requests at once by interleaving them at every `await`, so a handler
        that takes time *without* awaiting — a sync database driver,
        `requests`, `boto3`, Pillow — freezes every other request that loop is
        serving. Declaring the route blocking is what keeps it off the loop,
        and it is opt-in so that the cost is visible where the route is.

        The threadpool is shared by the whole process and bounded, so blocking
        calls queue rather than multiplying threads. A blocking handler cannot
        be cancelled once it has started: a thread inside a blocking call
        cannot be interrupted, so a client that leaves frees the loop but not
        the thread.

        `tool=True` also exposes it to agents over MCP. Opt-in on purpose:
        every route being agent-callable by default would mean an
        administrative delete endpoint is agent-callable by default.

        A handler is cancelled when its client disconnects, or its request
        times out, before it has answered: `asyncio.CancelledError` is raised
        at the handler's next `await`, and `finally` blocks and dependency
        teardown run as usual. Its answer could no longer reach anyone, and
        left running it would hold a worker's capacity for nothing. Wrap a
        step that must finish in `asyncio.shield`, or pass
        `cancel_on_disconnect=False` to let the whole handler run to the end.
        Once a handler has answered — including a stream that has started —
        this no longer applies.
        """
        method = method.upper()
        auth = _auth.check(auth, f"{method} {path}")

        def decorator(fn):
            # Validates the handler against its path and fails here, at import
            # time, rather than on the first request.
            route = build_route(fn, method, path, tool=tool,
                                cancel_on_disconnect=cancel_on_disconnect,
                                blocking=blocking)
            route.auth = auth
            self._add(route)
            return fn

        return decorator

    def _add(self, route: RouteInfo) -> None:
        """Register a route, refusing one the router could not hold.

        The radix tree rejects two routes of the same shape, and it is built
        when the server starts — on whatever thread called `serve`, which for
        the test client is a background thread where the error becomes a
        connection refused and nothing else. Checked here instead, so a
        duplicate fails at import, pointing at the decorator that caused it.

        A WebSocket route is registered as a GET, so it collides with a GET on
        the same path, which is the correct answer: only one of them could
        ever run.
        """
        shape = route_shape(route.path)
        if route.method == "GET":
            for mount in self.mounts:
                if shape in mount.shapes():
                    raise ValueError(
                        f"GET {route.path} conflicts with the static files mounted at "
                        f"{mount.prefix}: the router cannot hold both"
                    )
        for existing in self.routes:
            if existing.method == route.method and route_shape(existing.path) == shape:
                same = existing.path == route.path
                raise ValueError(
                    f"{route.method} {route.path} conflicts with "
                    f"{existing.method} {existing.path}: "
                    + (
                        "the same route is registered twice"
                        if same
                        else "the paths differ only in parameter names, which the "
                        "router cannot tell apart"
                    )
                )
        if route.pool is not None:
            # A Router builds its routes before it knows an app, so this is the
            # first moment a blocking route can be told where to run.
            route.pool.pool = self._blocking
        # Likewise the app's own declaration: the last one out, applied here.
        route.auth = _auth.resolve(route.auth, self.auth)
        self.routes.append(route)

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

    def static(
        self,
        prefix: str,
        directory: Any,
        *,
        index: str | None = "index.html",
        fallback: str | None = None,
        dotfiles: bool = False,
        cache_control: str | None = None,
    ) -> None:
        """Serve the files in `directory` under the URL `prefix`.

            app.static("/assets", "public")
            app.static("/", "frontend/dist", fallback="index.html")

        Served in Rust, streamed from disk, with conditional and range requests
        answered. A file request never wakes a Python worker, and middleware does
        not run for it.

        `index` is served for a directory; `None` makes directories 404. A path
        naming a directory without its trailing slash is redirected to it.

        `fallback` is served, with status 200, for a page load under the prefix
        that matches no file: no file extension, and `Accept` including
        `text/html`, as a browser sends when loading a page. That is how a
        single-page app's client-side routes load. A missing `app.js` is still
        404, and so is a mistyped API call from `fetch`, which does not ask for
        HTML.

        `dotfiles` allows files and directories whose names start with a dot.
        Off by default: `.env` and `.git` are the files most likely to sit in a
        static directory by accident.

        `cache_control` sets that header on every file served.

        `directory` resolves from the working directory when relative, and must
        exist now. Paths that leave the directory — `..`, encoded or not, or a
        symlink pointing outside it — are answered 404.
        """
        mount = build_mount(prefix, directory, index=index, fallback=fallback,
                            dotfiles=dotfiles, cache_control=cache_control)
        taken = set(mount.shapes())
        for route in self.routes:
            if route.method == "GET" and route_shape(route.path) in taken:
                raise ValueError(
                    f"static files at {prefix} conflict with {route.method} {route.path}: "
                    f"the router cannot hold both"
                )
        for existing in self.mounts:
            if taken & set(existing.shapes()):
                raise ValueError(f"static files at {prefix} conflict with the mount at "
                                 f"{existing.prefix}")
        self.mounts.append(mount)

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

    def include(self, router: Router, prefix: str = "") -> None:
        """Mount a router's routes, under `prefix` if given.

            app.include(users.router, prefix="/api/v1")

        Every route is validated against its full path here, and checked
        against the routes already registered, so a conflict between two
        modules fails at this call rather than at startup. Include after the
        router is fully declared: it cannot change afterwards.
        """
        if not isinstance(router, Router):
            raise TypeError(f"include() needs a Router, got {type(router).__name__}")
        for route in router._flatten(check_prefix(prefix), []):
            self._add(route)

    def middleware(self, fn):
        """Register middleware, which runs around every HTTP handler.

            @app.middleware
            async def require_key(request, call_next):
                if request.header("x-api-key") != SECRET:
                    return Reply({"error": "unauthorized"}, status=401)
                return await call_next(request)

        Runs outermost-first in registration order. WebSocket routes are not
        wrapped: their handshake completes before the handler runs, so there is
        nothing useful to intercept yet.
        """
        self._middleware.append(fn)
        return fn

    def exception_handler(self, exc_class: type):
        """Register how an exception becomes a response.

            @app.exception_handler(LookupError)
            async def missing(request, exc):
                return Reply({"error": "not found"}, status=404)

        Applies to the class and its subclasses; the most specific registered
        class wins. Raised by a handler, a dependency, body validation, an
        authorizer or middleware, the exception is mapped before middleware
        sees the result, so the access log records the real status.

        Registering `HTTPError` or `RequestValidationError` replaces the
        built-in response for it. See `oxbrook.HTTPError`.
        """
        if not (isinstance(exc_class, type) and issubclass(exc_class, Exception)):
            raise TypeError(
                f"exception_handler() needs an Exception subclass, got {exc_class!r}"
            )

        def decorator(fn):
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(
                    f"exception handler {getattr(fn, '__qualname__', fn)} for "
                    f"{exc_class.__name__} must be `async def`"
                )
            existing = self._exception_handlers.get(exc_class)
            if existing is not None:
                raise ValueError(
                    f"{exc_class.__name__} already has a handler, "
                    f"{getattr(existing, '__qualname__', existing)}"
                )
            self._exception_handlers[exc_class] = fn
            return fn

        return decorator

    def _handlers(self) -> dict[type, Any]:
        return {**DEFAULT_HANDLERS, **self._exception_handlers}

    def _layer(self, target: Any, chain: list[Any], handlers: dict[type, Any]) -> Any:
        """Exception mapping around the handler and around every middleware link,
        so each middleware sees a reply from everything inside it — including an
        `HTTPError` raised by an inner router's middleware — and never an
        exception that already has a handler."""
        target = guard_exceptions(target, handlers)
        if not chain:
            return target

        def around(call):
            guarded = guard_exceptions(call, handlers)

            async def link(request):
                return as_reply(await guarded(request))

            return link

        return wrap_middleware(target, chain, around)

    def _compose(self, target: Any, route: RouteInfo, socket: bool = False) -> Any:
        """What actually runs for a route served over HTTP, or for a socket's
        upgrade when `socket` is set.

        The order is the app's middleware, then authentication, then the
        routers' middleware, then the handler. Authentication inside the app's
        middleware so the access log records a refusal; outside the routers'
        so none of theirs runs for a caller who was refused.

        A route with no middleware, no auth and no registered exception
        handlers is left exactly as it was, so the common path pays nothing;
        the runtime applies the two built-in defaults itself.
        """
        inner = list(route.middleware)
        chain = self._middleware + inner
        if route.auth is None:
            if not chain and not self._exception_handlers:
                return target
            return self._layer(target, chain, self._handlers())
        if self._resource_metadata is not None and route.path == self.mcp_url:
            gate = _auth.ResourceGate(route.auth, self._resource_metadata)
        else:
            gate = _auth.Gate(route.auth)
        if socket:
            link = gate.socket_middleware()
        elif not chain and not self._exception_handlers:
            return gate.wrap(target)
        else:
            link = gate.middleware()
        return self._layer(target, [*self._middleware, link, *inner], self._handlers())

    def _tool_target(self, route: RouteInfo) -> Any:
        """What runs when a route is called as an MCP tool.

        The route's own authentication, its router middleware and the
        exception handlers, but not the app's middleware: that already ran,
        around the `/mcp` request that carried the call, and running it again
        would log twice. Authentication is the route's, with the agent's
        headers, so a tool is refused whatever `/mcp` itself accepts; router
        middleware must not be skipped either, since a router may guard with
        it.
        """
        link = _auth.forget if route.auth is None else _auth.Gate(route.auth).middleware()
        return self._layer(route.target, [link, *route.middleware], self._handlers())

    def per_worker(self, total: int) -> int:
        """One worker loop's share of a process-wide budget.

        Anything `worker_lifespan` opens is opened once per loop, so a number
        written there is multiplied by however many loops this process runs —
        which is decided by the machine, not by the code. A pool of ten on an
        eight-loop host is eighty connections, and a database that allows a
        hundred has room for one such process.

        Say what the *process* may use and divide it here:

            async def worker_lifespan(app):
                pool = await asyncpg.create_pool(
                    DSN, min_size=1, max_size=app.per_worker(40)
                )
                try:
                    yield {"db": pool}
                finally:
                    await pool.close()

        Rounds down, because going over a budget for connections is the
        dangerous direction: forty across three loops is thirteen each, not
        fourteen. A loop cannot have less than one, so a budget smaller than
        the number of loops cannot be honoured and says so rather than
        returning zero.
        """
        workers = self.workers
        if workers is None:
            raise RuntimeError(
                "per_worker() needs to know how many worker loops there are, "
                "which is decided when the server is built. Call it inside "
                "worker_lifespan, not at import time"
            )
        if not isinstance(total, int) or total < 1:
            raise ValueError(f"per_worker() needs a positive integer, got {total!r}")
        if total < workers:
            raise ValueError(
                f"per_worker({total}) cannot be shared across {workers} worker "
                f"loops: each one needs at least one, so the smallest workable "
                f"budget on this machine is {workers}. Run fewer loops with "
                f"app.run(workers=...) if the budget is the fixed part"
            )
        return total // workers

    def websocket(self, path: str, authorize: Any = None, *, blocking: bool = False,
                  auth: Any = UNSET):
        """Register a WebSocket endpoint.

        `auth` is checked before the handshake, as for any route, so a caller
        who is refused gets a real `401` or `403` rather than a socket that
        opens and closes. Browsers cannot set headers on a WebSocket, so for a
        page the credential is a cookie — `SessionAuth`. The check runs when
        the socket opens: a connection can outlive the credential that opened
        it.

        `authorize` runs after `auth`, before the handshake, and can refuse the upgrade,
        which the handler cannot: by the time it runs, the 101 has been sent
        and the client believes it is connected. Return None or True to
        accept; raise `HTTPError`, or return a `Response`/`Reply`, to refuse.

            async def members_only(request):
                if not valid(request.header("authorization")):
                    raise HTTPError(401, headers={"www-authenticate": "Bearer"})

            @app.websocket("/ws", authorize=members_only)
            async def feed(request, ws): ...
        

        The handler takes the request and the socket. Oxbrook performs the
        handshake, so the socket is already open when the handler runs, and the
        connection closes when it returns.

            @app.websocket("/ws")
            async def echo(request, ws):
                async for message in ws:
                    await ws.send(message)
        """

        auth = _auth.check(auth, f"WEBSOCKET {path}")

        def decorator(fn):
            route = build_route(fn, "GET", path, websocket=True, blocking=blocking)
            if authorize is not None:
                route.authorizer = make_gate(authorize)
            route.auth = auth
            self._add(route)
            return fn

        return decorator

    def backend(self) -> Any:
        """The shared Redis backend, connected lazily on first use."""
        if self._backend is None:
            if not self.redis_url:
                raise RuntimeError(
                    "durable topics need a redis_url: App(redis_url='redis://...')"
                )
            from ._redis import RedisBackend

            self._backend = RedisBackend(self.redis_url)
        return self._backend

    def topic(
        self,
        name: str,
        maxsize: int | None = None,
        policy: str | None = None,
        durable: bool = False,
    ) -> Topic:
        """Get or create a named topic.

        Shared across every worker loop in the process, so a message emitted by
        one handler reaches subscribers running on all of them. `durable=True`
        additionally shares it across processes and records it in Redis, which
        needs `App(redis_url=...)`.

        `maxsize`, `policy` and `durable` apply only when the topic is first
        created.
        """
        existing = self._topics.get(name)
        if existing is not None:
            return existing
        created = Topic(
            name,
            maxsize=maxsize or 1024,
            policy=policy or DROP_OLDEST,
            backend=self.backend() if durable else None,
        )
        # Racing handlers could both create one; keep whichever landed first so
        # every worker sees the same object.
        return self._topics.setdefault(name, created)

    @property
    def topics(self) -> dict[str, Topic]:
        return dict(self._topics)

    def capabilities(self) -> dict[str, Any]:
        """The capabilities this service exposes to agents.

        Built from the routes marked `tool=True`, without starting a server, so
        it can be inspected or checked into a test.
        """
        from . import _capabilities

        return _capabilities.build(self.routes, self._tool_target)

    def openapi(self) -> dict[str, Any]:
        """The OpenAPI 3.1 document for the routes registered so far.

        Built from the same metadata the router uses, so it cannot describe an
        endpoint the server would not accept. Callable without running the
        server, which makes it usable for client generation in CI.
        """
        return _openapi.build(self.routes, self.title, self.version, self.description,
                              default_auth=self.auth)

    def _register_mcp(self) -> None:
        """Add the agent endpoint, unless it was turned off."""
        if not self.mcp_url:
            return
        if ("POST", self.mcp_url) in {(r.method, r.path) for r in self.routes}:
            return

        from ._mcp import MCP

        # Capabilities are built once, at start, so a malformed one is an error
        # at boot rather than on an agent's first call.
        capabilities = self.capabilities()
        server = MCP(self, capabilities)
        self._mcp = server
        declared = _auth.resolve(self.mcp_auth, self.auth)
        issuers, scopes = _auth.authorization_servers(
            [declared, *(c.route.auth for c in capabilities.values())]
        )
        if issuers:
            self._register_resource_metadata(issuers, scopes)

        # All three on one path, which is what Streamable HTTP means by "a
        # single endpoint". POST carries messages, GET is the stream the server
        # talks on, DELETE ends the session.
        #
        # `cancel_on_disconnect` is off for the GET: the stream ending *is* the
        # client leaving, and cancelling the handler that owns the subscription
        # would race the generator's own cleanup.
        @self.post(self.mcp_url, auth=self.mcp_auth)
        async def mcp_endpoint(request):
            """Model Context Protocol endpoint."""
            return await server.post(request)

        @self.get(self.mcp_url, cancel_on_disconnect=False, auth=self.mcp_auth)
        async def mcp_stream(request):
            """Model Context Protocol server-to-client stream."""
            return await server.get(request)

        @self.delete(self.mcp_url, auth=self.mcp_auth)
        async def mcp_end(request):
            """End a Model Context Protocol session."""
            return await server.delete(request)

    def _register_resource_metadata(self, issuers: list[str], scopes: list[str]) -> None:
        """OAuth protected-resource metadata for the MCP endpoint (RFC 9728).

        At the path-suffixed well-known URI, which is where the metadata of a
        resource with a path lives and where MCP clients look first. Public,
        since a client reads it precisely because it has no token yet.
        """
        path = "/.well-known/oauth-protected-resource" + self.mcp_url
        if ("GET", path) in {(r.method, r.path) for r in self.routes}:
            return
        self._resource_metadata = path
        mcp_url, title = self.mcp_url, self.title

        @self.get(path, auth=None)
        async def protected_resource(request):
            """OAuth protected-resource metadata for the MCP endpoint."""
            base = _auth.public_base(request)
            if base is None:
                raise HTTPError(400, "a Host header is needed to name the resource")
            document = {
                "resource": base + mcp_url,
                "authorization_servers": issuers,
                "bearer_methods_supported": ["header"],
                "resource_name": title,
            }
            if scopes:
                document["scopes_supported"] = scopes
            return document

    def _register_docs(self) -> None:
        """Add the OpenAPI and docs routes, unless the user turned them off."""
        registered = {(r.method, r.path) for r in self.routes}

        if self.openapi_url and ("GET", self.openapi_url) not in registered:
            # Serialized once at startup, not per request.
            document = json.dumps(self.openapi()).encode()

            @self.get(self.openapi_url, auth=None)
            async def openapi_json(_request):
                """OpenAPI schema."""
                return Response(document, content_type="application/json")

        if self.docs_url and self.openapi_url and ("GET", self.docs_url) not in registered:
            page = _openapi.DOCS_TEMPLATE.format(
                title=self.title, openapi_url=self.openapi_url
            ).encode()

            @self.get(self.docs_url, auth=None)
            async def docs(_request):
                """API documentation."""
                return Response(page, content_type="text/html; charset=utf-8")

    def run(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        workers: int | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_body: int = DEFAULT_MAX_BODY,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_message: int = DEFAULT_MAX_MESSAGE,
        blocking_threads: int | None = None,
        *,
        tls_cert: str | os.PathLike[str] | None = None,
        tls_key: str | os.PathLike[str] | None = None,
        http2: bool = True,
    ) -> None:
        """Serve until interrupted.

        `max_concurrency` bounds the requests a single worker loop will accept
        at once, counting both those queued and those already running. When
        every worker is at its limit the server answers 503 rather than growing
        without bound. Lower it for slow handlers, where a deep backlog only
        adds latency before an inevitable client timeout; raise it to absorb
        larger bursts of fast requests.

        `max_body` caps a request body; anything larger is answered 413 without
        being buffered. `max_message` does the same for a single WebSocket
        message, where the connection is closed rather than answered.

        `request_timeout` is how long to wait for a handler's first response
        before answering 504, and the handler is then cancelled unless its route
        says `cancel_on_disconnect=False`. It does not cut short a stream that
        has already started, so SSE and WebSocket are unaffected. Zero disables
        it.

        `shutdown_grace` is how long Ctrl-C waits for in-flight requests to
        finish before stopping anyway.

        `max_connections` caps sockets held open. At the limit the server stops
        accepting rather than refusing, so the wait lands in the OS backlog.

        `tls_cert` and `tls_key` are PEM files, the certificate chain with the
        server's certificate first and its private key. Given both, the server
        speaks HTTPS only; they are read once, at startup.

        `http2` serves HTTP/2 alongside HTTP/1.1: negotiated through ALPN over
        TLS, and recognised by its opening bytes on a plain connection, where
        only clients configured for it will use it. `False` serves HTTP/1.1
        alone.

        `blocking_threads` sizes the one threadpool every `blocking=True` route
        shares. The default is `min(32, cpu + 4)` — what CPython would give a
        single event loop, given to all of them together rather than to each.
        Raise it for handlers that mostly wait on a network or a disk; it
        bounds how many blocking calls run at once, and the rest queue.
        """
        server = self.build_server(
            host, port, workers, max_concurrency, max_body, request_timeout,
            shutdown_grace, max_connections, max_message, announce=True,
            blocking_threads=blocking_threads,
            tls_cert=tls_cert, tls_key=tls_key, http2=http2,
        )
        try:
            server.serve()
        except KeyboardInterrupt:
            pass

    def build_server(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        workers: int | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_body: int = DEFAULT_MAX_BODY,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_message: int = DEFAULT_MAX_MESSAGE,
        announce: bool = False,
        blocking_threads: int | None = None,
        *,
        tls_cert: str | os.PathLike[str] | None = None,
        tls_key: str | os.PathLike[str] | None = None,
        http2: bool = True,
    ):
        """Prepare a server without starting it.

        `run` uses this; so does the test client, which needs to start the
        server on one thread and stop it from another. The lifespans run inside
        `serve`, not here.
        """
        from ._core import Server

        # Sized before anything can use it; the pool starts no thread until a
        # blocking handler actually runs, so a server without one pays nothing.
        if blocking_threads is not None:
            self._blocking = BlockingPool(blocking_threads)
            for route in self.routes:
                if route.pool is not None:
                    route.pool.pool = self._blocking

        if (tls_cert is None) != (tls_key is None):
            raise ValueError("tls_cert and tls_key are needed together")
        tls = None if tls_cert is None else (os.fspath(tls_cert), os.fspath(tls_key))
        self._serving_tls = tls is not None
        workers = workers or default_workers()
        self.workers = workers
        mode = "GIL" if gil_enabled() else "free-threaded"
        if announce:
            print(
                f"Oxbrook: {workers} worker loop(s), max {max_concurrency} concurrent/worker, "
                f"{mode} Python {sys.version_info.major}.{sys.version_info.minor}"
                f"{', debug' if self.debug else ''}",
                flush=True,
            )
        # Docs first: the document is built from the routes registered so far,
        # so registering /mcp afterwards keeps it out of the OpenAPI paths.
        self._register_docs()
        self._register_mcp()
        specs = [self._spec(r) for r in self.routes]
        lifecycle = Lifecycle(self)
        core = Server(
            host,
            port,
            workers,
            max_concurrency,
            max_body,
            max_message,
            self.debug,
            request_timeout,
            shutdown_grace,
            max_connections,
            not announce,
            specs,
            lifecycle,
            None if self.cors is None else self.cors.as_spec(),
            self._socket_origins(),
            [mount.as_spec() for mount in self.mounts],
            tls,
            bool(http2),
        )
        return ServerHandle(core, lifecycle)

    def _spec(self, r: RouteInfo) -> tuple:
        """The tuple the Rust server takes for one route."""
        if r.websocket:
            # The handler is left alone: its handshake is already done, so
            # there is nothing for middleware or a mapped status to act on. The
            # gate before the handshake gets both instead, and authentication,
            # so an app-wide rule and an `HTTPError(401)` cover sockets too.
            handler = r.target if r.auth is None else _auth.receive(r.target)
            authorizer = r.authorizer
            if authorizer is None and r.auth is not None:
                authorizer = make_gate(_auth.accept)
            gate = None if authorizer is None else self._compose(authorizer, r, socket=True)
            return (r.method, r.path, handler, [p.as_spec() for p in r.params], True, gate,
                    "collect", False)
        if r.stream is not None:
            body = "stream"
        elif r.auth is not None:
            # Read after authentication, so a refused caller never makes the
            # server take in what they sent.
            body = "defer"
        else:
            body = "collect"
        return (r.method, r.path, self._compose(r.target, r), [p.as_spec() for p in r.params],
                False, None, body, r.cancel_on_disconnect)

    def _socket_origins(self) -> tuple[bool, list[str]]:
        """(any origin, allowed origins) for the upgrade check.

        CORS `*` never opens sockets: it is the setting that forbids
        credentials, and a socket handshake always carries them.
        """
        if self.websocket_origins is not None:
            listed = self.websocket_origins
        elif self.cors is not None:
            listed = tuple(o for o in self.cors.allow_origins if o != "*")
        else:
            listed = ()
        return "*" in listed, [o for o in listed if o != "*"]
