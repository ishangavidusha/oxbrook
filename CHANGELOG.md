# Changelog

Every release, newest first, with what is merged but not yet released at the
top. While the version is `0.x`, a minor release may change the API and a patch
release does not; changes that break existing code are listed under
**Breaking** in the release that makes them.

## Unreleased

- **Response compression.** `App(compression=Compression())` compresses
  whole replies with brotli or gzip, as the client's `Accept-Encoding` asks,
  in Rust after the handler has answered. Streams, static files, images and
  bodies under `min_size` (1 KiB) are sent as they are; a reply opts out with
  `Cache-Control: no-transform`. Off unless set.
- **Health checks.** `App(health=Health())` serves `/livez` and `/readyz`.
  Liveness fails only when a worker loop has left requests waiting longer
  than `stall_after`, and is answered by the server even when every loop is
  stuck. Readiness runs `@health.check` functions on every worker loop
  against that loop's state, and fails while draining. `drain_delay` keeps
  serving, unready, for a few seconds after a stop, so a load balancer
  notices before the listener closes.
- **Metrics.** `App(metrics=Metrics())` serves the server's own metrics at
  `/metrics` in the Prometheus text format: requests and durations by route
  template and status, time waiting for a worker loop, queue depth per loop,
  connections, and requests shed or timed out. Measured in Rust and answered
  without a worker loop.
- **Rate limits.** `App(rate_limit=RateLimit("600/minute"))` limits every
  client across the app, and `rate_limit=` on a route or router adds a budget
  of its own; one `RateLimit` object is one budget. Counted by address (IPv6
  by /64) or by a header such as an API key, refilled continuously, and
  answered `429` with `Retry-After` in Rust before a worker loop sees the
  request. A route's limit applies to agents calling it as a tool too.
- **The client's address.** `request.client` is the caller's IP, and
  `App(trusted_proxies=...)` says which peers' `X-Forwarded-For` to believe:
  a list of networks, or how many proxies stand in front.
- **Configuration.** `oxbrook.Settings` is a pydantic model read from
  environment variables, with a prefix, secrets files, and every missing or
  invalid variable reported at once by name, never by value. Every
  `oxbrook run` option reads `OXBROOK_<OPTION>`, and `PORT` is followed;
  `--env-file` loads a `.env` for development and is re-read on reload.
  `oxbrook settings main:app` lists what an app needs and exits 1 if the
  environment does not provide it.
- **Work after the response.** `request.after_response(fn, *args, **kwargs)`
  runs `fn` on the handler's loop once the response is sent — awaited if
  async, on the blocking threadpool if not — in order, with failures logged.
  Nothing runs if the handler raised. The request keeps its place under
  `max_concurrency` until the work is done, so it is bounded and a graceful
  shutdown waits for it.
- **Fixed:** Ctrl-C stopped a server with several worker loops only after the
  whole `shutdown_grace`, and a Ctrl-C sent just as the server started was
  ignored. Both now stop at once.

## 0.3.0 — 2026-10-06

- **Authentication**, in `oxbrook.auth`. `auth=` on the app, a router, a route
  or a WebSocket declares who may call it, and the nearest declaration wins:
  `App(auth=...)` protects every route that does not say `auth=None`. Shipped
  schemes are `APIKey`, `Bearer`, `JWT`, `Basic` and `SessionAuth`; anything
  else is a class with one `async authenticate(request)` method. `a | b`
  accepts either, `.requires("scope")` adds a requirement, `optional(...)`
  lets anonymous callers through, and the handler gets the caller with
  `Depends(principal)`.
- The framework enforces the rules an app would otherwise get wrong by hand: a
  credential that is present and wrong is refused rather than falling through
  to the next scheme or to anonymous access; missing or wrong is `401` with a
  `WWW-Authenticate` challenge per scheme, known but not allowed is `403`; an
  API key reaches the app as its digest; a JWT's algorithm family, audience,
  issuer and expiry are always checked, and a refusal says "invalid" or
  "expired" and nothing finer; tokens in the query string are not looked for;
  Basic is refused over plain HTTP; no credential reaches a log line.
