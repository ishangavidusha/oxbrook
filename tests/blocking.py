#!/usr/bin/env python3
"""Blocking handlers: off the worker loop, on a bounded shared pool.

A worker loop serves many requests at once by interleaving them at every
`await`. A handler that takes time without awaiting freezes that loop and every
request on it. Before `blocking=True` existed, eight such handlers on four
loops made an unrelated `GET` wait 1829 ms; the first case here is that
measurement turned into an assertion.

The rest are about not being able to get it wrong: a `def` handler is still
refused unless the route says so, an `async def` marked blocking is refused
because it would be a lie, and the pool is one per process and bounded so that
blocking work queues instead of multiplying threads.
"""
import asyncio
import sys
import threading
import time

import httpx
from oxbrook import App, Request, Router
from oxbrook._blocking import Pool, default_threads
from pydantic import BaseModel

PORT = 8815
BASE = f"http://127.0.0.1:{PORT}"

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def refused(build, expecting: str, case: str) -> None:
    """A registration that must fail, with a message that names the fix."""
    try:
        build()
    except TypeError as exc:
        check(
            expecting in str(exc),
            f"{case}: refused, but the message never mentions {expecting!r}: {exc}",
        )
    else:
        failures.append(f"{case}: was accepted and should not have been")


class Item(BaseModel):
    name: str


app = App(title="blocking", version="1")
router = Router(prefix="/api")

#: How many blocking handlers are inside the pool at this instant, and the most
#: there have ever been at once. The pool's bound is the claim being tested.
live = 0
peak = 0
counter = threading.Lock()


@app.get("/fast")
async def fast(request: Request):
    """Answers immediately, and must keep doing so under load."""
    return {"ok": True}


@app.get("/sleep/{seconds}", blocking=True)
def sleep_route(request: Request, seconds: float):
    """Blocks its thread, not its loop. Path parameters still arrive."""
    global live, peak
    with counter:
        live += 1
        peak = max(peak, live)
    try:
        time.sleep(seconds)
        return {"slept": seconds, "thread": threading.current_thread().name}
    finally:
        with counter:
            live -= 1


@app.post("/echo", blocking=True)
def echo(request: Request, item: Item):
    """A validated body reaches a blocking handler like any other."""
    return {"name": item.name}


@router.get("/through-a-router", blocking=True)
def through_a_router(request: Request):
    """Routers build routes before they know an app, so this one's pool is
    wired at include time or not at all."""
    return {"ok": True}


app.include(router)


# ---- registration ----------------------------------------------------------


def a_plain_def_is_still_refused() -> None:
    """And the message has to name `blocking=True`.

    "handlers must be `async def`" on its own sends someone to rewrite a
    blocking handler as `async def`, which runs it on the loop and makes the
    problem worse. That was half of why this gap lasted.
    """
    other = App()

    def build():
        @other.get("/sync")
        def sync_handler(request: Request):
            return {}

    refused(build, "blocking=True", "a def handler with no blocking=True")


def an_async_handler_cannot_be_blocking() -> None:
    other = App()

    def build():
        @other.get("/already-async", blocking=True)
        async def already_async(request: Request):
            return {}

    refused(build, "already runs on the worker loop", "an async def marked blocking")


def a_websocket_cannot_be_blocking() -> None:
    other = App()

    def build():
        @other.websocket("/ws", blocking=True)
        def socket(request: Request, ws):
            return None

    # The message has to be the explanatory one, not Python's "unexpected
    # keyword argument": the decorator takes `blocking` precisely so that
    # someone who tries this is told why a socket cannot hold a thread.
    refused(build, "holds an open socket", "a blocking websocket")


def the_route_says_so() -> None:
    """`oxb routes` reads this, which is how blocking work stays visible."""
    blocking = {r.path for r in app.routes if r.blocking}
    check(
        blocking == {"/sleep/{seconds}", "/echo", "/api/through-a-router"},
        f"the blocking routes are {sorted(blocking)}",
    )
    for route in app.routes:
        if route.blocking:
            check(
                route.pool is not None and route.pool.pool is not None,
                f"{route.path} is blocking with no pool wired",
            )


# ---- the pool --------------------------------------------------------------


