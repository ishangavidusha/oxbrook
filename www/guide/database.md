# Databases

Oxbrook has no database layer of its own. It runs any async driver, and gives
it three things: a place to open a pool per worker loop, a dependency whose
teardown commits or rolls back, and one JSON encoding for what a query returns.
This page puts them together for PostgreSQL. A complete service, with its
migrations, is in `examples/database/`.

```python
from contextlib import asynccontextmanager

import asyncpg
from oxbrook import App, Depends, Reply, Request

@asynccontextmanager
async def worker_lifespan(app):
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=app.per_worker(20))
    try:
        yield {"pool": pool}
    finally:
        await pool.close()

app = App(worker_lifespan=worker_lifespan)

async def connection(request):
    async with request.state.pool.acquire() as db:
        yield db

async def transaction(db = Depends(connection)):
    async with db.transaction():
        yield db

@app.exception_handler(asyncpg.UniqueViolationError)
async def duplicate(request, exc):
    return Reply({"detail": "already exists"}, status=409)

@app.get("/notes")
async def notes(_: Request, db = Depends(connection)):
    return await db.fetch("select id, title, created_at from notes")

@app.post("/notes")
async def create(_: Request, note: NoteIn, db = Depends(transaction)):
    row = await db.fetchrow(
        "insert into notes (title) values ($1) returning id, title, created_at",
        note.title,
    )
    return Reply(row, status=201)
```

## Choosing a driver

The driver has to be async, and on the free-threaded build it has to publish a
free-threaded wheel. A C extension without one either fails to install or, if
it builds from source without declaring support, turns the GIL back on for the
whole process when it is imported, with a `RuntimeWarning`.
`sys._is_gil_enabled()` after your imports says which you got.

| database | driver | free-threaded |
|---|---|---|
| PostgreSQL | `asyncpg` | yes |
| PostgreSQL | `psycopg[binary]` | no wheel, so it does not install on 3.14t |
| PostgreSQL | `psycopg` (pure Python) | installs; needs a system `libpq` at run time |
| MySQL | `asyncmy` | yes |
| SQLite | `aiosqlite` | pure Python |
| MongoDB | `pymongo` (its async API) | yes |
| any of the above | `sqlalchemy[asyncio]` | yes, including `greenlet` |

The picture changes as projects publish wheels. For PostgreSQL today, use
asyncpg. It is the driver the examples on this site use.

## One pool per worker loop

An asyncio pool belongs to the loop that created it, and Oxbrook runs several
loops. So the pool is opened in `worker_lifespan`, once per loop, and reaches
handlers through `request.state`. A pool created at import time is shared by
every loop and fails intermittently under load, with "another operation is in
progress" or "attached to a different loop". Most requests in a burst fail
while a light test passes.

Every number in `worker_lifespan` is multiplied by the loop count, and
connections are what the database limits. `app.per_worker(20)` says this
process may hold twenty connections, whatever the machine. `min_size=1` keeps
startup from opening a full pool on every loop at once. See
[lifespan](lifespan.md#size-per-loop-budget-per-process) for the arithmetic.

## A transaction per request

The `transaction` dependency above is the whole pattern. Teardown runs after
the handler and before the response is sent, and it sees how the handler
finished:

| the handler | the transaction |
|---|---|
| returned | committed |
| raised, including `HTTPError` | rolled back |
| was cancelled because the client left | rolled back |
| returned, but the commit failed | rolled back, and the request is an error |

The last row matters for constraints checked at commit, such as a deferred
unique constraint, and for serialization failures under `REPEATABLE READ` or
`SERIALIZABLE`. The client is told the write failed, because it did. See
[dependencies](dependencies.md#teardown-sees-the-outcome) for the rules
underneath.

Reads that need no transaction can take `connection` directly. Both
dependencies are cached per request, so a handler that asks for both gets one
connection.

## Driver errors as responses

A constraint violation is a client error more often than a server one. An
exception handler maps it, and applies whether the error came from a statement
in the handler or from the commit:

```python
@app.exception_handler(asyncpg.UniqueViolationError)
async def duplicate(request, exc):
    return Reply({"detail": "already exists"}, status=409)

@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def missing_reference(request, exc):
    return Reply({"detail": "refers to something that does not exist"}, status=422)
```

Anything unmapped is a `500`, with the driver's message in the log and not in
the response.

## Returning rows

A row can be returned as it is. asyncpg's `Record` becomes a JSON object, and a
list of them an array. Timestamps become ISO 8601, `UUID` a string, `numeric` a
string so no precision is lost. [Responses](responses.md#how-values-become-json)
has the full table. A pydantic model, when you want the shape declared and
documented in OpenAPI, takes `Model(**row)` or `Model.model_validate(dict(row))`.

## SQLAlchemy

The same rules, with the engine in place of the pool. Install
`sqlalchemy[asyncio]` rather than `sqlalchemy`: the async API needs `greenlet`,
and plain `sqlalchemy` pulls it in on some platforms only.

```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

@asynccontextmanager
async def worker_lifespan(app):
    engine = create_async_engine(
        "postgresql+asyncpg://...",
        pool_size=app.per_worker(20),
        max_overflow=0,            # overflow is per engine, so per loop too
    )
    try:
        yield {"sessions": async_sessionmaker(engine, expire_on_commit=False)}
    finally:
        await engine.dispose()

async def session(request):
    async with request.state.sessions() as s, s.begin():
        yield s                    # s.begin() commits or rolls back
```

The engine made at import time, as most SQLAlchemy examples show it, is the
module-level pool again: one pool for every loop.

## Migrations

Oxbrook does not run migrations. Alembic does, and works unchanged:
`alembic init -t async migrations` gives an `env.py` for an async driver. Two
changes to it are worth making, both in `examples/database/migrations/env.py`:

- **Read the URL from the environment**, from the same variable the app reads,
  rather than from `alembic.ini`. The two then cannot point at different
  databases.
- **Take an advisory lock** before migrating, so two replicas that migrate at
  the same moment do not both try to create the same table:

```python
def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        connection.execute(text("select pg_advisory_xact_lock(:key)"), {"key": LOCK})
        context.run_migrations()
```

PostgreSQL runs DDL inside the transaction, so the lock is held until the last
migration and the version row are committed, and a runner that waited finds
the schema already at head.

### When to run them

As a step before the new version starts: `alembic upgrade head` in a release
job, an init container, or a deploy script. The schema is then in place before
any process serves a request, and a migration that fails stops the deploy
rather than a server.

To migrate as the server starts instead, do it in `lifespan`, which runs once
per process, and never in `worker_lifespan`, which runs once per loop.
Alembic's async `env.py` calls `asyncio.run`, which refuses to run inside a
loop that is already running, so call it from a thread:

```python
import asyncio
from alembic import command
from alembic.config import Config

@asynccontextmanager
async def lifespan(app):
    config = Config("alembic.ini")
    config.attributes["configure_logger"] = False   # keep the app's logging
    await asyncio.to_thread(command.upgrade, config, "head")
    yield {}
```

The advisory lock in `env.py` is what makes this safe with several replicas.

## Testing against a real database

A test that replaces the database with a fake cannot see a transaction that
never commits, a pool shared across loops, or a migration that races. Run a
real PostgreSQL in a container, give each test run its own database, and drive
the app through [`TestClient`](testing.md) with more than one worker loop:

```python
with TestClient(app, workers=4) as client:
    ...
```

One loop hides every bug this page describes.
