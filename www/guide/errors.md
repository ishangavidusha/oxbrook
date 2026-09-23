# Errors and limits

## Raising an HTTP error

```python
from oxbrook import HTTPError

@app.get("/users/{user_id}")
async def get_user(_: Request, user_id: int):
    user = await find(user_id)
    if user is None:
        raise HTTPError(404, "no such user")
    return user
```

The client gets a `404` with an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)
problem-details body:

```http
HTTP/1.1 404 Not Found
Content-Type: application/problem+json

{"type": "about:blank", "title": "Not Found", "status": 404, "detail": "no such user"}
```

`headers` adds response headers:

```python
raise HTTPError(401, headers={"www-authenticate": "Bearer"})
```

It works from a handler, a dependency, middleware, an exception handler or a
WebSocket authorizer. The status must be 4xx or 5xx; return a `Reply` or
`Response` for anything else.

`detail` is sent to the client, so write it for the client. It is the one
exception text that is returned, and it is a string: data meant for a program
goes in `extensions`.

## The error shape

Every error Oxbrook writes is problem details, whichever side answered it:
an `HTTPError`, a validation failure, a route that does not exist, a body over
the limit, a server at capacity, a handler that raised. A client parses one
format for all of them, and generic tooling that understands
`application/problem+json` reads them without being told about this API.

| member | meaning |
|---|---|
| `type` | a URI naming the kind of problem; `about:blank` when the status says it all |
| `title` | a short summary; the status's name when `type` is `about:blank` |
| `status` | the HTTP status, repeated for clients that lose the status line |
| `detail` | this occurrence, for a person; absent when there is nothing to add |
| `instance` | a URI for this occurrence, when there is one |

Anything else is an extension member. A problem a client should branch on gets
a `type` of its own, and its data goes in `extensions`:

```python
raise HTTPError(
    409,
    "a note with that title already exists",
    type="https://api.example.com/problems/duplicate-title",
    extensions={"existing_id": 7},
)
```

```json
{"type": "https://api.example.com/problems/duplicate-title", "title": "Conflict",
 "status": 409, "detail": "a note with that title already exists", "existing_id": 7}
```

