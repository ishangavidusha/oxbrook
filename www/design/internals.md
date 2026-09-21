# Internals

How a request actually moves through the process. Useful if you are reading the
source, debugging something odd, or deciding whether this design suits your
workload.

## Layout

```
src/            Rust crate, built as the oxbrook._core extension module
  server.rs     tokio accept loop, hyper 1, HEAD/405/413, upgrade handshake
  tls.rs        rustls configuration and ALPN
  router.rs     matchit radix tree per method, path and query coercion
  queue.rs      bounded per-worker queue + wake pair
  wake.rs       both ends of that pair, made the way each platform allows
  worker.rs     one OS thread + one asyncio loop per worker, drain callback
  request.rs    the frozen Request handed to handlers
  responder.rs  reply channel, streaming bodies, client-disconnect signal
  cancel.rs     cancelling a handler whose client is gone
  websocket.rs  tokio-tungstenite bridge
python/oxbrook/  App, routing, pydantic, OpenAPI, topics, SSE, sockets, runtime
```

## The request path

1. A tokio thread accepts the connection, completes the TLS handshake if there
   is one, and hyper parses the request as HTTP/1.1 or HTTP/2.
2. If the app has a CORS policy, a preflight is answered here. The router
   matches the request against a radix tree built per HTTP method, and coerces
   path and query parameters into owned Rust values. A request that cannot
   succeed — bad path parameter, missing required query parameter, wrong
   method, body over the limit — is answered here, and no Python worker is ever
   woken. The body is collected up to `max_body`, unless the route streams it.
3. The request becomes a plain Rust struct and is pushed onto the bounded
   queue of the least-loaded worker, counting queued plus in-flight requests.
   A loop held by a handler that computes cannot drain, so its count stays
   high and new requests go elsewhere. If that worker is at its limit it tries
   the next; if every worker is full the answer is `503`.
4. If no wakeup is already in flight, one byte goes down the worker's wake
   pair: a socketpair on Unix, and on Windows, which has none, a loopback TCP
   connection to a listener that exists for exactly that one connection.
5. The worker's asyncio loop wakes through `add_reader`. A native drain
   callback clears the flag, pops **every** queued request, and schedules each
   handler in that one callback. On Windows the loop is a selector loop,
   because the proactor loop Python uses there by default has no `add_reader`.
6. The handler returns. A dict is serialized to JSON in Rust; a pydantic model
   through pydantic's own serializer. The bytes travel back to the waiting
   tokio task over a oneshot channel.

Two properties carry the design: Python is only ever touched from the worker's
own thread, and a burst of requests collapses into one wakeup.

A request under a static mount takes a branch after step 2: the file is resolved
and read on tokio's blocking pool, in one task, and answered without a worker.

A WebSocket upgrade takes a branch after step 2: its `Origin` is checked in Rust,
and a refused origin is answered `403` before an authorizer or handler is ever
queued.

On the way back out, CORS headers are added in Rust to every response, including
the ones the server produced itself in step 2.

## Connections

With HTTP/2 enabled, one connection handler serves both protocols: over TLS,
ALPN has already chosen; in cleartext, the first bytes are compared with the
HTTP/2 preface. hyper times out slow HTTP/1.1 headers itself, but has no idle
timeout for HTTP/2 or for those first bytes, so each such connection has a
watcher that looks every 5 s. A connection with nothing in flight and nothing
started for three looks gets a graceful shutdown, which is a `GOAWAY` on
HTTP/2; three looks later it is dropped. The watcher stops once the connection
turns out to be HTTP/1.1, and requests on HTTP/1.1 are not counted at all.

### Cancellation

A request on a route that allows it carries a small shared cell. If the tokio
task waiting for the reply ends before the first reply arrives — the client
closed, the stream was reset, the timeout fired — dropping it marks the cell
abandoned and queues it for the request's worker, with the same coalesced wake
as a request. The worker cancels the asyncio task on its own thread; a request
it pops that is already abandoned is dropped without being started. The tokio
side only flips an atomic and pushes an `Arc`: the task handle is stored in the
cell by the worker after `create_task` and removed by the `Responder` when it
releases the request, so no Python reference is ever dropped on a tokio thread.
If the abandon races the task's creation, the worker checks the flag again
after storing the task.

An HTTP/2 connection also counts its running handlers. The count is taken
before the body is read and released by the `Responder`, together with the
worker's concurrency slot, rather than when the stream ends: a client can
reset a stream while its handler keeps running, and a count that followed the
stream would let one connection start handlers without limit.

