# Changelog

Every release, newest first, with what is merged but not yet released at the
top. While the version is `0.x`, a minor release may change the API and a patch
release does not; changes that break existing code are listed under
**Breaking** in the release that makes them.

## Unreleased

- **`blocking=True` on a route** runs a plain `def` handler on a threadpool
  instead of its worker loop. A handler that takes time without awaiting — a
  sync database driver, `requests`, `boto3`, Pillow — freezes every other
  request its loop is serving; measured, eight such handlers on four loops made
  an unrelated request wait 1829 ms, and 4 ms with the route declared blocking.
  The pool is one per process, bounded, sized with `app.run(blocking_threads=…)`,
  and starts no thread until a blocking handler runs. A blocking handler cannot
  be cancelled, so a client that leaves frees the loop but not the thread.
  A `def` handler on a route that does not declare it is still refused, and the
  error now names the fix.

- **The MCP endpoint speaks both of the protocol's transports.** A client that
  probes with `server/discover` gets the 2026-07-28 wire: self-contained
  requests, no handshake, no session, and `subscriptions/listen` answered with
  a stream. A client that does not falls back to the handshake wire, where
  `initialize` issues an `MCP-Session-Id`, a `GET` on `/mcp` opens a
  server-to-client stream and a `DELETE` ends the session. Both on the one
  endpoint; neither needs configuring.
- **An agent can follow a topic** rather than polling it, over either wire. The
  same declaration already serving browsers and WebSocket clients now feeds
  agents.
- `Origin` is validated on every request to `/mcp`, and a foreign origin is
  refused with `403` unless CORS already allows it.
- **Breaking:** the handshake wire now requires a session, so a client that
  posted `tools/call` to `/mcp` without calling `initialize` first receives
  `400`. `TestClient.mcp()` and `TestClient.call_tool()` handle this
  themselves and are unchanged; `TestClient.mcp()` also takes `headers=` now.

## 0.2.0 — 2026-09-21

- **Windows**, on both interpreter builds, with x86-64 wheels: `pip install`
  and run, with no WSL2 and no container. Ctrl-C, Ctrl-Break and the console
  closing all shut the server down gracefully, and `oxbrook run --reload`
  stops its server with Ctrl-Break so a reload still drains and still runs
  lifespan teardown. Windows is a development platform here: nothing on the
  site is measured there.
- A refusal decided before the request body is read — no such route, wrong
  method, a path parameter that will not coerce, a body over `max_body`, no
  capacity — now reads the body away briefly before answering. The client gets
  the answer rather than a connection reset, and the connection survives to
  carry the next request.
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
