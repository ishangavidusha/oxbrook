# Lifespan

Code that runs when the server starts and when it stops: opening a connection
pool, loading a model, closing clients cleanly.

```python
from contextlib import asynccontextmanager
from oxbrook import App, Request

@asynccontextmanager
async def lifespan(app):
    model = load_model()              # once, before any worker starts
    yield {"model": model}

@asynccontextmanager
async def worker_lifespan(app):
    pool = await asyncpg.create_pool(         # once per worker loop, so
        DSN,                                  # size it as a share of the
        min_size=1,                           # whole process's budget
        max_size=app.per_worker(40),
    )
    try:
        yield {"db": pool}
    finally:
        await pool.close()

app = App(lifespan=lifespan, worker_lifespan=worker_lifespan)

@app.get("/users")
async def users(request: Request):
    rows = await request.state.db.fetch("select id, name from users")
    return [dict(row) for row in rows]
```

## Why there are two

Oxbrook runs one asyncio loop per worker thread, several of them in one process.
An asyncio connection pool, an `httpx.AsyncClient`, or a Redis client belongs to
the loop that created it and cannot be used from another. A single startup hook
would create one pool that only one loop could use, and the failure would show
up under load as intermittent "attached to a different loop" errors.

| hook | runs | use it for |
|---|---|---|
| `lifespan` | once, before the workers start and after they stop | configuration, loaded models, thread-safe clients, one-off work such as migrations |
| `worker_lifespan` | on every worker loop, before it takes a request and after its last | anything bound to an event loop: pools, async clients |

`lifespan` runs on a loop of its own that is not running while requests are
served. What it yields must not depend on that loop, and it must not start
background tasks: they would never run. Put those in `worker_lifespan`.

On the GIL build there is one worker loop, so `worker_lifespan` runs once. Code
written for both builds needs no change.

## Size per loop, budget per process

`worker_lifespan` runs once per loop, so **every number written in it is
multiplied** by however many loops the process runs — and that is decided by
the machine, not by the code. Four loops on a laptop, eight on a large server.

Connection pools are where this bites, because the multiplier is invisible and
the limit is at the other end:

```python
pool = await asyncpg.create_pool(DSN)   # asyncpg's default: min_size=max_size=10
```

`min_size` is eager, so that line opens **ten connections per loop the moment
the server starts** — eighty on an eight-loop host, against a PostgreSQL whose
default `max_connections` is 100. One process fits. The second one does not
start, and the failure arrives as `too many clients already` on a machine that
looked fine in testing.

Say what the process may use, and let `per_worker` divide it:

```python
max_size=app.per_worker(40)     # 40 for this process, whatever the machine
```

It rounds down, because exceeding a connection budget is the dangerous
direction, and it refuses a budget smaller than the number of loops rather than
handing back zero. `app.workers` is the count itself, if the arithmetic is not
a simple division.

The same reasoning applies to anything else opened per loop and limited
elsewhere: a client with a connection cap, a queue consumer with a licence
count, a file handle budget.

## Writing one

Either hook is an async context manager factory that takes the app —
a function decorated with `@asynccontextmanager` — or a plain async generator
function, which is treated as one:

```python
async def lifespan(app):
    client = make_client()
    yield {"client": client}
    client.close()
```

Code before `yield` is startup; code after is shutdown. Use `try`/`finally`
around the `yield` when shutdown must run even if the server stopped because of
an error.

## State

What the hooks yield is merged into `request.state`, a read-only mapping that
supports both `request.state.db` and `request.state["db"]`. `worker_lifespan`
also receives the app, so it can read `app.state`, which holds what `lifespan`
yielded.

`request.state` is read-only because every request on a worker loop shares it: a
value written by one request would be read by the next. A key yielded by both
hooks is an error at startup rather than a silent override.

`app.state` is empty before the server starts and again after it stops.

## Startup failures

If a hook raises during startup, the server does not start and the exception it
raised is the one that surfaces — from `app.run()`, or from entering a
`TestClient`. Worker loops that had already started run their shutdown first, so
a database that is down on the third worker does not leave the first two holding
connections.

Workers start one at a time, each after the previous one's `worker_lifespan`
has finished.

## Shutdown

On Ctrl-C or `SIGTERM` the server stops accepting, lets in-flight requests finish within
`shutdown_grace`, then stops each worker loop and runs its shutdown, then runs
`lifespan`'s. The server waits for worker shutdown to finish before running the
process shutdown and returning.

That wait is bounded by `shutdown_grace` again. Shutdown code is application
code and can hang; when it does, the server reports how many workers were still
shutting down and exits anyway.

Requests still queued for a worker when its loop stops are never started.
