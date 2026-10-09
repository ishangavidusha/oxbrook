# Metrics

```python
from oxbrook import App, Metrics

app = App(metrics=Metrics())
```

`GET /metrics` serves the server's own counters, gauges and histograms in the
Prometheus text format, which Prometheus scrapes directly and an OpenTelemetry
collector reads with its Prometheus receiver.

They are measured in the server, not in Python. The numbers that explain a slow
service — how long requests waited for a worker loop, how many are queued,
how many connections are held, how many were turned away — are only visible
there, and the endpoint answers without a worker loop, so it still answers when
the loops are overwhelmed.

## What is measured

| metric | type | labels | |
|---|---|---|---|
| `oxbrook_requests_total` | counter | `method`, `route`, `status` | requests answered |
| `oxbrook_request_duration_seconds` | histogram | `method`, `route` | arrival to response headers |
| `oxbrook_queue_wait_seconds` | histogram | `loop` | time waiting for a worker loop |
| `oxbrook_loop_requests` | gauge | `loop` | queued or in flight |
| `oxbrook_loop_queued` | gauge | `loop` | waiting, not yet taken by the loop |
| `oxbrook_loop_wake_pending_seconds` | gauge | `loop` | how long a loop has left requests waiting |
| `oxbrook_connections` | gauge | | connections held open |
| `oxbrook_connections_limit` | gauge | | `max_connections` |
| `oxbrook_requests_shed_total` | counter | | `503` because every loop was at `max_concurrency` |
| `oxbrook_request_timeouts_total` | counter | | `504` because a handler passed `request_timeout` |
| `oxbrook_draining` | gauge | | `1` while [draining](health.md#draining), with `Health` |

`route` is the route's template — `/users/{user_id}` — never the path a client
sent. A label holding the raw path would start a new time series for every id,
and enough of them take the scraper down. Requests that matched no route, the
`404`s and `405`s, have an empty `method` and `route`. A static mount is
labelled with its pattern.

Durations run from the request's arrival to its response headers. For a
streamed reply such as [SSE](../streams/sse.md), that is the time to the first
byte, not the life of the stream.

A `503` the handler returned itself is counted under its route and status, and
not as shedding: `oxbrook_requests_shed_total` counts only the requests the
server turned away.

[Health probes](health.md), CORS preflights and the scrapes themselves are not
counted as requests. They are the server describing itself, at a rate a
scraper sets.

## Reading them

- **Queue wait rising while request duration stays flat**: the loops are
  saturated, and requests spend their time waiting for one. More worker loops,
  more instances, or less work per request.
- **`oxbrook_loop_wake_pending_seconds` above zero for long on one loop**: that
  loop is blocked by something. The [liveness probe](health.md#liveness) fails
  on the same signal.
- **Shed requests rising**: `max_concurrency` is the limit being hit. See
  [backpressure](../running.md#backpressure).
- **Connections near the limit**: idle keep-alive connections, or slow
  clients, are holding the slots.

## Exposure

The endpoint is public, is not in the [OpenAPI document](../openapi.md), and is
never an agent tool. It reveals route templates, request counts and load. That
is usually fine on a private network and worth keeping off a public one: leave
`/metrics` unrouted at the proxy, as you would a database port.

## Your own metrics

`Metrics` covers the server. The application's own numbers — orders placed,
cache hits — belong to a client library such as `prometheus_client`, served on
a route of their own:

```python
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

orders = Counter("orders_placed_total", "Orders placed.")

@app.get("/metrics/app", auth=None)
async def app_metrics(_: Request):
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)
```

Point the scraper at both paths.
