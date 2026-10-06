"""Liveness and readiness probes.

    health = Health(drain_delay=5)

    @health.check
    async def database(state):
        async with state.pool.acquire() as connection:
            await connection.execute("select 1")

    app = App(health=health)

`/livez` and the draining and stalled answers of `/readyz` come from the
server itself, without a worker loop, so they answer when the loops cannot.
With checks registered, `/readyz` otherwise runs every check on every worker
loop, each against that loop's own state: a pool belongs to the loop that
opened it, and a probe that looked at one loop would vouch for the others
without having seen them.
"""

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ._response import Response

log = logging.getLogger("oxbrook.health")

Check = Callable[[Any], Awaitable[Any]]


def _path(value: str | None, name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value.startswith("/")):
        raise ValueError(f"{name} must be a path starting with '/', or None, not {value!r}")
    return value


class Health:
    """Probes for a load balancer or an orchestrator such as Kubernetes.

    `live` is the liveness path. It fails only when a worker loop has had
    requests waiting for longer than `stall_after` seconds without getting
    back to them — a handler blocking the loop, or a deadlock — which is the
    state a restart fixes. It does not fail because the server is busy: a
    restart there moves the load onto the other instances.

    `ready` is the readiness path. It fails while the server is draining, when
    a loop has stalled, or when a check fails. A check is an `async` function
    taking the loop's state (what `lifespan` and `worker_lifespan` yielded);
    raising or returning `False` fails it. Every check runs on every loop at
    once, and all of them together must finish within `timeout` seconds.

    `drain_delay` is how long the server keeps accepting after it is asked to
    stop, failing readiness meanwhile, before it stops accepting and finishes
    what is in flight. A load balancer takes a few seconds to notice that a
    backend is leaving; without the delay, the requests it sends in those
    seconds are refused. A second Ctrl-C or SIGTERM ends the delay early.

    Set either path to `None` to leave it out. Both are public even in an app
    declared `App(auth=...)`, are not in the OpenAPI document, and are never
    agent tools.
    """

    def __init__(
        self,
        *,
        live: str | None = "/livez",
        ready: str | None = "/readyz",
        stall_after: float = 10.0,
        timeout: float = 1.0,
        drain_delay: float = 0.0,
    ) -> None:
        self.live = _path(live, "live")
        self.ready = _path(ready, "ready")
        if live is not None and live == ready:
            raise ValueError("live and ready must be different paths")
        for name, value in (("stall_after", stall_after), ("timeout", timeout)):
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                raise ValueError(f"{name} must be a positive number of seconds")
        if isinstance(drain_delay, bool) or not isinstance(drain_delay, int | float) \
                or drain_delay < 0:
            raise ValueError("drain_delay must be zero or a positive number of seconds")
        self.stall_after = float(stall_after)
        self.timeout = float(timeout)
        self.drain_delay = float(drain_delay)
        self.checks: dict[str, Check] = {}

    def check(self, fn: Check | None = None, *, name: str | None = None) -> Any:
        """Register a readiness check, as `@health.check` or `@health.check(name=...)`."""

        def register(fn: Check) -> Check:
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(f"a health check must be `async def`, got {fn!r}")
            label = name or getattr(fn, "__name__", "check")
            if label in self.checks:
                raise ValueError(f"a health check named {label!r} is already registered")
            self.checks[label] = fn
            return fn

        return register(fn) if fn is not None else register

    def as_spec(self) -> tuple:
        """The tuple the Rust server takes."""
        return (self.live, self.ready, bool(self.checks), self.stall_after, self.drain_delay)

    async def evaluate(self, request: Any) -> Response:
        """Run every check on every loop of the server this request reached."""
        context = request._context
        loops = list(context.lifecycle.loops) if context is not None else []
        here = asyncio.get_running_loop()

        async def one(loop: Any, state: Any, label: str, fn: Check) -> bool:
            async def run() -> bool:
                return (await fn(state)) is not False

            try:
                if loop is here:
                    return await run()
                # On the loop that owns the state: nothing loop-bound crosses
                # loops, and a stuck loop answers by timing out.
                return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(run(), loop))
            except Exception:
                log.exception("health check %r failed", label)
                return False

        jobs = [
            (label, one(loop, ctx.state, label, fn))
            for loop, ctx in loops
            for label, fn in self.checks.items()
        ]
        tasks = [asyncio.ensure_future(job) for _, job in jobs]
        _, pending = await asyncio.wait(tasks, timeout=self.timeout) if tasks else (set(), set())
        for task in pending:
            task.cancel()
        results: dict[str, str] = dict.fromkeys(self.checks, "ok")
        for (label, _), task in zip(jobs, tasks, strict=True):
            if task in pending:
                log.warning("health check %r did not finish within %ss", label, self.timeout)
                results[label] = "timeout"
            elif not task.result() and results[label] == "ok":
                results[label] = "failed"
        ready = bool(loops) and all(v == "ok" for v in results.values())
        body = {"status": "ready" if ready else "unavailable", "checks": results}
        return Response(
            _encode(body),
            status=200 if ready else 503,
            headers={"cache-control": "no-store"},
        )


def _encode(body: dict) -> bytes:
    from ._schema import encode

    return encode(body)
