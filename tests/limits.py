#!/usr/bin/env python3
"""Rate limits, and the client address they are keyed on.

Clients are told apart through `X-Forwarded-For` from a trusted loopback
proxy, since every request in a test comes from 127.0.0.1. That is also the
path most deployments take, so the header handling is tested by being used.

Held to account: the budget refills at its rate rather than resetting; one
client over its limit leaves another untouched; a route's limit is spent only
by that route, unless the same `RateLimit` is shared; a router's limit reaches
its routes and `rate_limit=None` exempts one; an agent calling a limited route
as a tool spends the same budget as HTTP; a forwarded address is believed only
from a trusted proxy, and only as far as the trusted entries reach; IPv6
clients count by /64; probes and scrapes are never limited; and a client
cycling through addresses does not grow the server's memory for ever.
"""
import asyncio
import sys
import time

import websockets
from oxbrook import App, Health, Metrics, RateLimit, Request, Router
from oxbrook.testing import TestClient
from prometheus_client.parser import text_string_to_metric_families

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def raises(exc: type, fn) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


def as_client(ip: str) -> dict[str, str]:
    return {"x-forwarded-for": ip}


def statuses(client: TestClient, path: str, n: int, **kwargs) -> list[int]:
    return [client.get(path, **kwargs).status_code for _ in range(n)]


def configuration_is_checked() -> None:
    for rate in ("100", "100/fortnight", "0/second", "-1/minute", "1/minute/extra", 100, None,
                 "1000001/second"):
        check(raises((ValueError, TypeError), lambda r=rate: RateLimit(r)),
              f"RateLimit({rate!r}) was accepted")
    for burst in (0, -1, True, 1.5, "5", 100_001):
        check(raises(ValueError, lambda b=burst: RateLimit("5/minute", burst=b)),
              f"burst={burst!r} was accepted")
    for key in ("ip", "header:", "header:bad header", "Header:x-api-key", None):
        check(raises(ValueError, lambda k=key: RateLimit("5/minute", key=k)),
              f"key={key!r} was accepted")
    check(RateLimit("1000000/second").burst == 100_000,
          "a default burst above the cap was not clamped to it")
    limit = RateLimit("10 / minutes", key="header:X-API-Key")
    check(limit.rate == "10/minute" and limit.burst == 10 and limit.key == "header:x-api-key",
          f"a valid RateLimit read back as {limit!r}")
    check(raises(TypeError, lambda: App(rate_limit="5/minute")),
          "App(rate_limit=<str>) was accepted")
    app = App()
    check(raises(TypeError, lambda: app.get("/x", rate_limit=5)), "rate_limit=5 was accepted")
    check(raises(TypeError, lambda: Router(rate_limit="5/minute")),
          "Router(rate_limit=<str>) was accepted")
    for proxies, exc in (("10.0.0.0/8", TypeError), (True, TypeError), (0, ValueError),
                         (["not-a-network"], ValueError), (["10.0.0.0/33"], ValueError)):
        check(raises(exc, lambda p=proxies: App(trusted_proxies=p)),
              f"trusted_proxies={proxies!r} was accepted")
    check(App(trusted_proxies=["10.1.2.3/8", "::1"]).trusted_proxies
          == (0, [("10.0.0.0", 8), ("::1", 128)]), "networks were not normalised")


def the_app_limit_refuses_and_refills() -> None:
    app = App(rate_limit=RateLimit("10/second", burst=3), trusted_proxies=["127.0.0.1"])
    calls = []

    @app.get("/hello")
    async def hello(_: Request):
        calls.append(1)
        return {"ok": True}

    with TestClient(app) as client:
        got = statuses(client, "/hello", 5, headers=as_client("192.0.2.1"))
        check(got == [200, 200, 200, 429, 429], f"burst of 3 answered {got}")
        check(len(calls) == 3, f"a refused request reached the handler ({len(calls)} calls)")
        refused = client.get("/hello", headers=as_client("192.0.2.1"))
        check(refused.headers.get("retry-after") == "1",
              f"Retry-After was {refused.headers.get('retry-after')!r}")
        check(refused.headers.get("content-type") == "application/problem+json"
              and refused.json().get("status") == 429, f"429 body was {refused.text!r}")

        other = statuses(client, "/hello", 3, headers=as_client("192.0.2.2"))
        check(other == [200, 200, 200], f"a second client was limited by the first: {other}")
        check(client.get("/missing", headers=as_client("192.0.2.1")).status_code == 429,
              "a request matching no route escaped the app's limit")

        # One request every 100 ms comes back, not the whole burst at once.
        time.sleep(0.12)
        got = statuses(client, "/hello", 2, headers=as_client("192.0.2.1"))
        check(got == [200, 429], f"after one interval the client got {got}, not one request")
        # Quiet for several intervals: the saved-up budget is `burst`, no more.
        time.sleep(0.6)
        got = statuses(client, "/hello", 4, headers=as_client("192.0.2.1"))
        check(got == [200, 200, 200, 429], f"after a quiet spell the burst was {got}")


