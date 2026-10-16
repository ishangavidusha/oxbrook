#!/usr/bin/env python3
"""Cross-origin resource sharing, applied in Rust.

CORS lives in the server rather than in middleware because the server answers
several responses itself — 404, 405, 413, 503, 504 — without waking a worker. A
middleware never sees those, and a browser that receives one without CORS
headers reports a CORS failure instead of the real status, which sends whoever
is debugging it to the wrong place. These cases assert the headers arrive on
responses no handler produced.

Also held to account: a preflight answered without routing, a foreign origin
getting nothing, `Vary: Origin` so a shared cache cannot hand one origin's
answer to another, and the one configuration refused outright — credentials with
any origin, which would let every website act as a signed-in user.
"""
import sys

from oxbrook import CORS, App, Request, Response
from oxbrook.testing import TestClient

GOOD = "https://app.example.com"
EVIL = "https://evil.example"
failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


credentialed = App(
    openapi_url=None, docs_url=None, mcp_url=None,
    cors=CORS(allow_origins=[GOOD], allow_credentials=True, expose_headers=["x-total"],
              allow_methods=["GET", "POST"], allow_headers=["content-type"]),
)
public = App(openapi_url=None, docs_url=None, mcp_url=None, cors=CORS(allow_origins=["*"]))
plain = App(openapi_url=None, docs_url=None, mcp_url=None)

for target in (credentialed, public, plain):

    @target.get("/items")
    async def items(_: Request):
        return [1]

    @target.post("/items")
    async def create(_: Request):
        return {"ok": True}

    @target.get("/own")
    async def own(_: Request):
        return Response(b"x", headers={"access-control-allow-origin": "https://chosen"})


def acao(response) -> str | None:
    return response.headers.get("access-control-allow-origin")


def configuration_is_checked() -> None:
    for label, kwargs, kind in [
        ("credentials with any origin", {"allow_origins": ["*"], "allow_credentials": True},
         ValueError),
        ("an origin with a trailing slash", {"allow_origins": ["https://a.com/"]}, ValueError),
        ("an origin with no scheme", {"allow_origins": ["a.com"]}, ValueError),
        ("a single string", {"allow_origins": "https://a.com"}, TypeError),
        ("no origins", {"allow_origins": []}, ValueError),
        ("a * on a top-level domain", {"allow_origins": ["https://*.dev"]}, ValueError),
        ("a * past the first label", {"allow_origins": ["https://a.*.example.dev"]},
         ValueError),
        ("a * in the scheme", {"allow_origins": ["*://a.example.dev"]}, ValueError),
        ("two * in one label", {"allow_origins": ["https://*a*.example.dev"]}, ValueError),
    ]:
        try:
            CORS(**kwargs)
            failures.append(f"CORS accepted {label}")
        except kind:
            pass
    try:
        App(cors={"allow_origins": [GOOD]})
        failures.append("App accepted a dict for cors")
    except TypeError:
        pass


def actual_requests(client: TestClient) -> None:
    response = client.get("/items", headers={"origin": GOOD})
    check(acao(response) == GOOD, f"allowed origin got {acao(response)!r}")
    check(response.headers.get("access-control-allow-credentials") == "true",
          "credentials header missing for an allowed origin")
    check(response.headers.get("access-control-expose-headers") == "x-total",
          "expose-headers missing")
    check("Origin" in response.headers.get("vary", ""), "Vary: Origin missing")

    response = client.get("/items", headers={"origin": EVIL})
    check(acao(response) is None, f"a foreign origin was allowed: {acao(response)!r}")
    check(response.status_code == 200, "a foreign origin's request was refused server-side; "
          "CORS is enforced by the browser, not by refusing")

    response = client.get("/items")
    check("Origin" in response.headers.get("vary", ""),
          "a response to a request with no Origin lacks Vary: Origin, so a cache could "
          "serve it to a foreign site")

    response = client.get("/own", headers={"origin": GOOD})
    check(acao(response) == "https://chosen", "the server overwrote a handler's own CORS header")


def responses_no_handler_produced(client: TestClient) -> None:
    """The reason CORS is in Rust."""
    for label, response in [
        ("404", client.get("/nowhere", headers={"origin": GOOD})),
        ("405", client.request("DELETE", "/items", headers={"origin": GOOD})),
        ("413", client.post("/items", content=b"x" * 2048, headers={"origin": GOOD})),
    ]:
        check(label in str(response.status_code), f"expected {label}, got {response.status_code}")
        check(acao(response) == GOOD, f"a {label} from the server itself had no CORS headers")


