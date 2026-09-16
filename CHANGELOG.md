# Changelog

Every release, newest first. While the version is `0.x`, a minor release may
change the API and a patch release does not; changes that break existing code
are listed under **Breaking** in the release that makes them.

## 0.1.0 — unreleased

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