def route_limits_are_their_own() -> None:
    shared = RateLimit("1/minute", burst=2)
    admin = Router(prefix="/admin", rate_limit=RateLimit("1/minute", burst=1))

    @admin.get("/a")
    async def admin_a(_: Request):
        return {}

    @admin.get("/b")
    async def admin_b(_: Request):
        return {}

    @admin.get("/free", rate_limit=None)
    async def admin_free(_: Request):
        return {}

    app = App(trusted_proxies=["127.0.0.1"])

    @app.post("/login", rate_limit=RateLimit("1/minute", burst=2))
    async def login(_: Request):
        return {}

    @app.get("/open")
    async def open_(_: Request):
        return {}

    @app.get("/x", rate_limit=shared)
    async def x(_: Request):
        return {}

    @app.get("/y", rate_limit=shared)
    async def y(_: Request):
        return {}

    app.include(admin)

    with TestClient(app) as client:
        me = as_client("198.51.100.7")
        got = [client.post("/login", headers=me).status_code for _ in range(3)]
        check(got == [200, 200, 429], f"login with burst 2 answered {got}")
        check(statuses(client, "/open", 5, headers=me) == [200] * 5,
              "a route without a limit was limited by another route's")
        got = [client.get(p, headers=me).status_code for p in ("/x", "/y", "/x", "/y")]
        check(got == [200, 200, 429, 429], f"one RateLimit on two routes answered {got}")
        got = [client.get(p, headers=me).status_code for p in ("/admin/a", "/admin/b")]
        check(got == [200, 429], f"a router's limit answered {got}: not one shared budget")
        check(statuses(client, "/admin/free", 3, headers=me) == [200] * 3,
              "rate_limit=None did not exempt a route from its router's limit")
        check(client.post("/login", headers=as_client("198.51.100.8")).status_code == 200,
              "a route's limit was shared between clients")


def both_limits_apply() -> None:
    app = App(rate_limit=RateLimit("1/minute", burst=3), trusted_proxies=["127.0.0.1"])

    @app.get("/strict", rate_limit=RateLimit("1/minute", burst=1))
    async def strict(_: Request):
        return {}

    @app.get("/plain")
    async def plain(_: Request):
        return {}

    with TestClient(app) as client:
        me = as_client("203.0.113.9")
        got = [client.get(p, headers=me).status_code
               for p in ("/strict", "/strict", "/plain", "/plain")]
        # The second /strict is refused by its route but has spent from the
        # app's budget: it was a request. So only one /plain is left.
        check(got == [200, 429, 200, 429], f"app and route limits together answered {got}")


def keyed_on_a_header() -> None:
    app = App(trusted_proxies=["127.0.0.1"])

    @app.get("/api", rate_limit=RateLimit("1/minute", burst=2, key="header:x-api-key"))
    async def api(_: Request):
        return {}

    with TestClient(app) as client:
        a = {"x-api-key": "alpha", **as_client("192.0.2.10")}
        a_elsewhere = {"x-api-key": "alpha", **as_client("192.0.2.11")}
        b = {"x-api-key": "beta", **as_client("192.0.2.10")}
        got = [client.get("/api", headers=h).status_code for h in (a, a_elsewhere, a)]
        check(got == [200, 200, 429], f"one key from two addresses answered {got}")
        check(client.get("/api", headers=b).status_code == 200,
              "a second key from the same address was limited by the first")
        bare = statuses(client, "/api", 3, headers=as_client("192.0.2.12"))
        check(bare == [200, 200, 429], f"without the header, by address, answered {bare}")


