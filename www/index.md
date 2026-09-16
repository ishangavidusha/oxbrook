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

!!! warning "Not a supported product"

    Oxbrook is a personal project. Every feature documented here works and is
    covered by the test suite on both CPython builds, but nothing is
    API-stable and there is no deprecation policy. Expect the API to change.

## What it is

Three things, in one process:

**A REST framework.** A radix-tree router with typed path and query parameters
coerced in Rust, pydantic request and response bodies, forms and file uploads,
streaming request bodies, static files, routers, middleware, exception handlers, dependency
injection, per-worker lifespans, CORS, sessions, HTTPS and HTTP/2, and OpenAPI
3.1 generated from the same route metadata the router uses.

**A stream engine.** Named topics with fan-out, backpressure policies, and
subscribers spread across every worker loop in the process. Server-Sent Events
and WebSocket are first-class ways to hand one to a client. Add a Redis URL and
a topic becomes durable: persisted, replayable, and shared across processes,
with consumer groups for work that must not be lost.

**An agent interface.** Mark a route `tool=True` and it also becomes a tool an
agent can call over the Model Context Protocol, at `/mcp`. Its name,
description, argument schema and output schema all come from the handler that
already exists. Nothing is declared twice.

## Why it is fast

The HTTP layer is Rust — tokio, hyper and PyO3 — and the developer-facing API is
Python. A tokio thread parses a request, routes it, coerces its parameters and
pushes it onto a worker's queue without ever touching the interpreter. Python is
woken once per burst rather than once per request.

| hello world, free-threaded 3.14 | req/s |
|---|---:|
| Oxbrook | 181,397 |
| granian, raw ASGI | 135,846 |
| granian + FastAPI | 29,674 |
| uvicorn + FastAPI | 12,411 |

Apple Silicon, 10 cores. The [performance page](design/performance.md) has the
method, the machine, and the caveats — including that hello world measures
dispatch rather than a framework, and that the FastAPI rows were taken in an
earlier run than the Oxbrook one.

Free-threaded CPython is the primary target, and not only for throughput: it is
what lets several event loops share one process, which is what makes an
in-memory topic reach subscribers on all of them. The GIL build works too, with
a single worker loop.

## Where to start

- [Install](install.md) — get a server running.
- [Routing](guide/routing.md) — paths, methods, typed parameters.
- [Routers](guide/routers.md) and [Lifespan](guide/lifespan.md) — an app in
  more than one module, with resources that live as long as the server.
- [Topics](streams/topics.md) — fan-out, backpressure, cross-loop delivery.
- [Agents](agents.md) — one handler, served to humans and to models.
- [Why Oxbrook is built this way](design/why.md) — the design decisions and their cost.
