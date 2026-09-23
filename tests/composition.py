#!/usr/bin/env python3
"""Routers and exception handlers: an app built from more than one module.

**Routers.** Prefixes join literally and accumulate through nesting; a router's
middleware applies to its own routes only, inside the app's; a path parameter
declared in an outer prefix reaches the handler; and every mistake — a bad
prefix, a conflict between two routers, a route added after the router was
included — fails at the line that caused it rather than at startup.

**Exception handlers.** The most specific registered class wins; `HTTPError`
works with no registration; a handler for `RequestValidationError` reshapes a
422. Mapping happens inside middleware, so middleware sees the status the client
gets. That last one was a defect found while building this: an `HTTPError`
raised by a router's middleware reached the app's middleware as an exception, so
an access log there would have recorded a 403 as a 500. Mapping now wraps every
link of the middleware chain.

**Tool calls.** Two more defects, demonstrated against a running server first:

* A `tool=True` route on a router guarded by auth middleware answered 403 over
  HTTP and ran anyway over MCP, because a tool call invoked the bare handler.
  An admin delete endpoint was callable by any agent. A tool call now runs its
  routers' middleware, with the headers the agent sent.
* A tool that raised returned `TypeName: message` to the agent — the exception
  text an HTTP 500 is built never to return. It now returns "internal error".
"""
import asyncio
import sys

import websockets
from oxbrook import App, Depends, HTTPError, Reply, Request, RequestValidationError, Router
from oxbrook.testing import TestClient
from pydantic import BaseModel

failures: list[str] = []
SECRET = "postgres://user:hunter2@internal/prod"


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def raises(fn, kind: type) -> str | None:
    try:
        fn()
    except kind as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# the app under test
# ---------------------------------------------------------------------------
seen_by_app_middleware: list[tuple[str, int]] = []


class Missing(LookupError):
    pass


class Gone(Missing):
    pass


class Item(BaseModel):
    n: int


users = Router(prefix="/users")
admin = Router(prefix="/admin")
tenant = Router(prefix="/t/{tenant}")
sockets = Router(prefix="/live")


@users.get("")
async def list_users(_: Request):
    return ["ada"]


@users.get("/")
async def slash(_: Request):
    return "slash"


@users.get("/{user_id}", tool=True)
async def get_user(_: Request, user_id: int):
    """Look up one user."""
    return {"id": user_id}


@users.patch("/{user_id}")
async def patch_user(_: Request, user_id: int):
    return {"patched": user_id}


@users.middleware
async def users_mark(request, call_next):
    reply = await call_next(request)
    reply.headers["x-trail"] = reply.headers.get("x-trail", "") + "users;"
    return reply


@admin.middleware
async def admin_key(request, call_next):
    if request.header("x-key") != "k":
        raise HTTPError(403, "admin only")
    reply = await call_next(request)
    reply.headers["x-trail"] = reply.headers.get("x-trail", "") + "admin;"
    return reply


@admin.get("/stats")
async def stats(_: Request):
    return {"ok": True}


@admin.delete("/purge/{user_id}", tool=True)
async def purge(_: Request, user_id: int):
    """Delete a user for good."""
    return {"purged": user_id}


@tenant.get("/me")
async def me(_: Request, tenant: str):
    return {"tenant": tenant}


async def members(request):
    if request.header("x-member") != "yes":
        raise HTTPError(401, "members only")


@sockets.websocket("/echo", authorize=members)
async def echo(_request, ws):
    async for message in ws:
        await ws.send(message)


users.include(admin)

app = App(openapi_url="/openapi.json", docs_url=None, mcp_url="/mcp")


@app.middleware
async def app_status(request, call_next):
    reply = await call_next(request)
    status = reply.status if reply.status is not None else getattr(reply.value, "status", 200)
    seen_by_app_middleware.append((request.path, status))
    reply.headers["x-app"] = "1"
    return reply


@app.exception_handler(LookupError)
async def lookup(_request, exc):
    return Reply({"missing": str(exc)}, status=404)


@app.exception_handler(Gone)
async def gone(_request, exc):
    return Reply({"gone": str(exc)}, status=410)


@app.exception_handler(RequestValidationError)
async def invalid(_request, exc):
    return Reply({"problems": len(exc.errors)}, status=400)