def the_client_address() -> None:
    def make(**options) -> App:
        app = App(**options)

        @app.get("/who")
        async def who(request: Request):
            return {"client": request.client}

        return app

    def who(client: TestClient, forwarded: str | None = None) -> str:
        headers = {} if forwarded is None else {"x-forwarded-for": forwarded}
        return client.get("/who", headers=headers).json()["client"]

    with TestClient(make()) as client:
        check(who(client) == "127.0.0.1", f"the peer read as {who(client)!r}")
        check(who(client, "6.6.6.6") == "127.0.0.1",
              "X-Forwarded-For was believed with no trusted proxies")

    with TestClient(make(trusted_proxies=["127.0.0.0/8", "10.0.0.0/8"])) as client:
        cases = {
            "6.6.6.6": "6.6.6.6",
            # The client wrote the left entry; the trusted proxy wrote the right.
            "6.6.6.6, 192.0.2.5": "192.0.2.5",
            "6.6.6.6, 192.0.2.5, 10.1.1.1": "192.0.2.5",
            "10.9.9.9, 10.1.1.1": "10.9.9.9",
            "192.0.2.5:4711": "192.0.2.5",
            "[2001:db8::1]:443": "2001:db8::1",
            "6.6.6.6, garbage, 10.1.1.1": "10.1.1.1",
            "": "127.0.0.1",
        }
        for forwarded, expected in cases.items():
            got = who(client, forwarded)
            check(got == expected, f"X-Forwarded-For {forwarded!r} gave {got!r}, not {expected}")
        two_headers = client.get("/who", headers=[("x-forwarded-for", "6.6.6.6"),
                                                  ("x-forwarded-for", "192.0.2.6")])
        check(two_headers.json()["client"] == "192.0.2.6",
              f"two X-Forwarded-For headers gave {two_headers.json()['client']!r}")

    with TestClient(make(trusted_proxies=["10.0.0.0/8"])) as client:
        check(who(client, "192.0.2.5") == "127.0.0.1",
              "X-Forwarded-For was believed from a peer outside the trusted networks")

    with TestClient(make(trusted_proxies=2)) as client:
        for forwarded, expected in {"6.6.6.6, 192.0.2.5, 10.1.1.1": "192.0.2.5",
                                    "192.0.2.5, 10.1.1.1": "192.0.2.5",
                                    "10.1.1.1": "127.0.0.1"}.items():
            got = who(client, forwarded)
            check(got == expected, f"two hops, {forwarded!r} gave {got!r}, not {expected}")

    check(Request(client="2001:db8::1").client == "2001:db8::1", "Request(client=) was lost")
    check(Request().client is None, "a request built by hand had a client")
    check(raises(ValueError, lambda: Request(client="nope")), "Request(client='nope') accepted")


def ipv6_counts_by_block() -> None:
    app = App(rate_limit=RateLimit("1/minute", burst=1), trusted_proxies=["127.0.0.1"])

    @app.get("/")
    async def root(_: Request):
        return {}

    with TestClient(app) as client:
        def status(ip: str) -> int:
            return client.get("/", headers=as_client(ip)).status_code

        check(status("2001:db8:0:1::1") == 200, "first IPv6 request refused")
        check(status("2001:db8:0:1::2") == 429, "another address in the same /64 was not limited")
        check(status("2001:db8:0:2::1") == 200, "a different /64 was limited")
        check(status("192.0.2.40") == 200, "first IPv4 request refused")
        check(status("::ffff:192.0.2.40") == 429,
              "an IPv4-mapped address was counted apart from the IPv4 one")


