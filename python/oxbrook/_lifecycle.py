"""Startup and shutdown: what exists for the life of a server.

    @asynccontextmanager
    async def lifespan(app):
        model = load_model()                 # once, before any worker starts
        yield {"model": model}

    @asynccontextmanager
    async def worker_lifespan(app):
        pool = await create_pool(DSN)        # once per worker loop
        try:
            yield {"db": pool}
        finally:
            await pool.close()

    app = App(lifespan=lifespan, worker_lifespan=worker_lifespan)

    @app.get("/users")
    async def users(request):
        return await request.state.db.fetch("select ...")

**Why there are two.** Oxbrook runs one asyncio loop per worker thread, all in
one process. An asyncio connection pool, an `httpx.AsyncClient` or a Redis
client belongs to the loop that created it and cannot be used from another. A
single startup hook would build one pool usable by one loop out of several, and
the failure would look like an intermittent "attached to a different loop"
error under load. So:

* `lifespan` runs once, on its own loop, before the workers start and after they
  stop. That loop is not running while requests are served, so what it yields
  must not depend on it: configuration, a loaded model, a thread-safe client.
  It is also where one-off work belongs, such as a migration or registering
  with service discovery.
* `worker_lifespan` runs on every worker loop, before that loop takes a request
  and after it has finished its last. Anything bound to a loop goes here.

Either is an async context manager factory taking the app, or a plain async
generator function, which is treated as one. What it yields is a mapping, or
nothing. The two are merged into `request.state`, which is read-only: it is
shared by every request on a loop, so a write from one request would be read by
the next. A key yielded by both is an error rather than a silent override.

Startup failures stop the server before it accepts a connection, and the
exception is the one the lifespan raised. Worker loops that had already started
are shut down, running their teardown, first.
"""

import asyncio
import contextlib
import inspect
from collections.abc import Iterator, Mapping
from typing import Any


class State(Mapping):
    """Read-only values yielded by the lifespans. `state.db` or `state["db"]`."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping | None = None) -> None:
        object.__setattr__(self, "_values", dict(values or {}))

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError:
            known = ", ".join(sorted(self._values)) or "nothing"
            raise AttributeError(
                f"state has no {name!r}; the lifespans yielded {known}"
            ) from None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            "state is read-only: it is shared by every request on a worker loop. "
            "Yield values from a lifespan instead"
        )

    def __repr__(self) -> str:
        return f"State({', '.join(sorted(self._values))})"


class WorkerContext:
    """What a worker loop hands to each request it serves."""

    __slots__ = ("app", "lifecycle", "manager", "state")

    def __init__(
        self, app: Any, state: State, manager: Any = None, lifecycle: Any = None
    ) -> None:
        self.app = app
        self.state = state
        self.manager = manager
        #: The server's `Lifecycle`, through which a readiness check reaches
        #: the other loops of the same server.
        self.lifecycle = lifecycle


def check_hook(hook: Any, name: str) -> Any:
    if hook is not None and not callable(hook):
        raise TypeError(f"{name} must be callable, got {type(hook).__name__}")
    return hook


def _manager(hook: Any, app: Any, name: str) -> Any:
    """An async context manager from either accepted shape of hook."""
    if inspect.isasyncgenfunction(hook):
        return contextlib.asynccontextmanager(hook)(app)
    manager = hook(app)
    if not (hasattr(manager, "__aenter__") and hasattr(manager, "__aexit__")):
        raise TypeError(
            f"{name} must be an async context manager factory, such as a function "
            f"decorated with @asynccontextmanager, or an async generator function; "
            f"calling it returned {type(manager).__name__}"
        )
    return manager


def _as_mapping(value: Any, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must yield a mapping or nothing, got {type(value).__name__}")
    return dict(value)


class Lifecycle:
    """Runs one app's lifespans for one server. Per server, never module state."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._process_manager: Any = None
        #: (loop, WorkerContext) for every loop started, so readiness checks
        #: can run on each of them. Appended from the loops' own threads, which
        #: list.append is safe for. Never pruned: loops stop only after the
        #: server has begun draining, and a draining server answers readiness
        #: itself without reaching a check.
        self.loops: list[tuple[asyncio.AbstractEventLoop, WorkerContext]] = []

    # ---- process ------------------------------------------------------------

    async def start_process(self) -> None:
        hook = self.app.lifespan
        if hook is None:
            self.app.state = State()
            return
        manager = _manager(hook, self.app, "lifespan")
        values = _as_mapping(await manager.__aenter__(), "lifespan")
        self._process_manager = manager
        self.app.state = State(values)

    async def stop_process(self) -> None:
        manager, self._process_manager = self._process_manager, None
        # Agent sessions hold an open SSE stream each. Closed explicitly, so a
        # shutdown ends them rather than leaving them to the collector.
        mcp = getattr(self.app, "_mcp", None)
        if mcp is not None:
            mcp.sessions.close()
        # Same rule for the blocking threadpool: a held resource is released
        # because something released it, not because nothing points at it.
        pool = getattr(self.app, "_blocking", None)
        if pool is not None:
            pool.close()
        try:
            if manager is not None:
                await manager.__aexit__(None, None, None)
        finally:
            # Whatever it yielded has just been torn down.
            self.app.state = State()

    # ---- worker loops -------------------------------------------------------

    async def start_worker(self) -> WorkerContext:
        """Called by each worker thread on its own loop, before it serves."""
        base = self.app.state
        hook = self.app.worker_lifespan
        if hook is None:
            return self._serving(WorkerContext(self.app, base, lifecycle=self))
        manager = _manager(hook, self.app, "worker_lifespan")
        values = _as_mapping(await manager.__aenter__(), "worker_lifespan")
        clash = sorted(set(values) & set(base))
        if clash:
            await manager.__aexit__(None, None, None)
            raise ValueError(
                f"worker_lifespan yielded {', '.join(map(repr, clash))}, which lifespan "
                f"already yielded; each key must come from one of them"
            )
        return self._serving(
            WorkerContext(self.app, State({**base, **values}), manager, lifecycle=self)
        )

    def _serving(self, context: WorkerContext) -> WorkerContext:
        self.loops.append((asyncio.get_running_loop(), context))
        return context

    async def stop_worker(self, context: WorkerContext) -> None:
        """Called by each worker thread after its loop has stopped serving."""
        if context.manager is not None:
            await context.manager.__aexit__(None, None, None)


class ServerHandle:
    """A server with an app's lifespans around it.

    `serve` blocks and `shutdown` may be called from another thread, the same
    two methods the native server has, so the test client and anything else
    driving a server does not need to know which it holds.
    """

    def __init__(self, core: Any, lifecycle: Lifecycle) -> None:
        self._core = core
        self._lifecycle = lifecycle

    def serve(self) -> None:
        import threading

        # Signals are handled only when serving from the main thread, as Python
        # does. A server on a background thread — the test client's — leaves
        # the process's signal handling as it found it.
        main_thread = threading.current_thread() is threading.main_thread()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._lifecycle.start_process())
            try:
                self._core.serve(main_thread)
            finally:
                loop.run_until_complete(self._lifecycle.stop_process())
        finally:
            loop.close()

    def shutdown(self) -> None:
        self._core.shutdown()
