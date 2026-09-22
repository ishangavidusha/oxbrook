"""A small notes service on PostgreSQL: pool per worker loop, a transaction per
request, and the schema owned by Alembic migrations.

Needs a PostgreSQL. With this repository's containers:

    make up
    cd examples/database
    alembic upgrade head          # the schema, before the server starts
    oxbrook run app:app

Then:

    curl -X POST 127.0.0.1:8000/notes -d '{"title":"first","body":"hello"}'
    curl 127.0.0.1:8000/notes
    curl -X POST 127.0.0.1:8000/notes -d '{"title":"first","body":"again"}'   # 409

`DATABASE_URL` points both the server and the migrations somewhere else. The
three things worth reading are `worker_lifespan`, `transaction`, and the
handler for `UniqueViolationError`; everything else is an ordinary route.
"""

import os
from contextlib import asynccontextmanager

import asyncpg
from oxbrook import App, Depends, HTTPError, Reply, Request
from pydantic import BaseModel, Field

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://oxbrook:oxbrook@127.0.0.1:5499/oxbrook"
)

# Connections this process may hold, across all of its worker loops. The
# database's max_connections is shared by every replica and every other client,
# so this is the number to agree with whoever runs it.
CONNECTIONS = 20


@asynccontextmanager
async def worker_lifespan(app):
    # Once per worker loop: an asyncpg pool belongs to the loop that made it.
    # min_size=1 because min_size is opened eagerly on every loop at startup.
    pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=app.per_worker(CONNECTIONS)
    )
    try:
        yield {"pool": pool}
    finally:
        await pool.close()


app = App(title="Notes", version="0.1.0", worker_lifespan=worker_lifespan)


async def connection(request):
    """A pooled connection for the length of one request, for reads."""
    async with request.state.pool.acquire() as db:
        yield db


async def transaction(db=Depends(connection)):
    """The same connection inside a transaction.

    Committed when the handler returns, rolled back when it raises, whether
    that is an HTTPError, a constraint violation or a cancelled request. The
    commit happens before the response is sent, so a commit that fails is an
    error response rather than a success that was not kept.
    """
    async with db.transaction():
        yield db


@app.exception_handler(asyncpg.UniqueViolationError)
async def duplicate(request, exc):
    # Raised by an INSERT, or by the commit when the constraint is deferred.
    # Either way the transaction has already been rolled back by then.
    return Reply({"detail": "a note with that title already exists"}, status=409)


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = ""


class NotePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = None


COLUMNS = "id, title, body, created_at"


@app.get("/notes")
async def list_notes(_: Request, db=Depends(connection), limit: int = 50):
    rows = await db.fetch(
        f"select {COLUMNS} from notes order by id desc limit $1", min(limit, 200)
    )
    return [dict(row) for row in rows]


@app.get("/notes/{note_id}")
async def get_note(_: Request, note_id: int, db=Depends(connection)):
    row = await db.fetchrow(f"select {COLUMNS} from notes where id = $1", note_id)
    if row is None:
        raise HTTPError(404, "no such note")
    return dict(row)


@app.post("/notes")
async def create_note(_: Request, note: NoteIn, db=Depends(transaction)):
    row = await db.fetchrow(
        f"insert into notes (title, body) values ($1, $2) returning {COLUMNS}",
        note.title,
        note.body,
    )
    return Reply(dict(row), status=201)


@app.patch("/notes/{note_id}")
async def update_note(_: Request, note_id: int, patch: NotePatch, db=Depends(transaction)):
    row = await db.fetchrow(
        f"update notes set title = coalesce($2, title), body = coalesce($3, body) "
        f"where id = $1 returning {COLUMNS}",
        note_id,
        patch.title,
        patch.body,
    )
    if row is None:
        raise HTTPError(404, "no such note")
    return dict(row)


@app.delete("/notes/{note_id}")
async def delete_note(_: Request, note_id: int, db=Depends(transaction)):
    if await db.fetchval("delete from notes where id = $1 returning id", note_id) is None:
        raise HTTPError(404, "no such note")
    # Returning nothing answers 204.


if __name__ == "__main__":
    app.run()
