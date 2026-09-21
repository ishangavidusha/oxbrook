# Changelog

Every release, newest first, with what is merged but not yet released at the
top. While the version is `0.x`, a minor release may change the API and a patch
release does not; changes that break existing code are listed under
**Breaking** in the release that makes them.

## 0.2.0 — 2026-09-21

- **Windows**, on both interpreter builds, with x86-64 wheels: `pip install`
  and run, with no WSL2 and no container. Ctrl-C, Ctrl-Break and the console
  closing all shut the server down gracefully, and `oxbrook run --reload`
  stops its server with Ctrl-Break so a reload still drains and still runs
  lifespan teardown. Windows is a development platform here: nothing on the
  site is measured there.
- A request body refused for being over `max_body` is read away briefly before
  the `413` goes out, so the client reads the answer rather than losing it to
  a connection reset.
- Static mounts refuse names that Windows resolves to something other than a
  file under the mount: a drive-relative segment such as `C:passwd`, a device
  name such as `CON` or `NUL`, and names ending in a dot or a space. Refused
  on every platform, so a mount answers the same everywhere.

## 0.1.0 — 2026-09-16

The first release.

**REST**

- `App` with `get`, `post`, `put`, `patch`, `delete` and `route` decorators;
  handlers are `async def`.
- A radix-tree router per method. Path and query parameters typed by
  annotations — `str`, `int`, `float`, `bool`, `uuid.UUID`, `datetime.date`,
  `datetime.datetime`, `list[T]` — coerced in Rust, with `422` before a worker
  is woken.
- Pydantic request and response bodies; forms with `Form()`; multipart uploads
  with `UploadFile`; `BodyStream` for bodies larger than memory.
- `Router` with prefixes, nesting and its own middleware.
- Middleware, `HTTPError`, exception handlers, `Depends` with teardown.
- `lifespan` per process and `worker_lifespan` per worker loop.
- Signed cookie sessions, CORS, static files with ranges and conditional
  requests, OpenAPI 3.1 and a documentation page.

**Serving**

- HTTP/1.1 and HTTP/2; HTTPS with `tls_cert` and `tls_key`.
- Worker loops on free-threaded CPython 3.14, one loop on the standard build.
- `max_concurrency`, `max_connections`, `max_body`, `max_message`,
  `request_timeout` and `shutdown_grace`; graceful shutdown on `SIGINT` and
  `SIGTERM`.
- Handlers are cancelled when their client leaves before they answer, unless
  the route passes `cancel_on_disconnect=False`.
- The `oxbrook` command, also installed as `oxb`: `run` with `--reload`,
  `routes`, `openapi`.
- Logging through the standard `logging` module, with a JSON formatter and an
  access log.

**Streams**

- `Topic` with fan-out across worker loops and four backpressure policies.
- `SSE` responses and `@app.websocket` endpoints, with an authorizer that runs
  before the handshake and an origin check on by default.
- Durable topics over Redis with `App(redis_url=...)`: persisted messages,
  `history()`, shared across processes, and consumer groups.

**Agents**

- `tool=True` exposes a route over the Model Context Protocol at `/mcp`.

**Testing**

- `oxbrook.testing.TestClient`, which runs the real server, over HTTP or HTTPS.
