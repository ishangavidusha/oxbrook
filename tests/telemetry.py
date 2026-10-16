#!/usr/bin/env python3
"""OpenTelemetry tracing through `App(telemetry=Telemetry())`.

Spans are read back from the SDK's in-memory exporter, so what is checked is
what an exporter would receive, not what the adapter meant to send. Each
attribute is held to OpenTelemetry's HTTP conventions, and each claim the
docstring makes is made to happen: an incoming `traceparent` joins the trace,
a handler and its after-response work see the span, a crash records its
exception, a timeout is a `504`, the queue wait is in the span.

And the other half of the promise: an app without telemetry never imports
OpenTelemetry and serves through the plain runner, checked in a fresh
interpreter where nothing else could have imported it first.
"""
import asyncio
import subprocess
import sys
import time

import httpx
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from oxbrook import SSE, App, Health, HTTPError, Request, Telemetry
from oxbrook.testing import TestClient

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE}-{PARENT}-01"


class Spans:
    """A provider of its own per test, so one test's spans never reach
    another's assertions."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))

    def telemetry(self, **options) -> Telemetry:
        return Telemetry(tracer_provider=self.provider, **options)

    def wait(self, count: int, timeout: float = 5.0) -> list:
        """The span ends on the worker after the reply is out, so the client
        can see the answer a moment before the exporter sees the span."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            finished = self.exporter.get_finished_spans()
            if len(finished) >= count:
                return list(finished)
            time.sleep(0.01)
        return list(self.exporter.get_finished_spans())

    def named(self, name: str) -> list:
        return [s for s in self.exporter.get_finished_spans() if s.name == name]


def make(spans: Spans, **options) -> App:
    app = App(telemetry=spans.telemetry(), health=Health(), **options)
    tracer = spans.provider.get_tracer("handler")

    @app.get("/items/{item_id}")
    async def item(_: Request, item_id: int):
        return {"id": item_id}

    @app.get("/context")
    async def context(request: Request):
        current = trace.get_current_span().get_span_context()
        with tracer.start_as_current_span("inner"):
            pass

        async def later():
            with tracer.start_as_current_span("after"):
                pass

        request.after_response(later)
        return {"trace": f"{current.trace_id:032x}", "span": f"{current.span_id:016x}"}

    @app.get("/crash")
    async def crash(_: Request):
        raise ValueError("boom")

    @app.get("/missing")
    async def missing(_: Request):
        raise HTTPError(404)

    @app.get("/typed")
    async def typed(_: Request, count: int):
        return {"count": count}

    @app.get("/slow")
    async def slow(_: Request, seconds: float = 2.0):
        await asyncio.sleep(seconds)
        return {}

    @app.get("/block")
    async def block(_: Request):
        time.sleep(0.4)  # holds the loop, so the next request waits for it
        return {}

    @app.get("/events")
    async def events(_: Request):
        async def source():
            for n in range(3):
                yield {"n": n}
        return SSE(source())

    async def shared(_: Request):
        return {}

    app.get("/one")(shared)
    app.get("/two")(shared)

    @app.websocket("/echo")
    async def echo(_: Request, ws):
        async for message in ws:
            await ws.send(message)

    async def members_only(request: Request):
        return request.query == "member=1"

    @app.websocket("/lounge", authorize=members_only)
    async def lounge(_: Request, ws):
        await ws.send("welcome")

    return app


def configuration_is_checked() -> None:
    for label, build, error in (
        ("a string for exclude", lambda: Telemetry(exclude="/x"), TypeError),
        ("a relative path in exclude", lambda: Telemetry(exclude=["x"]), ValueError),
        ("something else as telemetry", lambda: App(telemetry=object()), TypeError),
    ):
        try:
            build()
            failures.append(f"accepted {label}")
        except error:
            pass


