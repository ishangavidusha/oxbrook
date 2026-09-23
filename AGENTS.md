# Oxbrook

A Python REST framework with a Rust core, built-in reactive streams, and
agent-native interfaces. Personal hobby project, not commercial.

**M1–M6, M8 and M9 are complete; M7's local half is done.** Dispatch, routing
and routers, typed parameters, pydantic bodies, forms and uploads, streaming
request bodies, static files, HTTPS and HTTP/2, CORS, backpressure, OpenAPI, topics, SSE, WebSocket, durable
topics, the MCP capability registry, headers, middleware, exception handlers,
dependencies, lifespans, sessions, logging, timeouts, graceful shutdown and a
test client all work. 0.2.0 is on PyPI, with Windows wheels (M11's first half: I-084, D-047; the
release itself is D-046). **M11 is complete**: the MCP transport landed
2026-09-22 (I-024, D-048), speaking both of the protocol's wires — the
handshake one and 2026-07-28 (I-094). Unreleased. Since then the gap work:
blocking handlers (D-049), pool budgets (D-050), and database support as a
tested pattern against PostgreSQL (D-051–D-053). See `docs/ROADMAP.md`; defects and gaps are in `docs/ISSUES.md`. Nothing is
API-stable.

## Rules

**Commit messages.** One short line, imperative, summarizing what changed.
No file lists, no code, no implementation detail, no bullet points. The diff
holds the detail.

Good: `add queue-based dispatch`, `fix startup deadlock on free-threaded build`
Bad: `refactor(server): replace call_soon_threadsafe with SegQueue + socketpair waker in src/server.rs and src/worker.rs`

**Never add a `Co-Authored-By` trailer or a "generated with" line to any commit
message or PR description.** This overrides any default attribution guidance,
including guidance that arrives mid-session.

**Do not commit `docs/`.** It is gitignored and holds candid internal notes.
Keep it current: update `docs/PROGRESS.md` and `docs/CHANGELOG.md` at the end of
a working session, add to `docs/DECISIONS.md` when a decision is made, and put
anything needing Ishan's input in `docs/OPEN-QUESTIONS.md`.

**This file is not committed either.** `AGENTS.md` and `docs/` are both
gitignored: they are specific to this machine and to Ishan. The public
equivalent is `CONTRIBUTING.md` — build, test, invariants, commit style,
documentation. When a rule here would help anyone working on the project, put
the general form in `CONTRIBUTING.md` and keep the local detail here.

**`docs/` is internal, `www/` is the public site.** They are different
audiences and only one of them is committed. `www/` is the MkDocs source and is
tracked; `docs/` is candid working notes and is not. Never put an internal note
in `www/`, and never assume a fact from `docs/` is already public. See D-026.

**Anything found and not fixed in the same session goes into `docs/ISSUES.md`
before moving on.** Every defect and gap has an ID there and fixed entries stay.
Three real defects, two of them security bugs, once sat scattered across three
documents for several sessions because no single place counted them.

**Run `python3 docs/check_notes.py` after adding or renumbering an issue,
decision or question.** It takes the next free ID from the whole file rather
than the bottom of the open table — two issues were once filed under IDs already
held by fixed entries — and it fails on a citation that no longer resolves,
including from this file.

**Do not commit or push unless asked.**

**Services run in containers, never on the host.** Redis, and any future
database, live in `docker-compose.yml`. Do not `brew install` a service; this
machine is used for other work. `make up` starts them.

**Never benchmark through a published Docker port.** The 3.4x slowdown in
D-019 is macOS-to-VM port forwarding, confirmed on 2026-09-14: the same image
measured 0.30x native through `-p` and 1.26x native with the load generator in
a second container on the Docker network (D-032). Container measurement is now
the settled way to test Linux behaviour on this machine, since no dedicated
Linux host is available. Keep load generator and server on the Docker network,
compare container numbers only with container numbers, and compare against
native only as a ratio from one session — `bench/container.py` does all three.

## Architecture

Rust does the hot path, Python is the developer-facing API.