## Invariants

These are load-bearing. Breaking one silently destroys performance or
deadlocks.

**Tokio threads never attach to the interpreter.** No `Python::attach`, no
`Py::new`, no refcount touch. Worth 5.4x.

**Never block in native code while attached.** Any blocking wait is wrapped in
`py.detach`. Blocking while attached deadlocks free-threaded CPython at a
stop-the-world point — which is exactly how this framework's first startup
deadlock happened.

**Wakeups coalesce.** At most one wake byte in flight, and the flag is cleared
*before* draining. Clearing it after loses a racing push.

Anything a tokio thread needs to tell Python — a request, or a notification
that a client disconnected — goes through that same queue and that same byte.
A tokio thread that schedules work by calling into the interpreter can block
inside the event loop's self-pipe write, and on the GIL build a thread blocked
while attached stops every other thread in the process.

**Handlers are `async def`**, enforced at registration.

**Both Python builds work.** Free-threaded 3.14t is the primary target; the GIL
build runs a single worker loop.

**Agent exposure is opt-in.** Only `tool=True` routes reach `/mcp`.

**A resource limit is never released by garbage collection.** The concurrency
slot is freed by an explicit call, never by `Drop` and never by the cyclic
collector.

**Anything Rust validates, Rust canonicalises**, so Python's constructors
cannot fail on input Rust already accepted.

**Never return exception detail to a client.** Tracebacks go to the log;
`debug=True` is the only exception, and that flag is per server, never module
state.

## Concurrency model

One OS thread per worker, each running one asyncio loop, all in one process.
Handlers on different loops run Python in parallel on a free-threaded build.

The `Request` is frozen, so it needs no locking. Topics are shared across loops
and each subscription is bound to the loop that created it, which is how a
producer on another worker knows where to deliver.

## Streaming

A streaming response holds a guard that hyper drops when the connection ends.
The pump races the next message against that guard, so a disconnected client is
noticed without polling — including on a stream that is sitting idle on a quiet
topic, which has nothing to write and therefore nothing that would fail.

### Streaming request bodies

A route that takes a `BodyStream` is dispatched before its body is read. The
body is pumped from hyper into a queue by the connection's own request future,
so the pump cannot outlive the request: it starts only when the handler first
asks for a chunk, stops reading the socket while more than about a megabyte is
waiting, and is dropped when the response is sent. Chunks reach the handler
through the worker queue's wakeup path, the same one sockets use, so the tokio
thread never touches the interpreter.

The request timeout measures from the body's last progress. While the pump is
waiting on the client, only the pump's own idle timeout applies, so a client's
stall is a `408` and never races into a `504`.

## Startup and shutdown

Each worker thread runs `worker_lifespan` on its own loop before registering
its drain callback, and again after its loop stops, having first removed the
callback so nothing new starts during teardown. Workers start one at a time; if
one fails, the ones already running are stopped and joined before the error
surfaces. At shutdown — on `SIGINT` or `SIGTERM`, both handled in the accept loop — the
server waits for every worker thread, bounded by `shutdown_grace`, and only
then runs the process `lifespan`'s teardown. On Windows, where there is no
`SIGTERM`, Ctrl-Break and the console closing are handled the same way.

## Testing

Twenty-eight standalone scripts under `tests/`, each exiting non-zero on
failure, run against a real server on a real socket.

```bash
make verify        # free-threaded
make verify-gil    # GIL build
```

Where there is no `make`, `python tests/run.py` runs the same list.

`tests/verify.py` drives 5,000 concurrent requests, each carrying a unique
token, and asserts every response comes back with its own. That is the check
that matters for queue dispatch, where the plausible bug is a reply delivered to
the wrong request.

Each case in `tests/hardening.py` names a defect that was demonstrated against
a running server before it was fixed.

## Known gaps

- WebSockets are HTTP/1.1 only; there is no WebSocket over HTTP/2 (RFC 8441).
- A certificate is read at startup; a renewed one needs a restart.
- Middleware does not wrap socket handlers, only their authorizer.
- WebSocket origins are exact strings; there are no patterns for preview
  deployments.
- MCP is POST/JSON only: no streaming responses, no server-to-client channel,
  no resource subscriptions.
- On Windows a worker loop is a selector loop, so `select()` bounds it to 512
  sockets. That limits what handlers on one loop may hold open — outbound
  connections, mostly — not the connections the server itself accepts, which
  belong to Rust. Windows wheels are x86-64 only.