- **Authentication runs before the request body is read.** A refused upload is
  answered before it arrives, and the connection survives the refusal.
  Middleware outside the check finds the body unread: `request.body` raises
  there, and `await request.read()` reads it.
- One declaration covers every surface: a WebSocket's `auth=` is checked
  before the handshake, a tool call is checked against its route's
  declaration with the agent's headers, and the OpenAPI document lists
  `securitySchemes` and each operation's `security`.
- **OpenID Connect providers**: `OIDC(issuer, audience=...)` checks an identity
  provider's access tokens against the keys its discovery document names, with
  presets for Keycloak, Auth0, Entra ID, Okta, Cognito, Google and Firebase.
  Keys are fetched once per process and kept fresh; a rotated-in key is picked
  up by the first token that uses it, at most one fetch a minute; only the
  provider's public-key algorithms are accepted, each with its own kind of key;
  an unreachable provider is `503`, not `401`. `await scheme.load()` fetches the
  keys at startup.
- **Roles**, apart from scopes: `Principal.roles`, filled from a claim named by
  `roles_claim=` on `JWT` and `OIDC`, and checked by `.requires(roles=...)`. A
  role never satisfies a scope requirement, or the other way round. `claims=`
  on both requires claims to have given values.
- `JWT` and `OIDC` remember a verified token until it expires, so an RSA
  signature is checked once per token rather than once per request
  (`cache_size=`, `0` to turn it off).
- **Breaking: `SessionAuth` refuses cross-site requests.** A state-changing
  request authenticated by a session cookie must carry `Sec-Fetch-Site:
  same-origin`, an `Origin` naming the server or one of `trusted_origins`
  (the CORS origins by default), or, with neither header, an `X-CSRF-Token`
  equal to `SessionAuth.csrf_token(session)`; anything else is `403`. A script
  that posts with a session cookie and no `Origin` now needs the token.
  `csrf=False` turns it off.
- **WebSocket tickets**: `Tickets().issue(principal)` from an authenticated
  call, then `?ticket=...` on the socket URL. Single use, thirty seconds, read
  only from an upgrade: the one credential allowed in a URL.
- **Agents see only what they may call, and can log in by themselves.**
  `tools/list` leaves out tools whose declaration would refuse the caller.
  `App(mcp_auth=...)` gives `/mcp` a declaration of its own. With an `OIDC`
  provider, the app serves OAuth protected-resource metadata (RFC 9728) at
  `/.well-known/oauth-protected-resource/mcp`, and a `401` from `/mcp` points
  there, so an MCP client finds the provider, gets a token and retries.
- **Logging people in**: `OAuthLogin` runs the authorization-code flow with
  PKCE against an identity provider and leaves a session for `SessionAuth`.
  `routes(prefix="/auth")` gives `/auth/login`, `/auth/callback` and
  `/auth/logout`; `on_login(request, login)` receives a `Login` (the
  provider's subject, email, whether it was verified, the ID token's claims
  and the tokens) and returns what the session keeps. Presets for Google,
  Microsoft (any tenant, one tenant, work or personal accounts), GitHub and
  Keycloak, and `OAuthLogin(issuer, ...)` for any OpenID Connect provider.
  `state` and `nonce` are checked and single-use, the callback's `iss` is
  checked, `next` is followed only to a local path, the session is emptied
  before the login goes in, and logout is refused across sites. A failed
  login is `LoginFailed`.
- `TestClient.call_tool()` takes `headers=`.
- A response header can repeat: a list as a header's value sends it once per
  item.
- `Depends` works on WebSocket handlers. It raised `TypeError` on every
  connection.
- `TestClient.websocket()` takes `headers=`.

