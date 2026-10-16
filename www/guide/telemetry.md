# Telemetry

```python
from oxbrook import App, Telemetry

app = App(telemetry=Telemetry())
```

Every request a handler answers becomes an OpenTelemetry server span, joined
to the caller's trace when it sends a `traceparent`. Spans go wherever the
application's OpenTelemetry SDK sends them: Jaeger, Tempo, Honeycomb, an
OpenTelemetry collector.

Tracing is opt-in, and an app without it pays nothing. The server picks how
it runs requests once, when it starts. Without `telemetry=`, requests run
through exactly the code they would if tracing did not exist, and
OpenTelemetry is never imported.

## Install

Oxbrook needs only the OpenTelemetry API. The SDK and an exporter are the
application's choice, as with any instrumented library:

```bash
pip install 'oxbrook[otel]' opentelemetry-sdk opentelemetry-exporter-otlp
```

Configure the SDK the usual way, before the server starts. The provider is
looked up when the server starts, not when the app is created, so the order
of the two does not matter:

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider(resource=Resource.create({"service.name": "orders"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
```

To keep Oxbrook's spans apart from the global provider, pass a provider of
their own: `Telemetry(tracer_provider=provider)`.

## What a span holds

A span is named after its route, `GET /items/{item_id}`, never after the raw
path, and follows OpenTelemetry's HTTP conventions:

| attribute | |
|---|---|
| `http.request.method` | `GET` |
| `http.route` | `/items/{item_id}` |
| `url.path` | `/items/7` |
| `url.scheme` | `http` or `https` |
| `http.response.status_code` | `200` |
| `client.address` | the client, through [trusted proxies](rate-limits.md#behind-a-proxy) |
| `user_agent.original` | the `User-Agent` header |
| `error.type` | on failure: the exception's class, the status code, or `timeout` |
| `oxbrook.queue.wait` | seconds the request waited for a worker loop |

The span starts when the request arrived at the server, not when a worker
loop got to it. `oxbrook.queue.wait` is that difference: a span whose
duration is mostly queue wait is a loop that was busy, not a slow handler.
The span ends when the response is handed over, so work added with
[`after_response`](after-response.md) is not counted in it, though it still
runs inside the span's context.

A `5xx` marks the span as an error. A `4xx` does not, since that is the
client's error rather than the server's. An exception that reaches the
server is recorded on the span as an event with its traceback; the client
still gets only a status. A request that passes `request_timeout` is
recorded as `504`, with `error.type` set to `timeout`.

The query string is not recorded, since it often carries tokens.

## Inside a handler

The request's span is current while its handler runs. Clients instrumented
with OpenTelemetry, such as httpx, database drivers and Redis, start their spans
as its children and send the trace on to the next service. Use
`get_current_span` to add to the span:

```python
from opentelemetry import trace

tracer = trace.get_tracer("orders")

@app.post("/orders")
async def place(request: Request, order: Order):
    trace.get_current_span().set_attribute("order.items", len(order.items))
    with tracer.start_as_current_span("reserve stock"):
        await reserve(request.state.pool, order)
    return {"placed": True}
```

Log records can carry the trace and span ids with
`opentelemetry-instrumentation-logging`, which reads the same current span.

## Agents

Each MCP request an agent makes is a span of its own, after OpenTelemetry's
MCP conventions: `tools/call search`, with `mcp.method.name`,
`gen_ai.tool.name`, `mcp.session.id` and `jsonrpc.request.id`. If the agent
sent a trace context in the message's `params._meta`, as instrumented MCP
clients do, the span joins the agent's trace and links to the HTTP request
that carried it. Otherwise the span is a child of that request's span. A
tool that fails sets `error.type` to `tool_error`, and its exception is
recorded on the span.

## Streams and sockets

An [SSE](../streams/sse.md) stream is one span, from the request to the end of
the stream.

A WebSocket's session is one span, `WebSocket /path`, for as long as the
socket is open. When an authorizer or `auth=` decides the handshake, the
decision is an HTTP request of its own, `GET /path`, with status `101` or
the refusal.

## What has no span

Requests the server answers without running a handler: an unknown path, a
static file, a health probe, a metrics scrape, a CORS preflight, a path or
query parameter of the wrong type, and requests shed at `max_concurrency` or refused by a
rate limit. [Metrics](metrics.md) count all of them. The readiness route is
never traced, since an orchestrator calls it every few seconds. Leave out
other routes by the path they were registered with:

```python
App(telemetry=Telemetry(exclude=["/internal/ping"]))
```

## Cost

On a hello-world route, where per-request overhead is most visible, with
four worker loops on an Apple M4:

| | added per request |
|---|---|
| no `telemetry=` | none: the same throughput as a build without the adapter |
| `Telemetry()`, API only, no SDK configured | about 2.6 µs |
| the SDK sampling nothing | about 6 µs |
| the SDK recording every span, batch export | about 27 µs |

Nearly all of the last figure is the SDK creating, validating and queueing
the span. A sampler that keeps a fraction of traces, such as
`ParentBased(TraceIdRatioBased(0.1))`, is the usual way to trace a busy
service.
