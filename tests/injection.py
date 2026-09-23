#!/usr/bin/env python3
"""Dependency injection, sessions, and logging."""
import io
import json
import logging
import sys

from oxbrook import App, Depends, HTTPError, Request, Sessions
from oxbrook._logging import JsonFormatter
from oxbrook.testing import TestClient

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def dependencies() -> None:
    app = App(openapi_url=None, docs_url=None, mcp_url=None)
    events: list[str] = []

    async def config():
        events.append("config")
        return {"dsn": "db://x"}

    async def db(cfg=Depends(config)):
        events.append("open")
        try:
            yield f"conn:{cfg['dsn']}"
        finally:
            events.append("close")

    def caller(request):
        return request.header("x-user", "anon")

    @app.get("/q")
    async def q(_: Request, conn=Depends(db), user=Depends(caller), cfg=Depends(config)):
        events.append("handler")
        return {"conn": conn, "user": user, "dsn": cfg["dsn"]}

    @app.get("/boom")
    async def boom(_: Request, conn=Depends(db)):
        raise ValueError("handler failed")

    with TestClient(app) as c:
        body = c.get("/q", headers={"x-user": "ada"}).json()
        check(body["conn"] == "conn:db://x", f"dependency value was {body['conn']!r}")
        check(body["user"] == "ada", "the request was not passed to a dependency")
        check(
            events == ["config", "open", "handler", "close"],
            f"resolution order was {events}",
        )
        check(events.count("config") == 1, "a shared dependency ran more than once")

        events.clear()
        check(c.get("/boom").status_code == 500, "a raising handler should be a 500")
        check(
            "close" in events,
            "teardown did not run when the handler raised, so a resource leaks",
        )


def teardown_sees_the_outcome() -> None:
    """A generator dependency is finished like a `with` block, not closed.

    Each case was demonstrated against a running server first. Teardown used
    to close the generator, which raises GeneratorExit at the yield: a
    transaction written the obvious way rolled back on every successful
    request, a handler's exception never reached the dependency, and a commit
    that failed was logged while the client was told 200.
    """
    app = App(openapi_url=None, docs_url=None, mcp_url=None)
    seen: list[tuple] = []

    class Conflict(Exception):
        pass

    async def transaction():
        try:
            yield "tx"
        except BaseException as exc:
            seen.append(("rollback", type(exc).__name__))
            raise
        else:
            seen.append(("commit",))

    async def failing_commit():
        yield "tx"
        raise RuntimeError("could not serialize access")

    async def translating():
        try:
            yield "tx"
        except Conflict:
            raise HTTPError(409, "already exists") from None

    async def swallowing():
        try:
            yield "tx"
        except Exception:
            pass

    async def outer():
        try:
            yield "outer"
        except BaseException as exc:
            seen.append(("outer saw", type(exc).__name__))
            raise

    async def inner(o=Depends(outer)):
        yield "inner"
        raise LookupError("inner teardown failed")

    async def greedy():
        yield 1
        yield 2

    @app.get("/ok")
    async def ok(_: Request, tx=Depends(transaction)):
        return {"ok": True}

    @app.get("/refused")
    async def refused(_: Request, tx=Depends(transaction)):
        raise HTTPError(403, "no")

    @app.get("/commit-fails")
    async def commit_fails(_: Request, tx=Depends(failing_commit)):
        return {"written": True}

    @app.get("/translated")
    async def translated(_: Request, tx=Depends(translating)):
        raise Conflict()

    @app.get("/swallowed")
    async def swallowed(_: Request, tx=Depends(swallowing)):
        raise ValueError("handler failed")

    @app.get("/nested")
    async def nested(_: Request, i=Depends(inner)):
        return {"ok": True}

    @app.get("/greedy")
    async def greedy_route(_: Request, g=Depends(greedy)):
        return {"ok": True}

    with TestClient(app) as c:
        check(c.get("/ok").status_code == 200, "a plain success did not answer 200")
        check(
            seen == [("commit",)],
            f"a successful request was torn down as {seen}; a transaction dependency "
            f"would roll back everything the handler wrote",
        )

        seen.clear()
        check(c.get("/refused").status_code == 403, "the handler's own error was lost")
        check(
            seen == [("rollback", "HTTPError")],
            f"teardown saw {seen}, not the handler's exception",
        )

        r = c.get("/commit-fails")
        check(
            r.status_code == 500,
            f"a teardown that failed after the handler returned answered "
            f"{r.status_code}: a failed commit must not be reported as a success",
        )
        check("serialize" not in r.text, "teardown exception detail reached the client")

        r = c.get("/translated")
        check(r.status_code == 409, f"a dependency's HTTPError at teardown gave {r.status_code}")

        r = c.get("/swallowed")
        check(
            r.status_code == 500,
            f"a dependency that swallowed the handler's error gave {r.status_code}; "
            f"there is no response to send in its place",
        )

        seen.clear()
        check(c.get("/nested").status_code == 500, "a failing inner teardown was not an error")
        check(
            seen == [("outer saw", "LookupError")],
            f"the outer dependency saw {seen}; teardown in reverse order should hand "
            f"it the inner one's exception",
        )

        check(c.get("/greedy").status_code == 500, "a dependency that yielded twice was accepted")


