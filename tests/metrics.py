#!/usr/bin/env python3
"""Server metrics in the Prometheus text format, collected in Rust.

The exposition is parsed by `prometheus_client`'s own parser, not by a regex
written here: a format that only this suite could read is not one Prometheus
can scrape.

Each number is checked against an event made to happen, not merely for being
present: requests waiting behind a loop that is blocked, a burst shed at
`max_concurrency`, a handler running past `request_timeout`, a handler
sleeping into a known duration bucket. Also held to account: route templates
rather than raw paths (an id in a label is a new time series per id, and a
scraper that falls over), a handler's own `503` not counted as shedding, and
probes and scrapes left out of the request counts.
"""
import asyncio
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
from oxbrook import CORS, App, Health, Metrics, Request, Response
from oxbrook.testing import TestClient
from prometheus_client.parser import text_string_to_metric_families

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def scrape(client: TestClient) -> dict[str, list]:
    r = client.get("/metrics")
    check(r.status_code == 200, f"/metrics answered {r.status_code}")
    families = {}
    for family in text_string_to_metric_families(r.text):
        families[family.name] = family.samples
    return families


def value(families, name: str, **labels) -> float:
    family = name
    for suffix in ("_total", "_bucket", "_count", "_sum"):
        family = family.removesuffix(suffix)
    for base in (family, name):
        for sample in families.get(base, []):
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0.0


STATIC = Path(tempfile.mkdtemp(prefix="oxbrook-metrics-"))
(STATIC / "a.txt").write_text("hello")


def make(**options) -> App:
    app = App(metrics=Metrics(), **options)

    @app.get("/users/{user_id}")
    async def user(_: Request, user_id: int):
        return {"id": user_id}

    @app.get("/sleep")
    async def sleep(_: Request, seconds: float = 0.0):
        await asyncio.sleep(seconds)
        return {}

    @app.get("/block")
    async def block(_: Request, seconds: float = 0.3):
        time.sleep(seconds)  # holds the loop, so queued requests wait
        return {}

    @app.get("/unavailable")
    async def unavailable(_: Request):
        return Response('{"busy":true}', status=503)

    app.static("/files", STATIC)
    return app


# ---- cases --------------------------------------------------------------------------


def configuration_is_checked() -> None:
    for label, make_it, kind in [
        ("a path without a slash", lambda: Metrics(path="metrics"), ValueError),
        ("a dict", lambda: App(metrics={"path": "/metrics"}), TypeError),
        ("the health path", lambda: App(metrics=Metrics(path="/livez"), health=Health()),
         ValueError),
    ]:
        try:
            make_it()
            failures.append(f"accepted {label}")
        except kind:
            pass


def the_exposition_is_valid() -> None:
    with TestClient(make(), workers=2) as client:
        client.get("/users/1")
        r = client.get("/metrics")
        check(r.headers["content-type"].startswith("text/plain; version=0.0.4"),
              f"content type {r.headers['content-type']}")
        check(r.headers.get("cache-control") == "no-store", "a scrape may be cached")
        families = {f.name: f.type for f in text_string_to_metric_families(r.text)}
        for name, kind in [
            ("oxbrook_requests", "counter"),
            ("oxbrook_request_duration_seconds", "histogram"),
            ("oxbrook_queue_wait_seconds", "histogram"),
            ("oxbrook_loop_requests", "gauge"),
            ("oxbrook_loop_queued", "gauge"),
            ("oxbrook_loop_wake_pending_seconds", "gauge"),
            ("oxbrook_connections", "gauge"),
            ("oxbrook_connections_limit", "gauge"),
            ("oxbrook_requests_shed", "counter"),
            ("oxbrook_request_timeouts", "counter"),
        ]:
            check(families.get(name) == kind, f"{name} is {families.get(name)!r}, not {kind}")
        head = client.head("/metrics")
        check(head.status_code == 200 and head.content == b"", "HEAD /metrics")
        doc = client.get("/openapi.json").json()
        check("/metrics" not in doc["paths"], "/metrics is in the OpenAPI document")

    with TestClient(App(), workers=1) as client:
        check(client.get("/metrics").status_code == 404, "an app without Metrics has /metrics")