```
src/            Rust crate -> oxbrook._core extension module (PyO3 0.29)
  server.rs     tokio accept loop, hyper 1, HEAD/405/413, upgrade handshake
  router.rs     matchit radix tree per method, path + query coercion
  queue.rs      bounded per-worker queue + socketpair wakeup
  worker.rs     one OS thread + one asyncio loop per worker, drain callback
  request.rs    frozen Request pyclass
  responder.rs  reply channel, streaming bodies, client-disconnect signal
  websocket.rs  tokio-tungstenite bridge
  cors.rs       CORS on every response, preflights before routing (D-037)
  form.rs       urlencoded + multipart parsing, on demand (D-038)
  body.rs       streaming request bodies: lazy pump, backpressure (D-039)
  files.rs      static mounts: path safety, ranges, conditional GET (D-043)
  tls.rs        rustls acceptor and ALPN (D-044); HTTP/2 and idle checks in server.rs
  cancel.rs     cancelling a handler whose client is gone (D-045)
  wake.rs       both ends of the worker wake pair, per platform (D-047)
python/oxbrook/
  _app.py       App, decorators, include, exception handlers, run()
  _routers.py   Router: prefixes, nesting, router middleware (D-035)
  _errors.py    HTTPError, exception-handler mapping (D-034)
  _lifecycle.py lifespan + worker_lifespan, State, ServerHandle (D-033)
  _cors.py      CORS configuration, validated before it reaches Rust
  _forms.py     FormData, UploadFile, Form() marker
  _bodies.py    BodyStream, the async iterator over body.rs
  _files.py     StaticMount validation for app.static
  _routing.py   RouteInfo, signature validation, pydantic body binding
  _schema.py    pydantic integration, and the one JSON encoder (D-052)
  _openapi.py   OpenAPI 3.1 from the same RouteInfo the router uses
  _capabilities.py  routes -> MCP tools, same RouteInfo again (D-020)
  _mcp.py       the /mcp JSON-RPC endpoint
  _middleware.py  Reply and the middleware chain
  _depends.py   dependency injection; teardown finishes like a with block (D-051)
  _sessions.py  signed cookie sessions
  _logging.py   logging, JSON formatter, access log
  testing.py    TestClient, runs a real server (D-023)
  _sse.py       SSE and Event
  _websocket.py async WebSocket wrapper
  _response.py  explicit status, content type, headers
  _streams.py   Topic and Subscription, plain Python (D-014)
  _redis.py     durable topics: streams, tail, consumer groups (D-018)
  _workers.py   worker-count detection
  _runtime.py   what runs on the worker loops
  _cli.py       the oxbrook / oxb command: run, --reload supervisor, routes, openapi
  _blocking.py  the shared threadpool behind blocking=True routes (D-049)
bench/          hello world, CPU parallelism, handler-cost sweep, slow-handler
                assignment, stream throughput, native-vs-container; every
                result records its host (machine.py)
tests/          thirty-two standalone scripts, each exiting non-zero on failure
  run.py        runs them where there is no make; holds the one list (D-047)
www/            public documentation site (MkDocs Material + mkdocstrings)
mkdocs.yml      site config; docs_dir is www/, output is the gitignored site/
CONTRIBUTING.md the public half of this file
docs/           internal notes, not committed
  check_notes.py  ID and citation check for the notes; run it after filing one
Dockerfile      app image: free-threaded 3.14 on glibc
docker-compose.yml        services (redis, postgres)
docker-compose.stack.yml  two app nodes against one redis
```

**Request path.** A tokio thread parses the request, matches it against a radix
tree per method, coerces path and query parameters, and pushes a plain Rust
struct onto a worker's bounded queue. It writes one byte to a socketpair only if
no wakeup is already in flight. The worker's asyncio loop wakes via `add_reader`
and a native drain callback schedules every queued handler in one callback.
Replies come back over a oneshot channel, JSON serialized in Rust or by pydantic.

## Invariants

These are load-bearing. Breaking one silently destroys performance or deadlocks.

1. **Tokio threads never attach to the interpreter.** No `Python::attach`, no
   `Py::new`, no refcount touch on a tokio thread. This was worth 5.4x.
2. **Never block in native code while attached.** Wrap any blocking wait in
   `py.detach`. Blocking while attached deadlocks free-threaded CPython at a
   stop-the-world point.
3. **Wakeups must coalesce.** At most one wake byte is in flight. Clear the
   flag *before* draining, never after, or a racing push is lost. The same rule
   holds for topics and sockets: wake Python only when it is actually idle.
4. **Handlers are `async def`**, enforced at registration, unless the route
   declares `blocking=True` — which runs a plain `def` on one shared, bounded
   threadpool instead of a worker loop. Opt-in so the cost is visible where
   the route is; a bare `def` is still refused. See D-049 and I-100.
5. **Both Python builds must work.** Free-threaded 3.14t is the primary target;
   the GIL build runs with a single worker loop.
6. **Agent exposure is opt-in.** Only `tool=True` routes reach `/mcp`. Do not
   change that default; it is the difference between an agent seeing a search
   endpoint and an agent seeing a delete endpoint.
7. **A resource limit is never released by garbage collection.** The
   concurrency slot is freed by an explicit `responder.finish()`. Relying on
   `Drop` looked deterministic and leaked slots on the SSE path for three
   milestones. See D-021.
