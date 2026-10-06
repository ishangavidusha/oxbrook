#!/usr/bin/env python3
"""Liveness and readiness probes, and the drain delay.

Each property is asserted against the failure it exists for, made to happen:
a loop blocked by a handler for liveness, a check failing on one loop of
several for readiness, a check that hangs for the timeout, and a stop with
requests still arriving for the drain delay. A probe that answered 200 in
every one of those states would pass a suite that only asked it when nothing
was wrong.

Also held to account: a busy server is not a stalled one (a liveness probe
that fails under load gets healthy instances restarted), the probes are
public in an app that requires credentials, they stay out of the OpenAPI
document and the agent tools, and a failing check's exception text stays in
the log.
"""
import asyncio
import json
import sys
import threading
import time
from contextlib import asynccontextmanager

import httpx
from oxbrook import App, Health, Request
from oxbrook.auth import APIKey, Principal
from oxbrook.testing import TestClient

failures: list[str] = []
SECRET = "postgres://admin:hunter2@db.internal/prod"


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


# ---- configuration ----------------------------------------------------------------


def configuration_is_checked() -> None:
    for label, kwargs, kind in [
        ("a path without a slash", {"live": "livez"}, ValueError),
        ("the same path twice", {"live": "/health", "ready": "/health"}, ValueError),
        ("a zero stall_after", {"stall_after": 0}, ValueError),
        ("a negative timeout", {"timeout": -1}, ValueError),
        ("a negative drain_delay", {"drain_delay": -1}, ValueError),
        ("a boolean timeout", {"timeout": True}, ValueError),
    ]:
        try:
            Health(**kwargs)
            failures.append(f"Health accepted {label}")
        except kind:
            pass
    health = Health()
    try:
        @health.check
        def not_async(state):
            return True
        failures.append("Health accepted a plain def as a check")
    except TypeError:
        pass

    @health.check
    async def database(state):
        return True

    try:
        health.check(database)
        failures.append("Health accepted two checks with one name")
    except ValueError:
        pass
    try:
        App(health=True)
        failures.append("App accepted health=True instead of Health()")
    except TypeError:
        pass


# ---- the probes ---------------------------------------------------------------------


def the_probes_answer() -> None:
    key = "sk-test-" + "k" * 32
    def lookup(digest: str):
        return Principal(subject="ops") if digest == APIKey.digest(key) else None

    # With a check, so readiness is the Python route as well as the server's
    # own answers: both must be public, and neither in the document.
    health = Health()

    @health.check
    async def always(state):
        return True

    app = App(health=health, auth=APIKey(header="x-api-key", verify=lookup))

    @app.get("/items", tool=True)
    async def items(_: Request):
        return []

    with TestClient(app, workers=2) as client:
        for path, body in (("/livez", {"status": "ok"}),
                           ("/readyz", {"status": "ready", "checks": {"always": "ok"}})):
            r = client.get(path)
            check(r.status_code == 200 and r.json() == body,
                  f"{path} in an app that requires a key: {r.status_code} {r.text}")
            check(r.headers.get("cache-control") == "no-store",
                  f"{path} may be cached: {r.headers.get('cache-control')}")
            check(r.headers.get("content-type") == "application/json",
                  f"{path} content type {r.headers.get('content-type')}")
            head = client.head(path)
            check(head.status_code == 200 and head.content == b"",
                  f"HEAD {path}: {head.status_code} {head.content!r}")
        check(client.get("/items").status_code == 401, "the app's own routes became public")
        doc = client.get("/openapi.json", headers={"x-api-key": key}).json()
        check(not {"/livez", "/readyz"} & set(doc["paths"]),
              f"probes are in the OpenAPI document: {sorted(doc['paths'])}")
        tools = client.mcp("tools/list", headers={"x-api-key": key})
        names = [t["name"] for t in tools["tools"]]
        check(names == ["items"], f"an agent was offered {names}")

    plain = App()
    with TestClient(plain, workers=1) as client:
        check(client.get("/livez").status_code == 404, "an app without Health has /livez")

    custom = App(health=Health(live="/healthz", ready=None))
    with TestClient(custom, workers=1) as client:
        check(client.get("/healthz").status_code == 200, "a custom liveness path did not answer")
        check(client.get("/readyz").status_code == 404, "ready=None still served /readyz")


# ---- readiness checks -----------------------------------------------------------------


def checks_run_on_every_loop() -> None:
    health = Health(timeout=0.5)
    seen: list[int] = []
    broken: set[int] = set()
    slow: set[int] = set()
    counter = iter(range(100))
    lock = threading.Lock()

    @asynccontextmanager
    async def per_loop(_app):
        with lock:
            number = next(counter)
        yield {"loop_number": number}

    @health.check
    async def database(state):
        seen.append(state.loop_number)
        if state.loop_number in broken:
            raise RuntimeError(f"connection failed for {SECRET}")
        return True

    @health.check(name="cache")
    async def cache_ok(state):
        if state.loop_number in slow:
            await asyncio.sleep(5)
        return "fine"  # anything but False passes

    app = App(health=health, worker_lifespan=per_loop)
    with TestClient(app, workers=3) as client:
        r = client.get("/readyz")
        check(r.status_code == 200 and r.json() == {
            "status": "ready", "checks": {"database": "ok", "cache": "ok"}},
            f"all checks passing gave {r.status_code} {r.text}")
        check(sorted(seen) == [0, 1, 2], f"one probe ran the check on loops {sorted(seen)}")

        # One loop of three is broken: a probe that looked at only its own
        # loop would pass two times in three.
        broken.add(1)
        for attempt in range(6):
            r = client.get("/readyz")
            check(r.status_code == 503 and r.json()["checks"]["database"] == "failed",
                  f"attempt {attempt} with loop 1 broken: {r.status_code} {r.text}")
        check(SECRET not in r.text and "hunter2" not in r.text,
              "a failing check's exception reached the probe's body")
        broken.clear()

        slow.add(2)
        started = time.monotonic()
        r = client.get("/readyz")
        took = time.monotonic() - started
        check(r.status_code == 503 and r.json()["checks"] == {"database": "ok",
                                                               "cache": "timeout"},
              f"a check that hangs gave {r.status_code} {r.text}")
        check(took < 1.5, f"a hung check held the probe for {took:.2f}s against a 0.5s timeout")
        slow.clear()

        check(client.get("/readyz").status_code == 200, "readiness did not recover")
        check(client.get("/livez").status_code == 200,
              "a failing readiness check failed liveness, which would restart the instance")

    falsy = Health()

    @falsy.check
    async def answers_false(state):
        return False

    with TestClient(App(health=falsy), workers=1) as client:
        r = client.get("/readyz")
        check(r.status_code == 503 and r.json()["checks"]["answers_false"] == "failed",
              f"a check returning False gave {r.status_code} {r.text}")