def locals_are_per_request() -> None:
    """`request.locals`: middleware hands a handler something, per request.

    There was nowhere to put it: `request.state` is shared by every request on
    a worker loop and read-only for that reason, so a middleware that
    authenticated a caller could only make a dependency derive it again.
    """
    import concurrent.futures

    app = App(openapi_url=None, docs_url=None, mcp_url="/mcp")

    @app.middleware
    async def who(request, call_next):
        caller = request.header("x-caller")
        if caller is not None:
            request.locals["caller"] = caller
        return await call_next(request)

    def caller(request):
        return request.locals.get("caller")

    @app.get("/me")
    async def me(request: Request, name=Depends(caller)):
        same = request.locals is request.locals
        return {"caller": name, "keys": sorted(request.locals), "same": same}

    @app.get("/tool", tool=True)
    async def tool(request: Request):
        """Report the caller the middleware saw on this tool call."""
        return {"caller": request.locals.get("caller")}

    with TestClient(app, workers=4) as c:
        body = c.get("/me", headers={"x-caller": "ada"}).json()
        check(body["caller"] == "ada", f"a dependency did not see what middleware left: {body}")
        check(body["same"], "request.locals was a new dict on each access")

        body = c.get("/me").json()
        check(
            body == {"caller": None, "keys": [], "same": True},
            f"a request with nothing written saw {body}: locals leaked between requests",
        )

        # Concurrent requests on shared worker loops, each with its own caller.
        # A client per thread: httpcore 1.0.9 checks a connection's expiry
        # and reads it in two steps, and on the free-threaded build another
        # thread can clear it in between (a TypeError comparing float and
        # None). That race is the client's, not the server's.
        import httpx

        def one(i):
            with httpx.Client(base_url=c.base_url) as own:
                return i, own.get("/me", headers={"x-caller": f"c{i}"}).json()["caller"]

        with concurrent.futures.ThreadPoolExecutor(32) as pool:
            crossed = [(i, got) for i, got in pool.map(one, range(200)) if got != f"c{i}"]
        check(not crossed, f"requests saw one another's locals: {crossed[:5]}")

        # A tool call is a request of its own, with the agent's headers, run
        # through the same middleware.
        result = c.mcp(
            "tools/call", {"name": "tool", "arguments": {}}, headers={"x-caller": "agent"}
        )
        text = "".join(part.get("text", "") for part in result["content"])
        check(json.loads(text) == {"caller": "agent"}, f"a tool call's locals gave {text}")