- **Every error is RFC 9457 problem details**, `application/problem+json`,
  whichever side answered it: an `HTTPError`, a validation failure, a missing
  route, a wrong method, a body over the limit, a server at capacity, a timeout,
  a handler that raised. A client parses one format for all of them. `HTTPError`
  takes `type=`, `title=`, `instance=` and `extensions=`; an exception handler
  can answer by raising one.
- **`request.locals`**, a dict for one request alone, where middleware leaves
  something for a handler or a dependency — the caller it authenticated, a
  request id. `request.state` is shared by every request on a worker loop, so
  it could never hold that. A tool call starts with a copy of the `/mcp`
  request's.

- **A dependency's teardown now sees how the request ended.** A generator
  dependency is finished the way a `with` block is: resumed after its `yield`
  if the handler returned, and with the handler's exception raised there if it
  did not. It used to be closed, which raises `GeneratorExit` at the `yield`
  every time — so `async with db.transaction(): yield db` rolled back on every
  successful request and nothing a handler wrote was kept, while the client was
  told it succeeded. And an exception raised during teardown, such as a commit
  that fails, was logged while the handler's `200` went out; it is now the
  request's outcome, and exception handlers apply to it.

- **One JSON encoding everywhere.** A handler's return, a `Reply`, an
  `HTTPError` detail, SSE, WebSockets, durable topics and MCP tool results all
  encode by pydantic's rules, which models already followed. A plain return of
  a `datetime`, `UUID`, `Decimal`, enum, database row or list of models was a
  `500` and now encodes; timestamps are ISO 8601, `Decimal` a string, NaN
  `null`.

- **A database guide**, with `examples/database/`: asyncpg with a pool per
  worker loop, a transaction per request, driver errors as responses,
  SQLAlchemy, and Alembic migrations that are safe for several replicas to run
  at once. Every part of it is run against PostgreSQL by the test suites, and
  `make verify` now requires PostgreSQL as it does Redis.

- **`app.per_worker(total)` and `app.workers`**, for sizing anything a
  `worker_lifespan` opens. That hook runs once per worker loop, so a pool
  written there is multiplied by a loop count the code never chose — and
  asyncpg's default pool, opened eagerly, is ten connections per loop, which is
  eighty on an eight-loop host against a PostgreSQL allowing a hundred.
  `per_worker` divides a process-wide budget across the loops, rounding down,
  and refuses a budget too small to share. The lifespan guide now shows this.

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
- **Breaking:** a value with no JSON form inside a `Reply`, an `HTTPError`
  detail or an MCP tool result is refused with a `500`, as a plain return
  always was, rather than sent as its `str()`. The same values inside a `Reply`
  also change form to match a plain return: a `datetime` is
  `"2026-09-23T10:00:00Z"` rather than `"2026-09-23 10:00:00+00:00"`, an enum
  is its value rather than `"Color.red"`, and NaN is `null` rather than the
  invalid bare `NaN`.
- **Breaking:** error bodies change shape. `{"detail": "..."}` is now
  `{"type": "about:blank", "title": "...", "status": ..., "detail": "..."}`
  with content type `application/problem+json`; a `422`'s list moves from
  `detail` to `errors`, and `RequestValidationError.errors` reads it from
  there. Errors that were `text/plain` — `404`, `405`, `413`, `426`, `500`,
  `503`, `504` — are problem details too. `HTTPError`'s `detail` must now be a
  string, with data in `extensions=`; with no `detail` given the body has
  none, where it used to repeat the status phrase. OpenAPI documents the
  `422` as `ValidationProblem`.
- **Breaking:** an exception raised in a dependency's teardown is now the
  request's outcome rather than a log line, and a dependency that yields twice
  is an error rather than a logged warning.
- Two handlers with the same name, such as `list_items` on two routers, no
  longer make the OpenAPI document invalid: each gets its method and path added
  to its `operationId`. A handler whose name is unique keeps it as before.

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
