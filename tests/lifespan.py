#!/usr/bin/env python3
"""Startup and shutdown around a server's life.

Oxbrook runs one asyncio loop per worker thread. Something bound to a loop — a
connection pool, an async HTTP client — cannot be shared between them, so there
are two hooks: `lifespan` once for the process, and `worker_lifespan` on every
loop. These cases hold each part of that to account:

* every request sees the resource built on its *own* loop, never another's;
* the order is process up, workers up, workers down, process down;
* a worker's teardown — including one that awaits — has finished by the time the
  server has stopped. Worker threads were never joined before this, so a
  teardown would have been cut off when the process exited;
* a teardown that hangs cannot hang shutdown past the grace period;
* a startup failure surfaces as the exception the lifespan raised, and the
  workers that had already started release what they built first;
* state is read-only, and a key yielded by both hooks is refused.
"""
import asyncio
import sys
import threading
import time
from contextlib import asynccontextmanager

from oxbrook import App, Request
from oxbrook.testing import TestClient

WORKERS = 3
failures: list[str] = []
events: list[str] = []
lock = threading.Lock()


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def note(event: str) -> None:
    with lock:
        events.append(event)


def make_app(lifespan=None, worker_lifespan=None) -> App:
    app = App(openapi_url=None, docs_url=None, mcp_url="/mcp",
              lifespan=lifespan, worker_lifespan=worker_lifespan)

    @app.get("/probe", tool=True)
    async def probe(request: Request):
        """Report what this request can see."""
        state = request.state
        return {
            "own_loop": state.get("loop") is asyncio.get_running_loop(),
            "config": state.get("config"),
            "is_app": request.app is app,
        }

    @app.get("/write")
    async def write(request: Request):
        try:
            request.state.anything = 1
        except AttributeError:
            return {"refused": True}
        return {"refused": False}

    return app


async def process_lifespan(app):
    note("process up")
    yield {"config": "prod"}
    note("process down")


@asynccontextmanager
async def loop_lifespan(app):
    note("worker up")
    try:
        yield {"loop": asyncio.get_running_loop()}
    finally:
        # Awaits, so a teardown that is cut off shows up as a missing event.
        await asyncio.sleep(0.2)
        note("worker down")


def resources_stay_on_their_loop() -> None:
    events.clear()
    app = make_app(process_lifespan, loop_lifespan)
    with TestClient(app, workers=WORKERS) as client:
        results = [client.get("/probe").json() for _ in range(120)]
        check(all(r["own_loop"] for r in results),
              f"{sum(not r['own_loop'] for r in results)} of 120 requests saw another "
              f"loop's resource")
        check(all(r["config"] == "prod" for r in results),
              "process lifespan state did not reach requests")
        check(all(r["is_app"] for r in results), "request.app was not the app")
        check(client.get("/write").json() == {"refused": True}, "request.state accepted a write")
        check(dict(app.state) == {"config": "prod"}, f"app.state while serving: {app.state!r}")
        tool = client.call_tool("probe")
        check(tool.get("config") == "prod" and tool.get("own_loop"),
              f"an MCP tool call did not see the request's state: {tool}")
    check(len(app.state) == 0, f"app.state survived shutdown: {app.state!r}")

    want = ["process up", *["worker up"] * WORKERS, *["worker down"] * WORKERS, "process down"]
    check(events == want, f"lifecycle order was {events}, expected {want}")


def teardown_hang_is_bounded() -> None:
    hold = 3.0

    @asynccontextmanager
    async def stuck(app):
        yield {}
        await asyncio.sleep(hold)

    app = make_app(worker_lifespan=stuck)
    client = TestClient(app, workers=1, shutdown_grace=0.3).start()
    started = time.perf_counter()
    client.stop()
    took = time.perf_counter() - started
    check(took < hold - 1.0,
          f"a teardown that hangs for {hold}s held shutdown for {took:.1f}s past a 0.3s grace")
    # Let the abandoned teardown finish, so no thread is still attached when
    # the interpreter exits.
    time.sleep(hold + 0.5)


def startup_failure_cleans_up() -> None:
    events.clear()
    started = []

    @asynccontextmanager
    async def flaky(app):
        with lock:
            index = len(started)
            started.append(index)
        if index == 1:
            raise ConnectionRefusedError("database is down")
        try:
            yield {}
        finally:
            note(f"released {index}")

    app = make_app(process_lifespan, flaky)
    try:
        with TestClient(app, workers=WORKERS):
            failures.append("a server whose worker lifespan raised started anyway")
    except ConnectionRefusedError:
        pass
    except Exception as exc:
        failures.append(f"worker startup failure surfaced as {type(exc).__name__}: {exc}")
    check("released 0" in events, f"the worker that started was not torn down: {events}")
    check(events[-1:] == ["process down"],
          f"the process lifespan was not torn down after a worker failed: {events}")
    check(started == [0, 1], f"workers kept starting after one failed: {started}")