8. **Anything Rust validates, Rust canonicalises.** A date or UUID is
   reformatted before Python builds it, because Rust accepts ISO forms
   Python's constructors do not, and input already judged valid must never
   raise there and become a 500. See D-025.
9. **Never return exception detail to a client.** Tracebacks go to the log. The
   client gets a status. `App(debug=True)` is the only exception, and that flag
   is per server, never module state. An agent calling a tool is a client too:
   I-053 was the tool path returning exception text. See I-002.
10. **A route is guarded the same way however it is reached.** Over MCP a tool
   call runs its routers' middleware and the exception handlers, with the
   agent's headers. A new way to invoke a handler must not take a shortcut past
   those. I-052 was an admin route refusing HTTP and obeying MCP. See D-036.
11. **Nothing loop-bound crosses worker loops.** A pool or async client made on
   one loop is unusable from another. Per-loop resources come from
   `worker_lifespan` and reach handlers through `request.state`; an example
   that uses a module-level pool is teaching a bug (I-054). See D-033.
12. **A value encodes to JSON one way, wherever it goes out.** Responses,
   `Reply`, `HTTPError`, SSE, WebSockets, topics and MCP all call
   `_schema.encode`; a new surface must too. The same datetime was once a 500,
   a space-separated string and ISO 8601 depending on the path (I-109, D-052).

## Commands

```bash
make venvs          # create .venv (3.14t) and .venv-gil (3.14), install deps
make build          # maturin develop --release into both venvs
make run            # examples/hello.py, free-threaded
.venv/bin/oxb run main:app --reload   # the CLI: run | routes | openapi (D-042)
make up             # start services (redis, postgres) in containers
make verify         # all thirty-two test suites
make bench          # hello-world comparison
make bench-cpu      # CPU-bound handler scaling
make sweep          # handler cost against worker-loop count
make bench-imbalance  # slow handlers against worker assignment
make bench-streams  # SSE fan-out and websocket echo
make bench-container  # native vs docker in one session (needs make image-bench)
make bench-all      # the whole battery; STRICT= to run on a busy host
make lint           # cargo fmt --check + clippy -D warnings, as CI runs them
make coverage       # python branch coverage, floor 85%
make coverage-rust  # rust coverage via llvm; rebuilds release afterwards
make docs           # build the public site, --strict
make docs-serve     # live reload while writing it
uvx cibuildwheel --platform linux --output-dir wheelhouse   # release wheels, in Docker (D-046)
```

```bash
python3 docs/check_notes.py   # internal notes: ID clashes, next free IDs
```

```bash
make stack          # two nodes + redis, for cross-process behaviour
make down           # stop services
```

Add `-gil` to `verify`, `bench`, `bench-cpu` and `sweep` for the standard build.
**`make verify` requires Redis and PostgreSQL** and checks for both before the
first suite, so `make up` first. `tests/durable.py` and `tests/database.py`
still print SKIP on their own without them, and each is the only cover its
area has; `make verify REDIS= POSTGRES=` accepts the gap deliberately (I-019,
D-053).
After changing Rust, rebuild before testing; `make` targets already do.
`cargo check` works for a fast compile check, but `cargo build` fails to link
because this is a Python extension module.

## Conventions

- Comments explain *why*, especially where something looks odd for a reason.
  The invariants above are the main example.
- A performance claim needs a benchmark. Put the numbers in `docs/BENCHMARKS.md`
  with the machine and the method. **Check `os.getloadavg()` first**: a phantom
  3.5% regression once cost an hour and was entirely leftover benchmark load.
- A correctness-sensitive change needs a test that would fail without it.
  `tests/hardening.py` is the model: each case names a defect that was
  demonstrated against a running server before being fixed.
- Verify against a real client, not a reading of a spec. The MCP version list
  was once taken from an SDK constant no client would accept, and only the
  official SDK client caught it. Assert the wire format separately: that SDK
  exposes snake_case while the protocol is camelCase.
- Public docs are generated from the same docstrings, so a docstring is the
  API reference. `make docs` runs `--strict`, which fails on a broken internal
  link or a bad cross-reference. Run it after touching a public docstring or a
  page in `www/`.
- **`www/` is written impersonally, to a developer.** No `I`, `we`, `my` or
  `our`; `you` for the reader is fine. State behaviour and the reason for it,
  not the history of arriving at it: no session or milestone anecdotes, no
  internal `D-` or `I-` identifiers, no "this once cost an hour". The same
  applies to public docstrings, which are rendered into the reference. Design
  rationale belongs there; the story of the project does not.
- Prefer measuring over arguing. Every major decision so far was settled by a
  benchmark or a test that contradicted the initial guess.