def requests_are_counted_by_template() -> None:
    with TestClient(make(health=Health(), cors=CORS(allow_origins=["https://a.example"])),
                    workers=2) as client:
        for i in range(7):
            client.get(f"/users/{i}")
        client.get("/users/abc")      # 422, before any worker
        client.get("/nowhere")        # 404
        client.post("/users/1")       # 405
        client.get("/unavailable")    # a handler's own 503
        client.get("/files/a.txt")    # a static mount
        for _ in range(3):
            client.get("/livez")
            client.get("/readyz")
            client.get("/metrics")
        client.options("/users/1", headers={"origin": "https://a.example",
                                            "access-control-request-method": "GET"})
        f = scrape(client)
        route = {"method": "GET", "route": "/users/{user_id}"}
        check(value(f, "oxbrook_requests_total", status="200", **route) == 7,
              f"7 lookups counted {value(f, 'oxbrook_requests_total', status='200', **route)}")
        check(value(f, "oxbrook_requests_total", status="422", **route) == 1,
              "a bad path parameter was not counted against its route")
        unmatched = {"method": "", "route": ""}
        check(value(f, "oxbrook_requests_total", status="404", **unmatched) == 1, "404 not counted")
        check(value(f, "oxbrook_requests_total", status="405", **unmatched) == 1, "405 not counted")
        check(value(f, "oxbrook_requests_total", status="503", method="GET",
                    route="/unavailable") == 1, "a handler's 503 not counted")
        check(value(f, "oxbrook_requests_shed_total") == 0,
              "a handler's own 503 was counted as shedding")
        files = [s for s in f["oxbrook_requests"] if s.labels.get("route", "").startswith("/files")]
        check(files and files[0].value == 1, f"the static file was counted as {files}")
        routes = {s.labels.get("route") for s in f["oxbrook_requests"]}
        check(not any(r and r[-1].isdigit() for r in routes),
              f"a raw path reached a label: {sorted(routes)}")
        check(not {"/livez", "/readyz", "/metrics"} & routes,
              f"probes or scrapes were counted: {sorted(routes)}")
        total = sum(s.value for s in f["oxbrook_requests"])
        check(total == 12, f"{total} requests counted; 12 were made, besides probes, scrapes "
                           f"and a preflight")
        count = value(f, "oxbrook_request_duration_seconds_count", **route)
        check(count == 8, f"the duration histogram counted {count} of 8 requests")


def durations_land_in_their_bucket() -> None:
    with TestClient(make(), workers=1) as client:
        for _ in range(3):
            client.get("/sleep", params={"seconds": 0.3})
        f = scrape(client)
        route = {"method": "GET", "route": "/sleep"}
        below = value(f, "oxbrook_request_duration_seconds_bucket", le="0.25", **route)
        within = value(f, "oxbrook_request_duration_seconds_bucket", le="0.5", **route)
        top = value(f, "oxbrook_request_duration_seconds_bucket", le="+Inf", **route)
        check((below, within, top) == (0, 3, 3),
              f"three 0.3 s requests fell in buckets <=0.25: {below}, <=0.5: {within}, "
              f"+Inf: {top}")
        total = value(f, "oxbrook_request_duration_seconds_sum", **route)
        check(0.85 < total < 1.5, f"the sum of three 0.3 s requests is {total}")
        buckets = [s.value for s in f["oxbrook_request_duration_seconds"]
                   if s.name.endswith("_bucket") and s.labels.get("route") == "/sleep"]
        check(buckets == sorted(buckets), f"buckets are not cumulative: {buckets}")


def queue_wait_is_measured() -> None:
    with TestClient(make(), workers=1) as client:
        blocker = threading.Thread(target=lambda: client.get("/block", params={"seconds": 0.4}))
        blocker.start()
        time.sleep(0.1)
        # Queued behind a blocked loop: these wait about 0.3 s for it.
        waiting = [threading.Thread(target=lambda: httpx.get(f"{client.base_url}/sleep"))
                   for _ in range(3)]
        for t in waiting:
            t.start()
        time.sleep(0.1)
        during = scrape(client)
        queued = value(during, "oxbrook_loop_queued", loop="0")
        pending = value(during, "oxbrook_loop_wake_pending_seconds", loop="0")
        check(queued >= 1, f"requests behind a blocked loop showed as {queued} queued")
        check(pending > 0, "a blocked loop showed no pending wake")
        blocker.join()
        for t in waiting:
            t.join()
        f = scrape(client)
        slow = value(f, "oxbrook_queue_wait_seconds_count", loop="0") - value(
            f, "oxbrook_queue_wait_seconds_bucket", le="0.1", loop="0")
        check(slow >= 3, f"{slow} requests waited over 0.1 s; at least 3 queued behind the block")
        check(value(f, "oxbrook_loop_queued", loop="0") == 0, "the queue gauge did not fall back")


