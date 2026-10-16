<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ishangavidusha/oxbrook/main/www/assets/logo-dark.svg">
    <img alt="Oxbrook" src="https://raw.githubusercontent.com/ishangavidusha/oxbrook/main/www/assets/logo.svg" width="120">
  </picture>
</p>

<h1 align="center">Oxbrook</h1>

<p align="center">
  A fast Python REST framework with a Rust core, built-in reactive streams,<br>
  and agent-native interfaces.
</p>

<p align="center">
  <a href="https://pypi.org/project/oxbrook/"><img alt="PyPI" src="https://img.shields.io/pypi/v/oxbrook?color=C2410C"></a>
  <a href="https://pypi.org/project/oxbrook/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/oxbrook?color=0E7490"></a>
  <a href="https://github.com/ishangavidusha/oxbrook/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/ishangavidusha/oxbrook/actions/workflows/tests.yml/badge.svg?branch=main"></a>
  <a href="https://ishangavidusha.github.io/oxbrook/"><img alt="Documentation" src="https://img.shields.io/badge/docs-oxbrook-0E7490"></a>
  <a href="https://github.com/ishangavidusha/oxbrook/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/pypi/l/oxbrook?color=555"></a>
</p>

<p align="center">
  <a href="https://ishangavidusha.github.io/oxbrook/"><b>Documentation</b></a> ·
  <a href="https://ishangavidusha.github.io/oxbrook/install/">Install</a> ·
  <a href="https://ishangavidusha.github.io/oxbrook/design/performance/">Performance</a> ·
  <a href="https://github.com/ishangavidusha/oxbrook/blob/main/CHANGELOG.md">Changelog</a> ·
  <a href="https://github.com/ishangavidusha/oxbrook/blob/main/CONTRIBUTING.md">Contributing</a>
</p>

---

## Quickstart

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

Or start from a project with an app, tests and instructions for coding
assistants already in place:

```bash
oxbrook new my-api
```

## Highlights

- **Fast.** Requests are parsed, routed and validated in Rust, and handlers run
  on several asyncio loops at once on free-threaded Python.
- **Familiar.** Typed path and query parameters, pydantic bodies, routers,
  middleware, dependencies and OpenAPI 3.1, the way FastAPI taught them.
- **Streams built in.** Topics with fan-out and backpressure, delivered over
  Server-Sent Events and WebSockets, durable on Redis when they must be.
- **Agent-native.** `tool=True` exposes a route to AI agents over the Model
  Context Protocol, guarded exactly as it is over HTTP.
- **Production pieces included.** Authentication, health probes, Prometheus
  metrics, OpenTelemetry tracing, rate limits, compression and graceful
  shutdown, without plugins.

## What it does

**REST**

- A radix-tree router per HTTP method; path and query parameters typed by the
  handler's annotations and coerced in Rust. A request that cannot succeed is
  answered before a Python worker is woken.
- Pydantic request and response bodies, forms and multipart uploads parsed in
  Rust, request bodies larger than memory with backpressure, static files.
- Routers, middleware, exception handlers, dependency injection with teardown,
  lifespans, signed cookie sessions, CORS, response compression.
- PostgreSQL and MongoDB as tested patterns, a client per worker loop and a
  transaction per request.
- Health probes, Prometheus metrics, OpenTelemetry tracing (opt-in, free
  when off), rate limits, typed settings from the environment, work after the
  response.
- OpenAPI 3.1 from the same route metadata the router uses.

**Authentication**

- One `auth=` declaration — API keys, bearer tokens, JWT, OpenID Connect
  providers, Basic, sessions or a scheme of your own — enforced before the body
  is read, on HTTP, WebSockets and agent tool calls alike.
- Logging people in through Google, Microsoft, GitHub or any OpenID Connect
  provider.

**Serving**

- HTTP/1.1 and HTTP/2, HTTPS.
- Bounded concurrency with `503` rather than an unbounded backlog, request
  timeouts, handlers cancelled when their client leaves.
- Graceful shutdown on `SIGTERM`, and an `oxbrook` command with reload for
  development.

**Streams**

- Named topics with fan-out and four backpressure policies. Subscribers on
  every worker loop in the process receive every message, which free-threaded
  CPython makes possible.
- `SSE(...)` and `@app.websocket(...)` hand a topic to a client.
- `App(redis_url=...)` makes a topic durable: persisted, replayable, shared
  across processes, with consumer groups for work that must not be lost.

**Agents**

- `tool=True` on a route also exposes it over the Model Context Protocol at
  `/mcp`, with its name, description and schemas taken from the handler that
  already exists.
- Opt-in, so a route is not agent-callable until someone decides it should be.

## Performance

| hello world, free-threaded 3.14 | req/s |
|---|---:|
| **oxbrook** | **181,397** |
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

## With a coding assistant

Oxbrook looks like FastAPI but differs in ways an assistant will not guess.
Point it at [`llms.txt`](https://ishangavidusha.github.io/oxbrook/llms.txt),
or the whole documentation in one file,
[`llms-full.txt`](https://ishangavidusha.github.io/oxbrook/llms-full.txt).
Every documentation page has a Markdown copy, and a project made by
`oxbrook new` carries an `AGENTS.md` that points there.

## Status

**Alpha.** Every feature is covered by the test suites on both CPython 3.14
builds, but the API is not settled. While the version is `0.x`, a minor
release may change the API and a patch release does not; every change that
breaks code is listed in the
[changelog](https://github.com/ishangavidusha/oxbrook/blob/main/CHANGELOG.md).
This is a personal project, not a supported product.

Requires CPython 3.14, free-threaded (`python3.14t`) or standard, on Linux,
macOS or Windows. Wheels are published for both builds: x86-64 and ARM on
Linux and macOS, and x86-64 on Windows. Anywhere else pip builds from source,
which needs a Rust toolchain. Windows is supported so that the framework can be
tried and developed against on the machine in front of you; what is measured,
and what the performance page reports, is Linux and macOS.

## Contributing

Building from source, running the suites and the project's conventions are in
[CONTRIBUTING.md](https://github.com/ishangavidusha/oxbrook/blob/main/CONTRIBUTING.md).

## License

[MIT](https://github.com/ishangavidusha/oxbrook/blob/main/LICENSE).
