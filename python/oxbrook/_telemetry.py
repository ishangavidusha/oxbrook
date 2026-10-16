"""OpenTelemetry tracing: a span per request, joined to the caller's trace.

    from oxbrook import App, Telemetry

    app = App(telemetry=Telemetry())

An adapter, and opt-in: without `telemetry=` nothing here runs and
OpenTelemetry is never imported. The server picks the request runner once,
when it is built, so an app without telemetry serves every request through
exactly the code it would otherwise. Only `opentelemetry-api` is needed
(`pip install 'oxbrook[otel]'`); the SDK, the sampler and the exporter are
the application's choice, configured the usual OpenTelemetry way.
"""

import functools
import importlib.util
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from . import _runtime


@dataclass(frozen=True, slots=True)
class Telemetry:
    """Trace requests with OpenTelemetry.

    Every request a handler answers gets a server span named after its route
    (`GET /items/{id}`), following OpenTelemetry's HTTP conventions: method,
    route, path, scheme, client address, user agent and status code. A `5xx`
    marks the span as an error, and an exception that reached the server
    is recorded on it. The span starts when the request arrived, not when
    a worker loop got to it, and `oxbrook.queue.wait` says how long it
    waited for one.

    An incoming `traceparent` (or whatever the configured propagator reads)
    makes the span a child of the caller's, and the span is current while
    the handler runs, so instrumented clients it calls (httpx, a database
    driver) join the same trace, and `trace.get_current_span()` reaches it
    to add attributes. Work added with `after_response` runs inside it too.

    `tracer_provider` defaults to the global one, looked up when the server
    starts, so configuring the SDK after creating the app is fine.
    `propagator` defaults to the global text-map propagator, W3C Trace
    Context unless configured otherwise.

    `exclude` lists routes not to trace, by the path they were registered
    with (`/items/{id}`). The readiness route is never traced: it is called
    by an orchestrator every few seconds, not by a client.

    A WebSocket's session is one span, `WebSocket /path`, for as long as the
    socket is open. When an authorizer or `auth=` decides its handshake,
    that decision is an HTTP request of its own, `GET /path`.

    Requests the server answers without a handler have no span: an unknown
    path, a static file, a probe, a CORS preflight, and requests shed at
    `max_concurrency` or refused by a rate limit. `Metrics` counts those.
    The query string is not recorded, since it often carries tokens.
    """

    tracer_provider: Any = None
    propagator: Any = None
    exclude: Iterable[str] = ()

    def __post_init__(self) -> None:
        if importlib.util.find_spec("opentelemetry") is None:
            raise ImportError(
                "Telemetry needs opentelemetry-api: pip install 'oxbrook[otel]'"
            )
        if isinstance(self.exclude, str):
            raise TypeError("exclude is a list of route paths, not a single string")
        exclude = tuple(self.exclude)
        for path in exclude:
            if not isinstance(path, str) or not path.startswith("/"):
                raise ValueError(f"exclude holds route paths starting with '/', not {path!r}")
        object.__setattr__(self, "exclude", exclude)


def _qualified(cls: type) -> str:
    """An exception's class as `error.type` wants it: fully qualified, with
    builtins left bare."""
    module = cls.__module__
    return cls.__qualname__ if module == "builtins" else f"{module}.{cls.__qualname__}"


class _Headers:
    """Lets a propagator read a request's headers without building a dict:
    `request.header` looks one up in Rust's own map."""

    @staticmethod
    def get(carrier: Any, key: str) -> list[str] | None:
        value = carrier.header(key)
        return None if value is None else [value]

    @staticmethod
    def keys(carrier: Any) -> list[str]:
        return list(carrier.headers)