Extensions cannot reuse the five standard names, and are encoded like any
other [response value](responses.md#how-values-become-json).

The MCP endpoint is the one exception: it answers in JSON-RPC, as the protocol
requires.

## Exception handlers

Map your own exceptions to responses:

```python
class NotFound(Exception):
    pass

@app.exception_handler(NotFound)
async def not_found(request, exc):
    raise HTTPError(404, str(exc))
```

A handler applies to the class and its subclasses, and the most specific
registered class wins. Raising `HTTPError` from it is the short way to answer
with problem details. It can also return a response instead: what it returns
goes through the normal response path, so return a `Reply` or `Response` to set
the status, since a plain dict is a `200`.

Exceptions are mapped before middleware sees the result. Middleware — the access
log included — sees a reply with the mapped status, never the exception. An
exception raised by middleware itself is mapped on its way out, so an
`HTTPError(403)` from an inner router's middleware is still a reply to the
middleware outside it.

Register a handler for `HTTPError` to change the shape of every error raised
that way, or for `RequestValidationError` to change the shape of a `422`
produced by a body. Errors answered in Rust, before any Python runs, keep the
problem-details shape.

```python
from oxbrook import RequestValidationError

@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    return Reply({"errors": exc.errors}, status=400)
```

Exception handlers must be `async def`, and each class can have one. A handler
that raises anything but `HTTPError` is a `500`, logged like any other failure.

Exceptions in a WebSocket handler, or in an SSE source after the stream has
started, are not mapped: the response has already begun, so there is no status
left to change. They are logged.

## A handler that raises

With no exception handler for it, the client gets a `500` problem with no
`detail`. The traceback goes to the log. A handler registered for `Exception`
replaces this and takes over the logging.

Exception messages routinely carry connection strings, file paths, query
fragments and user data. Returning them to whoever triggered the exception is
how that information leaks.

During development:

```python
app = App(debug=True)   # the exception text becomes the 500's detail
```

`debug` is per server, never module state. Two apps in one process do not share
it.

## Validation

A parameter or body that fails validation is a `422` in one shape, whatever
failed. Each failure is an entry in `errors`, in pydantic's format:

```json
{"type": "about:blank", "title": "Unprocessable Content", "status": 422,
 "errors": [{"type": "int_parsing", "loc": ["query", "limit"], "msg": "..."}]}
```

Path and query failures are produced in Rust, before a worker is woken. Body
failures come from pydantic, on the worker.

## Limits

| limit | default | what happens past it |
|---|---|---|
| `max_body` | 16 MiB | `413`, without buffering the body; on a streaming route, as the chunks arrive |
| `max_concurrency` | 1024 per worker | `503` with `Retry-After: 1` |
| `max_connections` | 2048 | the listener stops accepting; the OS backlog holds the wait |
| `request_timeout` | 30s | `504`, the handler is [cancelled](#when-the-client-leaves), and the connection is freed; on a streaming route, counted from the last progress, and `408` when the client is the one that stalled |
| form `max_parts` | 1000 | `413` |
| header read timeout | 15s | the connection is dropped |
| `shutdown_grace` | 10s | in-flight requests are abandoned |

```python
app.run(
    max_body=64 * 1024 * 1024,
    max_concurrency=256,
    max_connections=4096,
    request_timeout=0,        # disabled
)
```

## Timeouts and streams

`request_timeout` measures the wait for a handler's *first* response. It does
not cut short a stream that has already started, so SSE and WebSocket
connections are unaffected by it and can stay open for as long as they like.

A route that [streams its request body](forms.md#streaming-a-body) measures from
the last chunk that moved instead, so a long upload that keeps making progress
is never cut off. A client that stops sending gets `408`; a handler that stops
reading or never answers gets `504`.

It costs about 5-8% of hello-world throughput. Set it to `0` for a service
whose handlers are legitimately long-running.

## When the client leaves

A handler whose client disconnects before it has answered is cancelled:
`asyncio.CancelledError` is raised at its next `await`. The same happens when
`request_timeout` answers `504` for it, and when an HTTP/2 client resets the
stream. Its answer could no longer reach anyone, and a handler left running
would keep its share of the worker's `max_concurrency`, so clients that sent
requests and hung up in a loop could fill every worker and have everyone else
refused with `503`. A request still waiting in the queue when its client leaves
is dropped without running, unless its route opts out as shown below.

Cancellation is ordinary asyncio cancellation:

- `finally` blocks run, and so does [dependency](dependencies.md) teardown,
  including an `await` inside it.
- `CancelledError` is not an `Exception`, so middleware `except Exception`
  clauses and exception handlers do not see it, and nothing is logged.
- A database transaction interrupted by it is rolled back by the driver, as it
  would be by any other error.

Two ways to keep work from being cut short:

```python
import asyncio

@app.post("/orders")
async def place_order(request: Request, body: Order):
    order = await charge_and_record(body)          # may be cancelled
    await asyncio.shield(send_receipt(order))      # finishes even if cancelled
    return order


@app.post("/webhooks/payment", cancel_on_disconnect=False)
async def payment_webhook(request: Request):
    # The sender may time out and retry; this still runs to the end.
    await process(request.body)
    return None
```

`asyncio.shield` protects one step: the handler itself still stops there, but
the shielded work completes. `cancel_on_disconnect=False`, on any route
decorator of an app or a router, lets the whole handler finish. Use it for work
whose side effects must not stop halfway and are not wrapped in a transaction.

Only the wait for the *first* response is covered. Once a handler has answered,
there is nothing left to cancel; a stream that has started stops through its own
disconnect handling, as described for [SSE](../streams/sse.md) and
[WebSockets](../streams/websockets.md). A task a handler starts with
`asyncio.create_task` is its own task and is not cancelled with it.

## Shutdown

Ctrl-C stops accepting new connections and waits up to `shutdown_grace` for
in-flight requests to finish before stopping anyway. Open streams and sockets
are closed.