def preflights(client: TestClient) -> None:
    response = client.options("/items", headers={
        "origin": GOOD, "access-control-request-method": "POST",
        "access-control-request-headers": "Content-Type",
    })
    check(response.status_code == 204, f"allowed preflight returned {response.status_code}")
    check(acao(response) == GOOD, f"allowed preflight origin {acao(response)!r}")
    check(response.headers.get("access-control-allow-methods") == "GET, POST",
          f"allow-methods was {response.headers.get('access-control-allow-methods')!r}")
    check(response.headers.get("access-control-allow-headers") == "content-type",
          f"allow-headers was {response.headers.get('access-control-allow-headers')!r}")
    check(response.headers.get("access-control-max-age") == "600", "max-age missing")

    for label, headers in [
        ("a foreign origin", {"origin": EVIL, "access-control-request-method": "POST"}),
        ("a method not listed", {"origin": GOOD, "access-control-request-method": "DELETE"}),
        ("a header not listed", {"origin": GOOD, "access-control-request-method": "POST",
                                 "access-control-request-headers": "x-secret"}),
    ]:
        response = client.options("/items", headers=headers)
        check(response.status_code == 400,
              f"preflight with {label} returned {response.status_code}")
        check(acao(response) is None, f"preflight with {label} was allowed")

    response = client.options("/does-not-exist", headers={
        "origin": GOOD, "access-control-request-method": "GET"})
    check(response.status_code == 204,
          "a preflight for a missing path was answered differently, revealing which paths exist")

    response = client.options("/items")
    check(response.status_code == 405,
          f"a plain OPTIONS was treated as a preflight: {response.status_code}")


def wildcard(client: TestClient) -> None:
    response = client.get("/items", headers={"origin": EVIL})
    check(acao(response) == "*", f"any-origin policy sent {acao(response)!r}")
    check(response.headers.get("access-control-allow-credentials") is None,
          "any-origin policy sent a credentials header")
    response = client.options("/items", headers={
        "origin": EVIL, "access-control-request-method": "PATCH",
        "access-control-request-headers": "x-anything"})
    check(response.status_code == 204, f"any-origin preflight returned {response.status_code}")
    check(response.headers.get("access-control-allow-methods") == "PATCH",
          "a wildcard method list did not echo the requested method")
    check(response.headers.get("access-control-allow-headers") == "x-anything",
          "a wildcard header list did not echo the requested headers")


def disabled(client: TestClient) -> None:
    response = client.get("/items", headers={"origin": GOOD})
    check(acao(response) is None and "vary" not in response.headers,
          "an app with no CORS policy sent CORS headers")
    response = client.options("/items", headers={"origin": GOOD,
                                                 "access-control-request-method": "POST"})
    check(response.status_code == 405,
          f"an app with no CORS answered a preflight: {response.status_code}")


#: (origin, allowed) against `https://*.preview.example.dev` and
#: `https://app-*-team.example.com`. Each refusal is a way origin patterns
#: are classically got wrong.
PATTERNED = [
    ("https://pr-12.preview.example.dev", True),
    ("https://PR-12.Preview.Example.dev", True),
    ("https://app-pr-12-team.example.com", True),
    ("https://preview.example.dev", False),                    # nothing for the *
    ("https://a.b.preview.example.dev", False),                # * is one label
    ("https://pr-12.preview.example.dev.attacker.com", False), # a suffix after
    ("https://attackerpreview.example.dev", False),            # a label glued on
    ("http://pr-12.preview.example.dev", False),               # another scheme
    ("https://pr-12.preview.example.dev:8443", False),         # another port
    ("https://pr_12.preview.example.dev", False),              # not a host label
    ("https://app--team.example.com", False),                  # empty middle
    ("https://app-x-team.example.com.attacker.com", False),
    ("https://evil-app-x-team.example.com", False),            # before the prefix
]


def patterns(client: TestClient) -> None:
    """I-057: one `*` in the first label of an origin, for preview
    deployments, and none of the ways such a pattern lets the wrong page in."""
    for origin, allowed in PATTERNED:
        r = client.get("/items", headers={"origin": origin})
        got = acao(r)
        if allowed:
            check(got == origin, f"{origin} should be allowed, got {got!r}")
        else:
            check(got is None, f"{origin} should be refused, got {got!r}")
        r = client.options("/items", headers={"origin": origin,
                                               "access-control-request-method": "POST"})
        check((acao(r) is not None) == allowed,
              f"preflight from {origin}: {r.status_code} {acao(r)!r}")


patterned = App(openapi_url=None, docs_url=None, mcp_url=None,
                cors=CORS(allow_origins=["https://*.preview.example.dev",
                                         "https://app-*-team.example.com"],
                          allow_credentials=True))


@patterned.get("/items")
async def patterned_items(_: Request):
    return [1]


def main() -> None:
    try:
        configuration_is_checked()
        print("  configuration_is_checked: ok")
    except Exception as exc:
        failures.append(f"configuration_is_checked raised {type(exc).__name__}: {exc}")

    for target, steps, options in [
        (credentialed, (actual_requests, responses_no_handler_produced, preflights),
         {"max_body": 1024}),
        (public, (wildcard,), {}),
        (plain, (disabled,), {}),
        (patterned, (patterns,), {}),
    ]:
        with TestClient(target, workers=1, **options) as client:
            for step in steps:
                try:
                    step(client)
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