class _Recorder:
    """The responder, as the traced runner hands it on: forwards every call
    and notes the status sent, when the response was handed over, and an
    exception the runtime logged."""

    __slots__ = ("_responder", "done_at", "error", "status")

    def __init__(self, responder: Any) -> None:
        self._responder = responder
        self.status: int | None = None
        self.done_at: int | None = None
        self.error: BaseException | None = None

    def send(self, status: int, *rest: Any) -> None:
        self._responder.send(status, *rest)
        self.status = status
        self.done_at = time.time_ns()

    def send_json(self, status: int, value: Any) -> None:
        # Recorded only once it was sent: a value Rust cannot encode raises
        # here, and the runtime answers with something else.
        self._responder.send_json(status, value)
        self.status = status
        self.done_at = time.time_ns()

    def start_stream(self, status: int, *rest: Any) -> None:
        self._responder.start_stream(status, *rest)
        self.status = status

    def end_stream(self) -> None:
        self._responder.end_stream()
        self.done_at = time.time_ns()

    def send_chunk(self, chunk: bytes) -> int:
        return self._responder.send_chunk(chunk)

    def notify_disconnect(self, callback: Any) -> None:
        self._responder.notify_disconnect(callback)

    def finish(self) -> None:
        self._responder.finish()

    def finish_or_keep(self) -> bool:
        return self._responder.finish_or_keep()

    def note_exception(self, exc: BaseException) -> None:
        self.error = exc


