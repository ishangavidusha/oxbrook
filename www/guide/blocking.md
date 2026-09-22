# Blocking work

A worker loop is one thread serving many requests at once. That works because
handlers `await`: every `await` is a point where the loop parks one request and
runs another. A handler that takes time *without* awaiting breaks the
arrangement — it does not just slow itself down, it freezes every other request
that loop was serving.

This is ordinary synchronous Python, not anything exotic:

```python
rows = cursor.execute("select * from orders").fetchall()  # a sync driver
reply = requests.get("https://api.example.com/rates")     # requests
s3.upload_file(path, bucket, key)                         # boto3
thumbnail = Image.open(upload).resize((200, 200))         # Pillow
```

None of those can be awaited. Run one directly in a handler and the loop stops
until it returns, however many other requests were in flight.

## Declare the route blocking

```python
@app.get("/report", blocking=True)
def report(request: Request):
    return cursor.execute("select * from orders").fetchall()
```

`blocking=True` runs the handler on a threadpool instead of the worker loop,
and it is what lets the handler be a plain `def`. The loop stays free the whole
time, so everything else it is serving keeps moving.

Everything else about the route is unchanged: path and query parameters, a
pydantic body, dependencies, middleware, exception handlers and `tool=True` all
work exactly as they do on an `async def` handler.

## Why it is opt-in

A blocking handler holds a thread from a bounded pool, and a handler on the
loop does not. Declaring it at the route keeps that cost where it can be seen —
in the source, and in `oxb routes`:

```
METHOD  PATH      HANDLER          NOTES
GET     /report   reports.report   blocking
```

A `def` handler without `blocking=True` is refused at registration, so nothing
ends up on a thread by accident, and nothing blocks a loop by accident either.

## The pool

One pool for the process, shared by every blocking route, and bounded. The
default is `min(32, cpu + 4)` threads — what CPython would give a *single*
event loop, given to all of them together rather than to each. Size it when the
work is mostly waiting on a network or a disk:

```python
app.run(blocking_threads=64)
```

The bound is the point. An unbounded pool only moves where a server falls over.
When every thread is busy, further blocking calls queue while the worker loops
carry on serving everything else.

On free-threaded builds those threads run in parallel, with no interpreter lock
between them. That is the build this is designed for.

## What it does not do

**A blocking handler cannot be cancelled.** A thread inside a blocking call
cannot be interrupted, so a client that disconnects frees the worker loop but
not the thread, and the work runs to completion. `cancel_on_disconnect` has
nothing to act on. Prefer an async client for anything long enough that
abandoning it matters.

**It does not help a blocking call inside an `async def` handler.** Marking the
route is a statement about the whole handler. A single blocking call inside an
otherwise async handler still stops the loop, and the fix there is
`asyncio.to_thread`, or an async client for whatever is being called.

**WebSocket handlers cannot be blocking.** A socket handler lives as long as
its connection, so it would hold a thread for that long too. Do the blocking
part in a route, or off the socket.

## Prefer async where there is a choice

A blocking handler is bounded by the pool; an async one is bounded by the
worker loops, which are far cheaper. Where a library offers both, the async one
scales better:

| blocking | async |
|---|---|
| `psycopg` (sync mode) | `asyncpg`, `psycopg` async |
| `requests` | `httpx.AsyncClient` |
| `redis` (sync) | `redis.asyncio` |

`blocking=True` is for the libraries that give no choice — and a good deal of
the ecosystem gives no choice.