def process_startup_failure_starts_no_workers() -> None:
    events.clear()

    async def broken(app):
        raise RuntimeError("config missing")
        yield  # pragma: no cover

    app = make_app(broken, loop_lifespan)
    try:
        with TestClient(app, workers=WORKERS):
            failures.append("a server whose lifespan raised started anyway")
    except RuntimeError as exc:
        check("config missing" in str(exc), f"lifespan failure surfaced as {exc!r}")
    check("worker up" not in events, f"workers started after the lifespan failed: {events}")


def bad_hooks_are_refused() -> None:
    async def not_a_mapping(app):
        yield ["db"]

    async def config(app):
        yield {"db": 1}

    async def same_key(app):
        yield {"db": 2}

    def not_a_manager(app):
        return 42

    for label, kwargs, kind in [
        ("a lifespan yielding a list", {"lifespan": not_a_mapping}, TypeError),
        ("a hook that is not a context manager", {"lifespan": not_a_manager}, TypeError),
        ("a key yielded by both hooks", {"lifespan": config, "worker_lifespan": same_key},
         ValueError),
    ]:
        try:
            with TestClient(make_app(**kwargs), workers=1):
                failures.append(f"{label} was accepted")
        except kind:
            pass
        except Exception as exc:
            failures.append(f"{label} raised {type(exc).__name__}: {exc}")

    try:
        App(lifespan=42)
        failures.append("a non-callable lifespan was accepted")
    except TypeError:
        pass


def a_per_loop_budget_is_divided_not_multiplied() -> None:
    """Anything `worker_lifespan` opens is opened once per loop, so a number
    written there is multiplied by a count the code never chose.

    asyncpg's own default is `min_size=max_size=10`, and `min_size` is eager,
    so the obvious pool opens ten connections per loop the moment the server
    starts — eighty on an eight-loop host, against a PostgreSQL that allows a
    hundred. This asserts the arithmetic that stops that.
    """
    opened: list[int] = []

    async def pools(app):
        # What a real worker_lifespan would pass to create_pool.
        opened.append(app.per_worker(40))
        yield {}

    app = make_app(None, pools)

    # Before a server exists the count is unknown, and guessing it would be
    # the same bug one layer down.
    try:
        app.per_worker(40)
    except RuntimeError as exc:
        check("worker_lifespan" in str(exc),
              f"per_worker() before serving did not say where to call it: {exc}")
    else:
        failures.append("per_worker() answered before the worker count was known")

    with TestClient(app, workers=WORKERS) as client:
        check(client.get("/probe").status_code == 200, "setup: the app did not serve")
        check(app.workers == WORKERS, f"app.workers was {app.workers}, expected {WORKERS}")

    check(len(opened) == WORKERS,
          f"worker_lifespan ran {len(opened)} times for {WORKERS} loops")
    check(all(share == 40 // WORKERS for share in opened),
          f"each loop was given {opened}, expected {40 // WORKERS} each")
    # The whole point: the process total stays inside the budget.
    check(sum(opened) <= 40,
          f"{WORKERS} loops of {opened[0]} is {sum(opened)}, over a budget of 40")

    app.workers = 8
    check(app.per_worker(40) == 5, f"40 across 8 loops gave {app.per_worker(40)}")
    # Rounds down: over a connection budget is the dangerous direction.
    check(app.per_worker(41) == 5, f"41 across 8 loops gave {app.per_worker(41)}")
    for bad in (0, -1, 3.5):
        try:
            app.per_worker(bad)
        except (ValueError, TypeError):
            pass
        else:
            failures.append(f"per_worker({bad!r}) was accepted")
    try:
        # Fewer connections than loops cannot be honoured; saying so beats
        # returning zero, which would be a pool that cannot open.
        app.per_worker(3)
    except ValueError as exc:
        check("at least one" in str(exc),
              f"a budget below the loop count was refused with: {exc}")
    else:
        failures.append("a budget smaller than the worker count was accepted")


def main() -> None:
    for step in (
        resources_stay_on_their_loop,
        a_per_loop_budget_is_divided_not_multiplied,
        startup_failure_cleans_up,
        process_startup_failure_starts_no_workers,
        bad_hooks_are_refused,
        teardown_hang_is_bounded,
    ):
        try:
            step()
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR")

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