class Tracing:
    """One server's tracing: the runners Rust calls instead of the plain
    ones, and which route each handler belongs to."""

    def __init__(
        self, telemetry: Telemetry, *, tls: bool, request_timeout: float, never: Iterable[str]
    ) -> None:
        from opentelemetry import context, propagate, trace
        from opentelemetry.trace import SpanKind, Status, StatusCode

        from . import __version__

        self._context = context
        self._trace = trace
        self._kind = SpanKind.SERVER
        self._error = Status(StatusCode.ERROR)
        provider = telemetry.tracer_provider or trace.get_tracer_provider()
        self.tracer = provider.get_tracer("oxbrook", __version__)
        self._propagator = telemetry.propagator or propagate.get_global_textmap()
        self._scheme = "https" if tls else "http"
        self._timeout = request_timeout if request_timeout > 0 else None
        self._exclude = frozenset(telemetry.exclude) | frozenset(never)
        #: handler -> (span name, route template)
        self._routes: dict[Any, tuple[str, str]] = {}

    def adopt(self, specs: list[tuple]) -> list[tuple]:
        """Note each route's handler, and return the specs with every traced
        handler a distinct object, since the runner finds the route by it.

        The same function registered on two routes would otherwise be one
        key; a partial of it is a second object that calls the same thing.
        """
        adopted = []
        for spec in specs:
            method, path, handler, params, websocket, gate, *rest = spec
            if path not in self._exclude:
                if websocket:
                    handler = self._claim(handler, f"WebSocket {path}", path)
                    if gate is not None:
                        gate = self._claim(gate, f"GET {path}", path)
                else:
                    handler = self._claim(handler, f"{method} {path}", path)
            adopted.append((method, path, handler, params, websocket, gate, *rest))
        return adopted

    def _claim(self, handler: Any, name: str, route: str) -> Any:
        if handler in self._routes:
            handler = functools.partial(handler)
        self._routes[handler] = (name, route)
        return handler

    def _start(self, request: Any, name: str, route: str) -> tuple[Any, Any, int]:
        parent = self._propagator.extract(request, getter=_Headers)
        start = time.time_ns()
        wait = request._queue_wait
        attributes: dict[str, Any] = {
            "http.request.method": request.method,
            "http.route": route,
            "url.path": request.path,
            "url.scheme": self._scheme,
        }
        if wait is not None:
            start -= int(wait * 1e9)
            attributes["oxbrook.queue.wait"] = wait
        client = request.client
        if client is not None:
            attributes["client.address"] = client
        agent = request.header("user-agent")
        if agent is not None:
            attributes["user_agent.original"] = agent
        span = self.tracer.start_span(
            name, context=parent, kind=self._kind, attributes=attributes, start_time=start
        )
        token = self._context.attach(self._trace.set_span_in_context(span, parent))
        return span, token, start

    def _end(self, span: Any, recorder: _Recorder, start: int, cancelled: bool) -> None:
        status, error = recorder.status, recorder.error
        timed_out = False
        if status is None and cancelled:
            # Cancelled before answering: the request timed out and the
            # server answered 504, or the client left, or the server stopped.
            if self._timeout is not None and time.time_ns() - start >= self._timeout * 1e9:
                status, timed_out = 504, True
            else:
                span.set_attribute("oxbrook.cancelled", True)
        if status is not None:
            span.set_attribute("http.response.status_code", status)
        if error is not None:
            span.record_exception(error)
            span.set_attribute("error.type", _qualified(type(error)))
            span.set_status(self._error)
        elif timed_out:
            span.set_attribute("error.type", "timeout")
            span.set_status(self._error)
        elif status is not None and status >= 500:
            span.set_attribute("error.type", str(status))
            span.set_status(self._error)
        span.end(end_time=recorder.done_at)

    async def mcp(self, message: dict, session: str | None, version: str | None, call: Any):
        """One MCP request as a span, after OpenTelemetry's MCP conventions:
        `tools/call search`, its parent the context the agent put in
        `params._meta` when it sent one, linked to the HTTP request's span."""
        trace = self._trace
        method = message["method"]
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        tool = params.get("name") if method == "tools/call" else None
        tool = tool if isinstance(tool, str) else None
        attributes: dict[str, Any] = {"mcp.method.name": method, "network.transport": "tcp"}
        if tool is not None:
            attributes["gen_ai.tool.name"] = tool
            attributes["gen_ai.operation.name"] = "execute_tool"
        request_id = message.get("id")
        if request_id is not None:
            attributes["jsonrpc.request.id"] = str(request_id)
        if session is not None:
            attributes["mcp.session.id"] = session
        if isinstance(version, str):
            attributes["mcp.protocol.version"] = version

        parent = None
        links = ()
        meta = params.get("_meta")
        if isinstance(meta, dict):
            sent = self._propagator.extract(meta)
            if trace.get_current_span(sent).get_span_context().is_valid:
                parent = sent
                ambient = trace.get_current_span().get_span_context()
                if ambient.is_valid:
                    links = (trace.Link(ambient),)

        name = method if tool is None else f"{method} {tool}"
        with self.tracer.start_as_current_span(
            name, context=parent, kind=self._kind, attributes=attributes, links=links
        ) as span:
            reply = await call
            if isinstance(reply, dict):
                error = reply.get("error")
                result = reply.get("result")
                if isinstance(error, dict):
                    code = str(error.get("code"))
                    span.set_attribute("rpc.response.status_code", code)
                    span.set_attribute("error.type", code)
                    span.set_status(self._error)
                elif tool is not None and isinstance(result, dict) and result.get("isError"):
                    span.set_attribute("error.type", "tool_error")
                    span.set_status(self._error)
            return reply

    def record(self, exc: BaseException) -> None:
        """An exception a tool raised, on the span of the call that ran it."""
        self._trace.get_current_span().record_exception(exc)

    async def run_handler(self, handler, request, responder, params, debug):
        route = self._routes.get(handler)
        if route is None:
            return await _runtime.run_handler(handler, request, responder, params, debug)
        name, template = route
        span, token, start = self._start(request, name, template)
        recorder = _Recorder(responder)
        cancelled = False
        try:
            await _runtime.run_handler(handler, request, recorder, params, debug)
        except BaseException:
            cancelled = True
            raise
        finally:
            self._end(span, recorder, start, cancelled)
            self._context.detach(token)

    async def run_websocket(self, handler, request, responder, socket, params):
        route = self._routes.get(handler)
        if route is None:
            return await _runtime.run_websocket(handler, request, responder, socket, params)
        name, template = route
        span, token, start = self._start(request, name, template)
        recorder = _Recorder(responder)
        try:
            await _runtime.run_websocket(handler, request, recorder, socket, params)
        finally:
            self._end(span, recorder, start, False)
            self._context.detach(token)