def the_pool_default_is_one_loops_worth() -> None:
    """Not one per loop, which is what `asyncio.to_thread` would have given."""
    check(
        Pool().threads == default_threads(),
        f"the default pool is {Pool().threads} threads, expected {default_threads()}",
    )
    check(default_threads() <= 32, "the default pool is unbounded")
    for bad in (0, -1):
        try:
            Pool(bad)
        except ValueError:
            pass
        else:
            failures.append(f"a pool of {bad} threads was accepted")


def the_pool_starts_no_thread_until_it_is_used() -> None:
    pool = Pool(2)
    check(not pool.started, "a fresh pool had already started threads")


def closing_is_explicit() -> None:
    """Invariant 7: released because something released it."""
    pool = Pool(2)

    async def touch():
        return await pool.run(lambda: 1)

    asyncio.run(touch())
    check(pool.started, "setup: the pool should have started")
    pool.close()
    check(not pool.started, "close() left the pool started")


# ---- under load ------------------------------------------------------------


def the_loop_keeps_serving(client) -> None:
    """The measurement that named this gap, as an assertion.

    Eight concurrent one-second blocking handlers against four worker loops.
    Before the fix an unrelated `GET` waited 1829 ms behind them.
    """

    async def load():
        async with httpx.AsyncClient(timeout=30) as c:
            slow = [
                asyncio.create_task(c.get(f"{BASE}/sleep/1.0"))
                for _ in range(8)
            ]
            await asyncio.sleep(0.3)
            started = time.monotonic()
            quick = await c.get(f"{BASE}/fast")
            waited = time.monotonic() - started
            answered = await asyncio.gather(*slow)
            return waited, quick, answered

    waited, quick, answered = asyncio.run(load())
    check(quick.status_code == 200, f"the trivial GET returned {quick.status_code}")
    check(
        waited < 0.3,
        f"a trivial GET waited {waited * 1000:.0f} ms behind blocking handlers, "
        f"which means they are running on the worker loops",
    )
    check(
        all(r.status_code == 200 for r in answered),
        "a blocking handler did not answer",
    )
    threads = {r.json()["thread"] for r in answered}
    check(
        all(name.startswith("oxbrook-blocking") for name in threads),
        f"blocking handlers ran on {threads}",
    )


def the_pool_bounds_what_runs_at_once(client) -> None:
    """Queueing is the trade: the loops stay free, the pool has a ceiling."""
    global peak
    peak = 0

    async def load():
        async with httpx.AsyncClient(timeout=30) as c:
            await asyncio.gather(
                *[c.get(f"{BASE}/sleep/0.3") for _ in range(12)]
            )

    asyncio.run(load())
    check(
        0 < peak <= THREADS,
        f"{peak} blocking handlers ran at once against a pool of {THREADS}",
    )


def blocking_handlers_are_ordinary_handlers(client) -> None:
    """The shim goes on innermost, so everything above it is unchanged."""
    slept = client.get(f"{BASE}/sleep/0.01")
    check(slept.json()["slept"] == 0.01, f"a path parameter arrived as {slept.json()}")

    echoed = client.post(f"{BASE}/echo", json={"name": "ada"})
    check(echoed.json() == {"name": "ada"}, f"a validated body gave {echoed.json()}")

    invalid = client.post(f"{BASE}/echo", json={"name": 1234, "nope": True})
    check(invalid.status_code in (200, 422), f"a bad body gave {invalid.status_code}")

    routed = client.get(f"{BASE}/api/through-a-router")
    check(routed.status_code == 200, f"a router's blocking route gave {routed.status_code}")


THREADS = 4


def main() -> None:
    for step in (
        a_plain_def_is_still_refused,
        an_async_handler_cannot_be_blocking,
        a_websocket_cannot_be_blocking,
        the_route_says_so,
        the_pool_default_is_one_loops_worth,
        the_pool_starts_no_thread_until_it_is_used,
        closing_is_explicit,
    ):
        step()
        print(f"  {step.__name__}: ok")

    threading.Thread(
        target=lambda: app.run(port=PORT, workers=4, blocking_threads=THREADS),
        daemon=True,
    ).start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/fast", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        print("server never came up")
        sys.exit(1)

    with httpx.Client(timeout=30) as client:
        for step in (
            the_loop_keeps_serving,
            the_pool_bounds_what_runs_at_once,
            blocking_handlers_are_ordinary_handlers,
        ):
            step(client)
            print(f"  {step.__name__}: ok")

    if failures:
        print("\nfailures:")
        for failure in failures:
            print(f"  - {failure}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
