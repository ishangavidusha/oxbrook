# MongoDB

MongoDB works through PyMongo's async API, `AsyncMongoClient`, which loads its
C extensions on the free-threaded build without turning the GIL back on. The
pattern is the one for [PostgreSQL](database.md): a client per worker loop, a
transaction per request where writes must go together, and documents returned
as they are or read into pydantic models. A complete service is in
`examples/mongo/`.

```bash
pip install pymongo
```

```python
from contextlib import asynccontextmanager

from oxbrook import App, Depends, HTTPError, Reply, Request
from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError

@asynccontextmanager
async def worker_lifespan(app):
    client = AsyncMongoClient(URL, maxPoolSize=app.per_worker(20), tz_aware=True)
    try:
        yield {"mongo": client, "db": client["notes"]}
    finally:
        await client.close()

app = App(worker_lifespan=worker_lifespan)

async def transaction(request):
    async with request.state.mongo.start_session() as session:
        async with await session.start_transaction():
            yield session

@app.exception_handler(DuplicateKeyError)
async def duplicate(request, exc):
    raise HTTPError(409, "already exists")

@app.get("/notes")
async def notes(request: Request):
    return await request.state.db.notes.find().to_list(50)

@app.post("/notes")
async def create(request: Request, note: NoteIn, session = Depends(transaction)):
    result = await request.state.db.notes.insert_one(note.model_dump(), session=session)
    return Reply({"id": result.inserted_id}, status=201)
```

## One client per worker loop

`AsyncMongoClient` belongs to the event loop it was first used on, and raises
`RuntimeError` when another loop uses it. Oxbrook runs several loops, so a
client made at import time works on one of them and fails on the rest. Make it
in `worker_lifespan`, once per loop, and reach it through `request.state`.

`maxPoolSize` is per client, so it is multiplied by the loop count:
`app.per_worker(20)` says this process may hold twenty connections in all. See
[lifespan](lifespan.md#size-per-loop-budget-per-process) for the arithmetic.

Work that belongs to the database rather than to a loop, such as creating
indexes, goes in `lifespan` with a short-lived client of its own:

```python
@asynccontextmanager
async def lifespan(app):
    client = AsyncMongoClient(URL)
    try:
        await client["notes"].notes.create_index("title", unique=True)
    finally:
        await client.close()
    yield
```

## A transaction per request

The `transaction` dependency above opens a session and a transaction around
the handler. Its teardown sees how the handler finished, as with
[PostgreSQL](database.md#a-transaction-per-request):

| the handler | the transaction |
|---|---|
| returned | committed |
| raised, including `HTTPError` and `DuplicateKeyError` | aborted |
| was cancelled because the client left | aborted |

Every operation that belongs to the transaction takes `session=session`. One
that leaves it out runs outside the transaction, is kept whatever happens, and
does not see the transaction's own uncommitted writes.

Transactions need a replica set or a sharded cluster. A standalone `mongod`
refuses them. For development, a replica set of one member is enough:

```yaml
services:
  mongo:
    image: mongo:8
    command: ["--replSet", "rs0", "--bind_ip_all"]
    ports: ["127.0.0.1:27017:27017"]
    healthcheck:
      test: ["CMD", "mongosh", "--quiet", "--eval", "try { rs.status().ok } catch (e) { rs.initiate({_id: 'rs0', members: [{_id: 0, host: 'localhost:27017'}]}).ok }; db.hello().isWritablePrimary || quit(1)"]
      interval: 2s
      retries: 30
```

Connect to it with `directConnection=true`. The member advertises itself as
`localhost:27017`, which reaches it from the host only when the published port
is the same, and a client that follows the advertised name otherwise fails to
find the primary.

Reads that need no transaction use `request.state.db` directly.

## Returning documents

A document can be returned as PyMongo gives it. Its `ObjectId` becomes the
24-character hex string, `Decimal128` a string, and datetimes ISO 8601. See
[responses](responses.md#how-values-become-json) for the full table.

For a declared shape, in the OpenAPI document and checked on the way out, read
documents into a pydantic model. An `ObjectId` is not a `str`, so the field
converts it:

```python
from typing import Annotated
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

ObjectIdStr = Annotated[str, BeforeValidator(str)]

class Note(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: ObjectIdStr = Field(alias="_id")
    title: str
    created_at: datetime

@app.get("/notes/{note_id}")
async def get_note(request: Request, note_id: str) -> Note:
    if not ObjectId.is_valid(note_id):
        raise HTTPError(404, "no such note")
    doc = await request.state.db.notes.find_one({"_id": ObjectId(note_id)})
    if doc is None:
        raise HTTPError(404, "no such note")
    return Note.model_validate(doc)
```

An id that cannot be an `ObjectId` names a note that does not exist, so it is a
`404`. Without the check, `ObjectId()` raises and the request is a `500`.

## Dates

BSON stores datetimes in UTC to the millisecond. Two consequences:

- Pass `tz_aware=True` to the client. Without it, datetimes come back naive,
  and are written without an offset.
- A reply built from a document before it was written carries microseconds a
  later read does not. Round to the millisecond before writing if the two must
  agree:

```python
at = datetime.now(UTC)
at = at.replace(microsecond=at.microsecond // 1000 * 1000)
```

## Object-document mappers

An ODM that binds its models to one database for the whole process cannot work
with more than one worker loop. Beanie is the common example. `init_beanie`
stores the client on the document classes, so the last loop to initialise wins
and every other loop gets PyMongo's "different event loop" error. With such an
ODM, run one worker loop (`workers=1`), or use pydantic models over the driver
as above, which keeps the client per loop.

## Testing against a real database

Use a real MongoDB in a container, give each test run its own database, and
drive the app through [`TestClient`](testing.md) with more than one worker
loop. A client shared across loops fails as soon as a second loop uses it, and
one loop hides that.