def off_costs_nothing() -> None:
    """Without telemetry the runner is the plain one and OpenTelemetry is
    never imported. A fresh interpreter, since this one imported it above."""
    script = """
import sys
from oxbrook import App, Request, _runtime
from oxbrook._lifecycle import Lifecycle
from oxbrook.testing import TestClient

app = App()

@app.get("/")
async def index(_: Request):
    return {}

with TestClient(app, workers=1) as client:
    assert client.get("/").status_code == 200
lifecycle = Lifecycle(app)
assert lifecycle.run_handler is _runtime.run_handler, "not the plain runner"
assert lifecycle.run_websocket is _runtime.run_websocket, "not the plain socket runner"
loaded = sorted(m for m in sys.modules if m.split(".")[0] == "opentelemetry")
assert not loaded, f"imported {loaded}"
print("clean")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            timeout=60)
    check(result.returncode == 0 and "clean" in result.stdout,
          f"an app without telemetry was not left alone: {result.stderr.strip()[-500:]}")


def a_request_is_a_server_span() -> None:
    spans = Spans()
    with TestClient(make(spans), workers=2) as client:
        r = client.get("/items/7", headers={"user-agent": "probe/1.0"})
        check(r.status_code == 200, f"/items/7 answered {r.status_code}")
        finished = spans.wait(1)
    check(len(finished) == 1, f"one request made {len(finished)} spans")
    if not finished:
        return
    span = finished[0]
    check(span.name == "GET /items/{item_id}", f"span named {span.name!r}, not after the route")
    check(span.kind == SpanKind.SERVER, f"span kind {span.kind}")
    expected = {
        "http.request.method": "GET",
        "http.route": "/items/{item_id}",
        "url.path": "/items/7",
        "url.scheme": "http",
        "http.response.status_code": 200,
        "client.address": "127.0.0.1",
        "user_agent.original": "probe/1.0",
    }
    for key, want in expected.items():
        got = span.attributes.get(key)
        check(got == want, f"{key} was {got!r}, not {want!r}")
    check(span.parent is None, "a request with no traceparent was given a parent")
    check(span.status.status_code == StatusCode.UNSET, f"a 200 had status {span.status}")
    check(span.start_time <= span.end_time, "the span ended before it started")
    check(span.instrumentation_scope.name == "oxbrook", "the tracer is not named oxbrook")


def a_caller_trace_is_joined() -> None:
    spans = Spans()
    with TestClient(make(spans), workers=1) as client:
        r = client.get("/context", headers={"traceparent": TRACEPARENT})
        seen = r.json()
        finished = spans.wait(3)
    server = [s for s in finished if s.name == "GET /context"]
    check(len(server) == 1, f"/context made {len(server)} server spans")
    if not server:
        return
    span = server[0]
    check(f"{span.context.trace_id:032x}" == TRACE, "the span did not join the caller's trace")
    check(span.parent is not None and f"{span.parent.span_id:016x}" == PARENT,
          "the span's parent is not the caller's span")
    check(span.parent is not None and span.parent.is_remote, "the parent is not marked remote")
    check(seen["trace"] == TRACE, "the handler did not see the caller's trace as current")
    check(seen["span"] == f"{span.context.span_id:016x}",
          "the handler's current span is not the server span")
    for name in ("inner", "after"):
        child = [s for s in finished if s.name == name]
        check(len(child) == 1 and child[0].parent is not None
              and child[0].parent.span_id == span.context.span_id,
              f"the handler's {name!r} span is not a child of the server span")


def failures_are_recorded() -> None:
    spans = Spans()
    with TestClient(make(spans), workers=1) as client:
        check(client.get("/crash").status_code == 500, "/crash did not answer 500")
        check(client.get("/missing").status_code == 404, "/missing did not answer 404")
        check(client.get("/typed", params={"count": "x"}).status_code == 422,
              "a bad parameter did not answer 422")
        spans.wait(2)
    crash = spans.named("GET /crash")
    if not crash:
        failures.append("no span for /crash")
    else:
        span = crash[0]
        check(span.status.status_code == StatusCode.ERROR, "a 500 was not an error span")
        check(span.attributes.get("http.response.status_code") == 500, "the 500 was not recorded")
        check(span.attributes.get("error.type") == "ValueError",
              f"error.type was {span.attributes.get('error.type')!r}")
        events = [e for e in span.events if e.name == "exception"]
        check(len(events) == 1 and events[0].attributes.get("exception.message") == "boom",
              "the exception was not recorded on the span")
    missing = spans.named("GET /missing")
    if missing:
        span = missing[0]
        check(span.attributes.get("http.response.status_code") == 404, "the 404 was not recorded")
        check(span.status.status_code == StatusCode.UNSET,
              "a 404 marked a server span as an error; only 5xx should")
    else:
        failures.append("no span for /missing")
    # A parameter Rust refuses is answered before any handler: no span, like
    # any request the server answers itself.
    check(not spans.named("GET /typed"), "a 422 from Rust's own coercion made a span")


def a_timeout_is_a_504() -> None:
    spans = Spans()
    with TestClient(make(spans), workers=1, request_timeout=0.3) as client:
        check(client.get("/slow").status_code == 504, "/slow did not time out")
        finished = spans.wait(1)
    slow = [s for s in finished if s.name == "GET /slow"]
    if not slow:
        failures.append("a timed-out request made no span")
        return
    span = slow[0]
    check(span.attributes.get("http.response.status_code") == 504,
          f"a timeout recorded status {span.attributes.get('http.response.status_code')}")
    check(span.attributes.get("error.type") == "timeout", "a timeout's error.type is not timeout")
    check(span.status.status_code == StatusCode.ERROR, "a timeout was not an error span")


def the_queue_wait_is_in_the_span() -> None:
    spans = Spans()
    with TestClient(make(spans), workers=1) as client:
        url = client.base_url

        async def both():
            async with httpx.AsyncClient(base_url=url) as http:
                first = asyncio.ensure_future(http.get("/block"))
                await asyncio.sleep(0.1)
                await http.get("/items/1")
                await first

        asyncio.run(both())
        spans.wait(2)
    waited = spans.named("GET /items/{item_id}")
    if not waited:
        failures.append("no span for the request that waited")
        return
    span = waited[0]
    wait = span.attributes.get("oxbrook.queue.wait")
    check(wait is not None and wait >= 0.2,
          f"a request held behind a 0.4 s handler recorded a wait of {wait}")
    duration = (span.end_time - span.start_time) / 1e9
    check(wait is not None and duration >= wait,
          f"the span ({duration:.3f}s) does not cover the wait ({wait}s)")


def streams_and_sockets_are_spans() -> None:
    import websockets

    spans = Spans()
    with TestClient(make(spans), workers=1) as client:
        with client.stream("GET", "/events") as r:
            body = r.read()
        check(body.count(b"data:") == 3, "the stream did not carry three events")
        ws_url = client.base_url.replace("http://", "ws://")

        async def sockets():
            async with websockets.connect(f"{ws_url}/echo") as ws:
                await ws.send("hi")
                check(await ws.recv() == "hi", "the echo socket did not echo")
            async with websockets.connect(f"{ws_url}/lounge?member=1") as ws:
                check(await ws.recv() == "welcome", "the lounge did not welcome a member")
            try:
                async with websockets.connect(f"{ws_url}/lounge"):
                    failures.append("the lounge let a non-member in")
            except websockets.InvalidStatus:
                pass

        asyncio.run(sockets())
        spans.wait(6)
    events = spans.named("GET /events")
    check(len(events) == 1 and events[0].attributes.get("http.response.status_code") == 200,
          "the SSE stream made no span with its status")
    echo = spans.named("WebSocket /echo")
    check(len(echo) == 1, f"the echo socket made {len(echo)} session spans")
    check(not spans.named("GET /echo"),
          "a socket whose handshake Rust decided made a handshake span")
    gates = spans.named("GET /lounge")
    statuses = sorted(s.attributes.get("http.response.status_code") for s in gates)
    check(statuses == [101, 403], f"the lounge's handshakes recorded {statuses}, not [101, 403]")
    check(len(spans.named("WebSocket /lounge")) == 1,
          "the accepted lounge socket made no session span")


def agent_calls_are_mcp_spans() -> None:
    """Each MCP request is a span after OpenTelemetry's MCP conventions,
    parented by the context the agent sent in `params._meta`."""
    spans = Spans()
    app = App(telemetry=spans.telemetry())

    @app.get("/search", tool=True)
    async def search(_: Request, q: str):
        return {"hits": [q]}

    @app.get("/explode", tool=True)
    async def explode(_: Request):
        raise RuntimeError("tool broke")

    with TestClient(app, workers=1) as client:
        client.mcp("tools/list")
        client.mcp("tools/call", {"name": "search", "arguments": {"q": "ox"},
                                  "_meta": {"traceparent": TRACEPARENT}}, request_id=7)
        broke = client.mcp("tools/call", {"name": "explode", "arguments": {}})
        check(broke.get("isError") is True, "a raising tool was not an error result")
        try:
            client.mcp("tools/call", {"name": "nonexistent"})
            failures.append("calling a missing tool did not fail")
        except Exception:  # the client raises the JSON-RPC error
            pass
        spans.wait(8)
    names = sorted(s.name for s in spans.exporter.get_finished_spans())
    for want in ("tools/list", "tools/call search", "tools/call explode",
                 "tools/call nonexistent"):
        check(want in names, f"no {want!r} span among {names}")
    call = spans.named("tools/call search")
    if call:
        span = call[0]
        attributes = span.attributes
        for key, want in (("mcp.method.name", "tools/call"), ("gen_ai.tool.name", "search"),
                          ("gen_ai.operation.name", "execute_tool"),
                          ("jsonrpc.request.id", "7"), ("network.transport", "tcp")):
            check(attributes.get(key) == want, f"{key} was {attributes.get(key)!r}, not {want!r}")
        check(attributes.get("mcp.session.id"), "the tool call carried no session id")
        check(span.kind == SpanKind.SERVER, f"the MCP span kind was {span.kind}")
        check(f"{span.context.trace_id:032x}" == TRACE,
              "a traceparent in params._meta did not set the MCP span's trace")
        posts = [s for s in spans.named("POST /mcp")
                 if any(link.context.span_id == s.context.span_id for link in span.links)]
        check(len(posts) == 1, "the MCP span is not linked to its HTTP request's span")
    explode_span = spans.named("tools/call explode")
    if explode_span:
        span = explode_span[0]
        check(span.attributes.get("error.type") == "tool_error",
              "a tool that failed did not set error.type to tool_error")
        check(span.status.status_code == StatusCode.ERROR, "a failed tool call was not an error")
        check(any(e.name == "exception" for e in span.events),
              "the tool's exception was not recorded on its span")
    missing = spans.named("tools/call nonexistent")
    if missing:
        check(missing[0].attributes.get("rpc.response.status_code") == "-32602",
              "a JSON-RPC error's code was not recorded")
    listed = spans.named("tools/list")
    if listed:
        posts = {s.context.span_id for s in spans.named("POST /mcp")}
        check(listed[0].parent is not None and listed[0].parent.span_id in posts,
              "an MCP span with no _meta context is not a child of its HTTP span")


def what_is_left_out() -> None:
    spans = Spans()
    app = make(spans)
    app.telemetry = spans.telemetry(exclude=["/items/{item_id}"])
    with TestClient(app, workers=1) as client:
        client.get("/items/1")
        client.get("/readyz")
        client.get("/livez")
        client.get("/no/such/path")
        client.get("/one")
        client.get("/two")
        finished = spans.wait(2)
    names = sorted(s.name for s in finished)
    check(names == ["GET /one", "GET /two"],
          f"spans were {names}; an excluded route, probes and a 404 should make none, "
          "and one function on two routes should make one each")


def main() -> None:
    for step in (configuration_is_checked, off_costs_nothing, a_request_is_a_server_span,
                 a_caller_trace_is_joined, failures_are_recorded, a_timeout_is_a_504,
                 the_queue_wait_is_in_the_span, streams_and_sockets_are_spans,
                 agent_calls_are_mcp_spans, what_is_left_out):
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
