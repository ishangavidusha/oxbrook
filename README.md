# Oxbrook

A fast Python REST framework with a Rust core, built-in reactive streams, and
agent-native interfaces.

```python
# main.py
from oxbrook import App, Request

app = App()

@app.get("/")
async def hello(_: Request):
    return {"hello": "world"}
```

```bash
oxbrook run main:app --reload
```

**Status: working, not API-stable.** Routing and routers, typed parameters,
pydantic bodies, forms and file uploads, streaming request bodies, static files,
HTTPS and HTTP/2, backpressure,
CORS, OpenAPI 3.1, in-process topics, Server-Sent Events, WebSocket, durable
topics over Redis, an MCP endpoint, middleware, exception handlers,
dependencies, lifespans, sessions, logging and a test client all work.

This is a personal project, not a supported product. Every feature is covered
by the test suite on both CPython builds, but nothing is API-stable and there
is no deprecation policy.

## Documentation

The full documentation lives in [`www/`](www/) and builds into a site.

```bash
make docs-serve     # http://127.0.0.1:8000, live reload
make docs           # build into site/
```

| | |
|---|---|
| [Install](www/install.md) | requirements, build, first app |
| [Routing](www/guide/routing.md) · [Query parameters](www/guide/parameters.md) · [Request bodies](www/guide/bodies.md) | the request path |
| [Topics](www/streams/topics.md) · [SSE](www/streams/sse.md) · [WebSocket](www/streams/websockets.md) · [Durable topics](www/streams/durable.md) | streaming |
| [OpenAPI](www/openapi.md) · [Agents](www/agents.md) | generated interfaces |
| [Running a server](www/running.md) | workers, backpressure, limits, containers |
| [Why Oxbrook is built this way](www/design/why.md) · [Internals](www/design/internals.md) · [Performance](www/design/performance.md) | design and measurements |

The API reference is generated from the docstrings and needs the built site.

## What it does

**REST.** A radix-tree router per HTTP method, with path and query parameters
typed by the handler's annotations and coerced in Rust — `str`, `int`, `float`,
`bool`, `uuid.UUID`, `datetime.date`, `datetime.datetime`, and `list[T]`. A
request that cannot succeed is answered before a Python worker is woken.
Pydantic models bind request and response bodies, and form fields with
`Form()`; multipart uploads are parsed in Rust, and a `BodyStream` reads a body
larger than memory with backpressure. Routers with prefixes and
their own middleware, exception handlers, dependency injection with teardown,
startup and shutdown hooks per process and per worker loop, and signed cookie
sessions.

**Streams.** Named topics with fan-out and four backpressure policies.
Subscribers on every worker loop in the process receive every message, which is
what free-threaded CPython makes possible. `SSE(...)` and `@app.websocket(...)`
hand a topic to a client. `App(redis_url=...)` makes a topic durable:
persisted, replayable, shared across processes, with consumer groups for work
that must not be lost.

**Agents.** `tool=True` on a route also exposes it over the Model Context
Protocol at `/mcp`, with the name, description, argument schema and output
schema taken from the handler that already exists. Opt-in, so a route is not
agent-callable until someone decides it should be.

## Performance

| hello world, free-threaded 3.14 | req/s |
|---|---:|
| oxbrook | 181,397 |
| granian, raw ASGI | 135,846 |
| granian + FastAPI | 29,674 |
| uvicorn + FastAPI | 12,411 |

Apple Silicon, 10 cores, 64 connections. A CPU-bound handler gains 3.15x on
four worker loops on the free-threaded build, against 1.03x on the GIL build.

Hello world measures dispatch, not a framework. See
[performance](www/design/performance.md) for the method, the machine, the cost
of each feature, and what has not been measured.

## Build

Requires Rust, [uv](https://docs.astral.sh/uv/), Docker for services, and
[oha](https://github.com/hatoo/oha) for benchmarks. Nothing Oxbrook depends on
is installed on the host.

```bash
make venvs          # .venv (free-threaded 3.14t) and .venv-gil (standard 3.14)
make build          # maturin develop --release into both
make run            # examples/hello.py
make verify         # all eighteen test suites
make bench          # hello-world comparison
make bench-cpu      # CPU-bound handler scaling
make sweep          # handler cost against worker-loop count
```

Add `-gil` to any of the last four for the standard build. Services run in
containers:

```bash
make up             # redis, for durable topics
make stack          # two nodes against one redis
make down
```

`tests/durable.py` prints SKIP without Redis and still passes, so start the
container before trusting a green run.

## Layout

```
src/            Rust crate, built as the oxbrook._core extension module
  server.rs     tokio accept loop, hyper 1, HEAD/405/413, upgrade handshake
  router.rs     matchit radix tree per method, path and query coercion
  queue.rs      bounded per-worker queue + socketpair wakeup
  worker.rs     one OS thread + one asyncio loop per worker, drain callback
  request.rs    frozen Request pyclass
  responder.rs  reply channel, streaming bodies, disconnect signal
  websocket.rs  tokio-tungstenite bridge
python/oxbrook/  App, routing, pydantic, OpenAPI, topics, SSE, sockets, runtime
examples/       hello world, live feed, durable queue, agent service, cluster node
bench/          hello-world, CPU-parallelism and handler-cost sweeps
tests/          eighteen standalone scripts, each exiting non-zero on failure
www/            documentation sources
```

A tokio thread parses a request, matches it against a radix tree, coerces its
parameters, and pushes a plain Rust struct onto a worker's bounded queue
without ever touching the interpreter. It writes one byte to a socketpair only
if no wakeup is already in flight, so a burst of requests collapses into a
single wakeup. See [internals](www/design/internals.md).

## License

MIT. See [LICENSE](LICENSE).

## Known gaps

- Middleware does not wrap socket handlers, only their authorizer.
- MCP is POST/JSON only: no streaming responses, no server-to-client channel,
  no resource subscriptions.
- Multipart forms are held in memory; streaming multipart parsing is not
  available, only streaming the raw body.
- The WebSocket throughput ceiling is unmeasured: a Python load generator
  saturates first.
- The worker cap of 8 is a guard against an absurd probe result, not a measured
  ceiling; it has not been tested on a large homogeneous machine.
