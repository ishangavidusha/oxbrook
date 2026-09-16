# Oxbrook

A fast Python REST framework with a Rust core, built-in reactive streams, and
agent-native interfaces.

```bash
pip install oxbrook
```

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

**Documentation: [ishangavidusha.github.io/oxbrook](https://ishangavidusha.github.io/oxbrook/)**

## Status

**Alpha.** Every feature below is covered by the test suites on both CPython
3.14 builds, but the API is not settled. While the version is `0.x`, a minor
release (`0.2.0`) may change the API and a patch release (`0.1.1`) does not;
every change that breaks code is listed in the
[changelog](https://github.com/ishangavidusha/oxbrook/blob/main/CHANGELOG.md).
This is a personal project, not a supported product.

Requires CPython 3.14, free-threaded (`python3.14t`) or standard, on Linux or
macOS. Wheels are published for both builds on x86-64 and ARM; anywhere else
pip builds from source, which needs a Rust toolchain. Windows is not
supported.

## What it does

**REST.** A radix-tree router per HTTP method, with path and query parameters
typed by the handler's annotations and coerced in Rust. A request that cannot
succeed is answered before a Python worker is woken. Pydantic request and
response bodies, forms and multipart uploads parsed in Rust, request bodies
larger than memory with backpressure, static files, routers, middleware,
exception handlers, dependency injection with teardown, lifespans, signed
cookie sessions, CORS, and OpenAPI 3.1 from the same route metadata the router
uses.

**Serving.** HTTP/1.1 and HTTP/2, HTTPS, bounded concurrency with `503` rather
than an unbounded backlog, request timeouts, handlers cancelled when their
client leaves, graceful shutdown on `SIGTERM`, and an `oxbrook` command with
reload for development.

**Streams.** Named topics with fan-out and four backpressure policies.
Subscribers on every worker loop in the process receive every message, which
free-threaded CPython makes possible. `SSE(...)` and `@app.websocket(...)` hand
a topic to a client. `App(redis_url=...)` makes a topic durable: persisted,
replayable, shared across processes, with consumer groups for work that must
not be lost.

**Agents.** `tool=True` on a route also exposes it over the Model Context
Protocol at `/mcp`, with its name, description and schemas taken from the
handler that already exists. Opt-in, so a route is not agent-callable until
someone decides it should be.

## Performance

| hello world, free-threaded 3.14 | req/s |
|---|---:|
| oxbrook | 181,397 |
| granian, raw ASGI | 135,846 |
| granian + FastAPI | 29,674 |
| uvicorn + FastAPI | 12,411 |

Apple Silicon, 10 cores, 64 connections. A CPU-bound handler gains 3.15x on
four worker loops on the free-threaded build, against 1.03x on the GIL build.

Hello world measures dispatch, not a framework. The
[performance page](https://ishangavidusha.github.io/oxbrook/design/performance/)
has the method, the machine, what each feature costs, and what has not been
measured.

## How it works

A tokio thread parses a request, matches it against a radix tree, coerces its
parameters, and pushes a plain Rust struct onto a worker's bounded queue
without touching the interpreter. It writes one byte to a socketpair only if no
wakeup is already in flight, so a burst of requests collapses into a single
wakeup, and each worker's asyncio loop schedules every queued handler in one
callback. See
[internals](https://ishangavidusha.github.io/oxbrook/design/internals/).

## Contributing

Building from source, running the suites and the project's conventions are in
[CONTRIBUTING.md](https://github.com/ishangavidusha/oxbrook/blob/main/CONTRIBUTING.md).

## License

MIT.