class Taken(Exception):
    pass


@app.exception_handler(Taken)
async def taken(_request, exc):
    # The short way to answer with problem details: raise from the handler.
    raise HTTPError(409, f"{exc} is taken")


@app.get("/taken")
async def taken_route(_: Request):
    raise Taken("ada")


@app.exception_handler(ZeroDivisionError)
async def broken_handler(_request, _exc):
    raise RuntimeError(f"the exception handler itself failed: {SECRET}")


@app.get("/missing")
async def missing(_: Request):
    raise Missing("user 7")


@app.get("/gone")
async def gone_route(_: Request):
    raise Gone("user 8")


@app.get("/teapot")
async def teapot(_: Request):
    raise HTTPError(418, headers={"x-tea": "earl grey"})


async def needs_token(request):
    raise HTTPError(401, "token required")


@app.get("/dependency")
async def dependency(_: Request, _token=Depends(needs_token)):
    return {}


@app.post("/items")
async def items(_: Request, item: Item):
    return {"n": item.n}


@app.patch("/things/{thing}")
async def patch_thing(_: Request, thing: str):
    return {"thing": thing}


@app.get("/divide")
async def divide(_: Request):
    return 1 / 0


@app.get("/unhandled")
async def unhandled(_: Request):
    raise RuntimeError(f"unhandled: {SECRET}")


@app.get("/leaky", tool=True)
async def leaky(_: Request):
    """A tool whose failure carries a secret."""
    raise RuntimeError(f"tool failed: {SECRET}")


@app.get("/refuses", tool=True)
async def refuses(_: Request):
    """A tool that refuses with an error written for the caller."""
    raise HTTPError(409, "already exists")


app.include(users, prefix="/api")
app.include(tenant)
app.include(sockets)

# A separate app with no middleware and no handlers, which takes the runtime's
# inline path for HTTPError rather than any wrapper.
bare = App(openapi_url=None, docs_url=None, mcp_url=None)


@bare.get("/nope")
async def nope(_: Request):
    raise HTTPError(404, "nothing here", headers={"x-why": "none"})


# ---------------------------------------------------------------------------
# registration-time checks
# ---------------------------------------------------------------------------
def registration_mistakes_fail_where_they_are_made() -> None:
    check(raises(lambda: Router(prefix="users"), ValueError) is not None,
          "a prefix without a leading slash was accepted")
    check(raises(lambda: Router(prefix="/users/"), ValueError) is not None,
          "a prefix with a trailing slash was accepted")

    def bare_path():
        r = Router()

        @r.get("")
        async def root(_: Request):
            return 1

    check(raises(bare_path, ValueError) is not None,
          "an empty path on a router with no prefix was accepted")

    def late():
        @users.get("/late")
        async def added_late(_: Request):
            return 1

    message = raises(late, RuntimeError)
    check(message is not None and "already been included" in message,
          f"a route added after include was accepted: {message!r}")

    def cross_router_conflict():
        a, b = Router(prefix="/x"), Router(prefix="/x")

        @a.get("/{one}")
        async def first(_: Request, one: str):
            return 1

        @b.get("/{two}")
        async def second(_: Request, two: str):
            return 2

        target = App(openapi_url=None, docs_url=None, mcp_url=None)
        target.include(a)
        target.include(b)

    message = raises(cross_router_conflict, ValueError)
    check(message is not None and "conflicts with" in message,
          f"two routers with the same route shape were both included: {message!r}")

    def handler_signature_checked_at_decoration():
        r = Router(prefix="/p")

        @r.get("/{a}")
        async def missing_param(_: Request):
            return 1

    check(raises(handler_signature_checked_at_decoration, TypeError) is not None,
          "a router handler missing its path parameter was accepted")

    def sync_exception_handler():
        other = App(openapi_url=None, docs_url=None, mcp_url=None)

        @other.exception_handler(ValueError)
        def not_async(_request, _exc):
            return None

    check(raises(sync_exception_handler, TypeError) is not None,
          "a sync exception handler was accepted")

    def duplicate_exception_handler():
        other = App(openapi_url=None, docs_url=None, mcp_url=None)

        @other.exception_handler(ValueError)
        async def one(_request, _exc):
            return None

        @other.exception_handler(ValueError)
        async def two(_request, _exc):
            return None

    check(raises(duplicate_exception_handler, ValueError) is not None,
          "a second handler for the same class was accepted silently")
    check(raises(lambda: HTTPError(302), ValueError) is not None,
          "HTTPError accepted a non-error status")