def no_cache() -> None:
    app = App(openapi_url=None, docs_url=None, mcp_url=None)
    calls = []

    def token():
        calls.append(1)
        return len(calls)

    @app.get("/twice")
    async def twice(
        _: Request, a=Depends(token, use_cache=False), b=Depends(token, use_cache=False)
    ):
        return {"a": a, "b": b}

    with TestClient(app) as c:
        body = c.get("/twice").json()
    check(body["a"] != body["b"], f"use_cache=False still cached: {body}")


def sessions_round_trip() -> None:
    store = Sessions(secret="unit-test-secret", secure=False)
    app = App(openapi_url=None, docs_url=None, mcp_url=None)
    app.middleware(store.middleware)

    @app.get("/bump")
    async def bump(_: Request, session=Depends(store.load)):
        session["n"] = session.get("n", 0) + 1
        return {"n": session["n"]}

    @app.get("/peek")
    async def peek(_: Request, session=Depends(store.load)):
        return {"n": session.get("n", 0)}

    with TestClient(app) as c:
        first = c.get("/bump")
        check(first.json() == {"n": 1}, f"first bump gave {first.json()}")
        check("set-cookie" in first.headers, "no session cookie was set")
        check("HttpOnly" in first.headers["set-cookie"], "session cookie is not HttpOnly")
        check(c.get("/bump").json() == {"n": 2}, "session did not persist")

        unchanged = c.get("/peek")
        check(unchanged.json() == {"n": 2}, "reading the session lost its contents")
        check(
            "set-cookie" not in unchanged.headers,
            "an unmodified session still rewrote its cookie",
        )

        # A forged or edited cookie must be treated as no session at all.
        c.http.cookies.set("oxbrook_session", "ZmFrZQ.bm90LWEtc2lnbmF0dXJl", domain="127.0.0.1")
        check(c.get("/peek").json() == {"n": 0}, "a forged session cookie was trusted")


def session_signing() -> None:
    store = Sessions(secret="a", secure=False)
    other = Sessions(secret="b", secure=False)
    token = store.encode({"user": "ada"})
    check(store.decode(token) == {"user": "ada"}, "a session did not round-trip")
    check(other.decode(token) == {}, "a session signed with another key was accepted")
    payload, _, sig = token.partition(".")
    check(store.decode(f"{payload}x.{sig}") == {}, "an edited payload was accepted")
    check(store.decode("garbage") == {}, "a malformed cookie raised instead of failing shut")
    expired = Sessions(secret="a", max_age=-1, secure=False)
    check(expired.decode(expired.encode({"x": 1})) == {}, "an expired session was accepted")


def logging_output() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("oxbrook")
    previous, previous_level = root.handlers[:], root.level
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    root.propagate = False

    try:
        app = App(openapi_url=None, docs_url=None, mcp_url=None, access_log=True)

        @app.get("/ok")
        async def ok(_: Request):
            return {"ok": True}

        @app.get("/boom")
        async def boom(_: Request):
            raise ValueError("leaky detail")

        with TestClient(app) as c:
            c.get("/ok")
            check(c.get("/boom").status_code == 500, "boom should be a 500")
    finally:
        root.handlers[:] = previous
        root.setLevel(previous_level)

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    check(bool(lines), "nothing was logged")
    access = [line for line in lines if line["logger"] == "oxbrook.access"]
    check(len(access) == 2, f"expected 2 access lines, got {len(access)}")
    check(
        access[0]["status"] == 200 and access[0]["path"] == "/ok",
        f"access line was {access[0]}",
    )
    check("duration_ms" in access[0], "access line has no duration")
    check(access[1]["status"] == 500, f"failed request logged as {access[1]['status']}")

    tracebacks = [line for line in lines if "exception" in line]
    check(len(tracebacks) == 1, f"traceback logged {len(tracebacks)} times, expected once")
    check(
        "leaky detail" in tracebacks[0]["exception"],
        "the traceback did not include the error",
    )


def main() -> None:
    for step in (
        dependencies,
        teardown_sees_the_outcome,
        locals_are_per_request,
        no_cache,
        sessions_round_trip,
        session_signing,
        logging_output,
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