def agents_spend_the_same_budget() -> None:
    app = App(trusted_proxies=["127.0.0.1"])

    @app.get("/search", tool=True, rate_limit=RateLimit("1/minute", burst=2))
    async def search(_: Request, q: str = ""):
        return {"q": q}

    with TestClient(app) as client:
        me = as_client("192.0.2.77")
        check(client.get("/search", headers=me).status_code == 200, "HTTP call refused")
        check(client.call_tool("search", {"q": "a"}, headers=me) == {"q": "a"},
              "the tool call within budget failed")
        result = client.mcp("tools/call", {"name": "search", "arguments": {"q": "b"}},
                            headers=me)
        text = "".join(p.get("text", "") for p in result.get("content", []))
        check(result.get("isError") is True and "429" in text,
              f"a tool call over the route's limit was not refused: {result!r}")
        check(client.get("/search", headers=me).status_code == 429,
              "HTTP after the tool calls was not limited: the budgets are separate")
        other = as_client("192.0.2.78")
        check(client.call_tool("search", {"q": "c"}, headers=other) == {"q": "c"},
              "another agent was limited by the first")


def probes_and_scrapes_are_never_limited() -> None:
    health = Health()

    @health.check
    async def fine(_state):
        return True

    app = App(rate_limit=RateLimit("1/minute", burst=1), health=health, metrics=Metrics(),
              trusted_proxies=["127.0.0.1"])

    @app.get("/")
    async def root(_: Request):
        return {}

    with TestClient(app) as client:
        me = as_client("192.0.2.90")
        check(statuses(client, "/", 3, headers=me) == [200, 429, 429], "the limit did not hold")
        for path in ("/livez", "/readyz", "/metrics"):
            got = statuses(client, path, 3, headers=me)
            check(got == [200, 200, 200], f"{path} was limited: {got}")
        families = {f.name: f for f in
                    text_string_to_metric_families(client.get("/metrics").text)}
        limited = families["oxbrook_requests_limited"].samples[0].value
        check(limited == 2, f"oxbrook_requests_limited_total read {limited}, not 2")
        by_route = [s for s in families["oxbrook_requests"].samples
                    if s.labels.get("route") == "/" and s.labels.get("status") == "429"]
        check(by_route and by_route[0].value == 2,
              f"the 429s were not counted under their route: {by_route}")


def sockets_are_limited_before_the_handshake() -> None:
    app = App(trusted_proxies=["127.0.0.1"])
    opened = []

    @app.websocket("/ws", rate_limit=RateLimit("1/minute", burst=1))
    async def ws(_: Request, socket):
        opened.append(1)
        await socket.send("hi")

    async def attempt(client: TestClient) -> int:
        try:
            async with client.websocket("/ws", headers=as_client("192.0.2.60")) as socket:
                await socket.recv()
                return 101
        except websockets.InvalidStatus as exc:
            return exc.response.status_code

    with TestClient(app) as client:
        got = [asyncio.run(attempt(client)) for _ in range(2)]
        check(got == [101, 429], f"two socket opens answered {got}")
        check(len(opened) == 1, f"a refused socket reached its handler ({len(opened)})")


def memory_is_reclaimed() -> None:
    # One request every millisecond: an address's entry means nothing a
    # millisecond after its last request, and must be swept, not kept.
    limit = RateLimit("1000/second", burst=1)
    app = App(rate_limit=limit, trusted_proxies=["127.0.0.1"])

    @app.get("/")
    async def root(_: Request):
        return {}

    with TestClient(app) as client:
        for i in range(9000):
            client.get("/", headers=as_client(f"10.{i // 62500}.{i // 250 % 250}.{i % 250}"))
        tracked = limit._limiter._tracked()
        check(0 < tracked < 4500,
              f"{tracked} of 9000 addresses still tracked: expired entries were kept")


def the_guide_example_works() -> None:
    app = App(rate_limit=RateLimit("600/minute"), trusted_proxies=["10.0.0.0/8", "127.0.0.1"])

    @app.post("/login", rate_limit=RateLimit("5/minute"))
    async def login(_: Request):
        return {}

    with TestClient(app) as client:
        got = [client.post("/login").status_code for _ in range(6)]
        check(got == [200] * 5 + [429], f"the guide's login limit answered {got}")


def main() -> None:
    for step in (configuration_is_checked, the_app_limit_refuses_and_refills,
                 route_limits_are_their_own, both_limits_apply, keyed_on_a_header,
                 the_client_address, ipv6_counts_by_block, agents_spend_the_same_budget,
                 probes_and_scrapes_are_never_limited, sockets_are_limited_before_the_handshake,
                 memory_is_reclaimed, the_guide_example_works):
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