def shedding_and_timeouts_are_counted() -> None:
    with TestClient(make(), workers=1, max_concurrency=2, request_timeout=0.5) as client:
        results: list[int] = []

        def call():
            results.append(httpx.get(f"{client.base_url}/sleep",
                                     params={"seconds": 0.3}, timeout=5).status_code)

        burst = [threading.Thread(target=call) for _ in range(12)]
        for t in burst:
            t.start()
        for t in burst:
            t.join()
        shed = results.count(503)
        check(shed > 0, f"a burst of 12 at max_concurrency=2 shed nothing: {results}")
        r = client.get("/sleep", params={"seconds": 2})
        check(r.status_code == 504, f"a handler past request_timeout gave {r.status_code}")
        f = scrape(client)
        check(value(f, "oxbrook_requests_shed_total") == shed,
              f"{shed} requests were shed; the counter says "
              f"{value(f, 'oxbrook_requests_shed_total')}")
        check(value(f, "oxbrook_request_timeouts_total") == 1,
              f"timeouts counted {value(f, 'oxbrook_request_timeouts_total')}")


def gauges_follow_the_load() -> None:
    with TestClient(make(), workers=1, max_connections=50) as client:
        clients = [httpx.Client(base_url=client.base_url) for _ in range(5)]
        try:
            slow = [threading.Thread(target=lambda c=c: c.get("/sleep", params={"seconds": 0.6}))
                    for c in clients]
            for t in slow:
                t.start()
            time.sleep(0.25)
            f = scrape(client)
            check(value(f, "oxbrook_loop_requests", loop="0") >= 5,
                  f"5 sleeping handlers showed as {value(f, 'oxbrook_loop_requests', loop='0')}")
            check(value(f, "oxbrook_loop_queued", loop="0") == 0,
                  "handlers running on an idle loop showed as queued: in flight is not queued")
            opened = value(f, "oxbrook_connections")
            check(6 <= opened <= 8,
                  f"6 open clients (5 and the test client's own) showed {opened} connections")
            check(value(f, "oxbrook_connections_limit") == 50, "the connection limit is wrong")
            for t in slow:
                t.join()
        finally:
            for c in clients:
                c.close()
        time.sleep(0.1)
        f = scrape(client)
        check(value(f, "oxbrook_loop_requests", loop="0") == 0,
              "the in-flight gauge did not fall back to zero")
        check(value(f, "oxbrook_connections") < opened,
              f"closing 5 clients left {value(f, 'oxbrook_connections')} connections")


def the_guide_example_works() -> None:
    # www/guide/metrics.md, "Your own metrics": an app route beside /metrics.
    from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest

    registry = CollectorRegistry()
    orders = Counter("orders_placed_total", "Orders placed.", registry=registry)
    app = make()

    @app.get("/metrics/app", auth=None)
    async def app_metrics(_: Request):
        return Response(generate_latest(registry), content_type=CONTENT_TYPE_LATEST)

    orders.inc(3)
    with TestClient(app, workers=1) as client:
        own = {f.name: f for f in text_string_to_metric_families(client.get("/metrics/app").text)}
        check(own["orders_placed"].samples[0].value == 3, "the app's own counter did not read 3")
        server = scrape(client)
        check(value(server, "oxbrook_requests_total", method="GET", route="/metrics/app",
                    status="200") == 1, "the app's metrics route was not counted as a route")


def main() -> None:
    for step in (configuration_is_checked, the_exposition_is_valid,
                 requests_are_counted_by_template, durations_land_in_their_bucket,
                 queue_wait_is_measured, shedding_and_timeouts_are_counted,
                 gauges_follow_the_load, the_guide_example_works):
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