# ---------------------------------------------------------------------------
# served behaviour
# ---------------------------------------------------------------------------
def routers_mount_where_expected(client: TestClient) -> None:
    for path, want in [
        ("/api/users", ["ada"]),
        ("/api/users/", "slash"),
        ("/api/users/7", {"id": 7}),
        ("/t/acme/me", {"tenant": "acme"}),
    ]:
        response = client.get(path)
        check(response.status_code == 200, f"{path} returned {response.status_code}")
        if response.status_code == 200:
            is_json = "json" in response.headers.get("content-type", "")
            body = response.json() if is_json else response.text
            check(body == want, f"{path} gave {body!r}, expected {want!r}")

    response = client.request("PATCH", "/api/users/9")
    check(response.status_code == 200 and response.json() == {"patched": 9},
          f"PATCH on a router returned {response.status_code} {response.text}")
    response = client.request("PATCH", "/things/x")
    check(response.status_code == 200, f"PATCH on the app returned {response.status_code}")

    paths = set(client.get("/openapi.json").json()["paths"])
    for expected in ("/api/users/{user_id}", "/api/users/admin/stats", "/t/{tenant}/me"):
        check(expected in paths, f"OpenAPI is missing {expected}; it has {sorted(paths)}")

    tools = {tool["name"] for tool in client.mcp("tools/list")["tools"]}
    check(any("user" in name for name in tools),
          f"a tool=True route on a router did not reach MCP: {tools}")


def router_middleware_is_scoped_and_ordered(client: TestClient) -> None:
    response = client.get("/api/users/admin/stats", headers={"x-key": "k"})
    check(response.status_code == 200, f"keyed admin request returned {response.status_code}")
    # Innermost unwinds first: admin adds before users, users before the app.
    check(response.headers.get("x-trail") == "admin;users;",
          f"router middleware ran in the wrong order: {response.headers.get('x-trail')!r}")
    check(response.headers.get("x-app") == "1", "app middleware did not wrap a router route")

    response = client.get("/t/acme/me")
    check("x-trail" not in response.headers,
          "a router's middleware ran on a route outside that router")


def inner_middleware_errors_reach_outer_middleware_as_replies(client: TestClient) -> None:
    """The defect found while building routers."""
    seen_by_app_middleware.clear()
    response = client.get("/api/users/admin/stats")
    check(response.status_code == 403, f"unkeyed admin request returned {response.status_code}")
    check(response.json().get("detail") == "admin only", f"403 body was {response.text!r}")
    check(("/api/users/admin/stats", 403) in seen_by_app_middleware,
          f"app middleware did not see the 403 as a reply: {seen_by_app_middleware}")
    check(response.headers.get("x-app") == "1",
          "app middleware was skipped by an exception from a router's middleware")


def exception_handlers_map_by_class(client: TestClient) -> None:
    response = client.get("/missing")
    check(response.status_code == 404 and response.json() == {"missing": "user 7"},
          f"a registered class mapped to {response.status_code} {response.text}")
    response = client.get("/gone")
    check(response.status_code == 410,
          f"the more specific handler did not win: {response.status_code} {response.text}")

    response = client.get("/teapot")
    check(response.status_code == 418, f"HTTPError(418) returned {response.status_code}")
    check(response.json() == {"type": "about:blank", "title": "I'm a Teapot", "status": 418},
          f"HTTPError with no detail: {response.text}")
    check(response.headers.get("x-tea") == "earl grey", "HTTPError headers were dropped")

    response = client.get("/dependency")
    check(response.status_code == 401 and response.json().get("detail") == "token required",
          f"HTTPError from a dependency returned {response.status_code} {response.text}")

    # An exception handler that raises HTTPError. Demonstrated before the fix:
    # the answer was right, but the app middleware around the route saw an
    # exception instead of a reply, so it never recorded the status and its
    # header was missing from the response.
    seen_by_app_middleware.clear()
    response = client.get("/taken")
    check(response.status_code == 409 and response.json().get("detail") == "ada is taken",
          f"an exception handler raising HTTPError gave {response.status_code} {response.text}")
    check(("/taken", 409) in seen_by_app_middleware,
          f"app middleware did not see the handler's HTTPError as a reply: "
          f"{seen_by_app_middleware}")
    check(response.headers.get("x-app") == "1",
          "app middleware was skipped by an HTTPError raised from an exception handler")

    response = client.post("/items", content=b'{"n": "not a number"}')
    check(response.status_code == 400 and response.json() == {"problems": 1},
          f"a RequestValidationError handler did not apply: {response.status_code} {response.text}")

    seen_by_app_middleware.clear()
    client.get("/missing")
    check(("/missing", 404) in seen_by_app_middleware,
          f"middleware saw {seen_by_app_middleware} rather than the mapped 404")