# ---- liveness: stalled, not busy ----------------------------------------------------------


def a_stalled_loop_fails() -> None:
    app = App(health=Health(stall_after=0.3))
    release = threading.Event()

    @app.get("/block")
    async def block(_: Request):
        release.wait(5)  # blocks the loop: the defect liveness exists to catch
        return {}

    @app.get("/wait")
    async def wait(_: Request):
        await asyncio.sleep(1.0)
        return {}

    with TestClient(app, workers=1, max_concurrency=2048) as client:
        # Busy, not stuck: hundreds of requests awaiting I/O.
        busy = [threading.Thread(target=lambda: httpx.get(f"{client.base_url}/wait", timeout=10))
                for _ in range(200)]
        for t in busy:
            t.start()
        time.sleep(0.6)
        r = client.get("/livez")
        check(r.status_code == 200, f"a busy server failed liveness: {r.status_code} {r.text}")
        for t in busy:
            t.join()

        blocker = threading.Thread(target=lambda: client.get("/block"))
        blocker.start()
        time.sleep(0.1)
        # A request queued behind the blocked loop: the wake it sends goes
        # unanswered, which is what is measured.
        # Steady traffic behind it, as a real service has: each new request
        # must not restart the clock on the wake that is already waiting.
        stop_traffic = threading.Event()

        def traffic():
            while not stop_traffic.is_set():
                threading.Thread(
                    target=lambda: httpx.get(f"{client.base_url}/wait", timeout=10)).start()
                time.sleep(0.05)

        queued = threading.Thread(target=traffic)
        queued.start()
        time.sleep(0.6)
        live, ready = client.get("/livez"), client.get("/readyz")
        check(live.status_code == 503 and live.json() == {"status": "stalled", "loops": [0]},
              f"a blocked loop gave liveness {live.status_code} {live.text}")
        check(ready.status_code == 503 and ready.json()["status"] == "stalled",
              f"a blocked loop gave readiness {ready.status_code} {ready.text}")
        stop_traffic.set()
        release.set()
        blocker.join()
        queued.join()
        time.sleep(1.5)
        check(client.get("/livez").status_code == 200, "liveness did not recover")


# ---- draining -----------------------------------------------------------------------------


def draining_keeps_serving() -> None:
    app = App(health=Health(drain_delay=1.5))

    @app.get("/items")
    async def items(_: Request):
        return [1]

    client = TestClient(app, workers=1)
    client.start()
    base = client.base_url
    try:
        server = client._server
        started = time.monotonic()
        server.shutdown()
        time.sleep(0.2)
        # A fresh connection each time: one the load balancer opens after the
        # stop began must still be accepted and answered.
        with httpx.Client(base_url=base, timeout=5) as fresh:
            ready = fresh.get("/readyz")
            check(ready.status_code == 503 and json.loads(ready.text) == {"status": "draining"},
                  f"readiness while draining: {ready.status_code} {ready.text}")
            check(fresh.get("/livez").status_code == 200, "liveness failed while draining")
        with httpx.Client(base_url=base, timeout=5) as fresh:
            check(fresh.get("/items").status_code == 200,
                  "a request during the drain delay was not served")
        client._thread.join(timeout=10)
        took = time.monotonic() - started
        check(took >= 1.4, f"the server stopped {took:.2f}s after shutdown, inside its delay")
        try:
            httpx.get(f"{base}/items", timeout=1)
            failures.append("the server still accepted after the drain delay")
        except httpx.ConnectError:
            pass
    finally:
        client._server = None
        client.stop()

    # A second request to stop ends the delay.
    app = App(health=Health(drain_delay=30))
    client = TestClient(app, workers=1)
    client.start()
    try:
        started = time.monotonic()
        client._server.shutdown()
        time.sleep(0.2)
        client._server.shutdown()
        client._thread.join(timeout=10)
        took = time.monotonic() - started
        check(took < 5, f"a second stop did not cut a 30s drain delay short: {took:.1f}s")
    finally:
        client._server = None
        client.stop()

    # Without a delay the stop is immediate, as before.
    client = TestClient(App(health=Health()), workers=1)
    client.start()
    started = time.monotonic()
    client.stop()
    check(time.monotonic() - started < 2, "drain_delay=0 delayed the stop")


def main() -> None:
    for step in (configuration_is_checked, the_probes_answer, checks_run_on_every_loop,
                 a_stalled_loop_fails, draining_keeps_serving):
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