def failures_still_hide_detail(client: TestClient) -> None:
    for path in ("/divide", "/unhandled"):
        response = client.get(path)
        check(response.status_code == 500, f"{path} returned {response.status_code}")
        check(SECRET not in response.text, f"{path} leaked exception text")


def authorizers_can_raise(client: TestClient) -> None:
    async def attempt(headers):
        try:
            async with websockets.connect(
                f"{client.ws_url}/live/echo", additional_headers=headers
            ) as ws:
                await ws.send("hi")
                return await ws.recv()
        except websockets.InvalidStatus as exc:
            return exc.response.status_code

    refused = asyncio.run(attempt({}))
    check(refused == 401, f"an authorizer raising HTTPError(401) gave {refused!r}")
    accepted = asyncio.run(attempt({"x-member": "yes"}))
    check(accepted == "hi", f"an accepted socket under a router prefix gave {accepted!r}")


def call_tool_raw(client: TestClient, name: str, arguments: dict, headers=None) -> dict:
    """The raw result, including isError, with the agent's own headers on it."""
    return client.mcp(
        "tools/call", {"name": name, "arguments": arguments}, headers=headers
    )


def tool_calls_run_router_middleware(client: TestClient) -> None:
    """The bypass: HTTP refused this call, MCP ran it."""
    check(client.delete("/api/users/admin/purge/7").status_code == 403,
          "the admin route did not refuse an unkeyed HTTP request")

    result = call_tool_raw(client, "purge", {"user_id": 7})
    text = "".join(part.get("text", "") for part in result.get("content", []))
    check(result.get("isError") is True and "purged" not in text,
          f"an unkeyed tool call ran a route its router's middleware refuses: {result}")
    check("admin only" in text, f"the refusal did not carry the middleware's detail: {text!r}")

    result = call_tool_raw(client, "purge", {"user_id": 7}, headers={"x-key": "k"})
    check(result.get("isError") is False,
          f"a keyed tool call was refused; the agent's headers were not passed on: {result}")


def tool_errors_hide_detail(client: TestClient) -> None:
    result = call_tool_raw(client, "leaky", {})
    text = "".join(part.get("text", "") for part in result.get("content", []))
    check(result.get("isError") is True, f"a raising tool did not set isError: {result}")
    check(SECRET not in text, f"a raising tool returned its exception text to the agent: {text!r}")

    result = call_tool_raw(client, "refuses", {})
    text = "".join(part.get("text", "") for part in result.get("content", []))
    check(result.get("isError") is True and "already exists" in text,
          f"an HTTPError from a tool did not reach the agent as an error: {result}")


def bare_app_answers_http_error_inline() -> None:
    with TestClient(bare, workers=1) as client:
        response = client.get("/nope")
        check(response.status_code == 404, f"bare HTTPError returned {response.status_code}")
        check(response.json().get("detail") == "nothing here",
              f"bare HTTPError body: {response.text}")
        check(response.headers.get("x-why") == "none", "bare HTTPError dropped its headers")


def main() -> None:
    steps = [registration_mistakes_fail_where_they_are_made, bare_app_answers_http_error_inline]
    for step in steps:
        try:
            step()
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR")

    with TestClient(app, workers=2) as client:
        for step in (
            routers_mount_where_expected,
            router_middleware_is_scoped_and_ordered,
            inner_middleware_errors_reach_outer_middleware_as_replies,
            exception_handlers_map_by_class,
            failures_still_hide_detail,
            authorizers_can_raise,
            tool_calls_run_router_middleware,
            tool_errors_hide_detail,
        ):
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
