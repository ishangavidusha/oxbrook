"""A small notes service on MongoDB: a client per worker loop, a transaction
per write, and documents read into pydantic models.

Needs a MongoDB that is a replica set, since transactions need one. With this
repository's containers:

    make up
    cd examples/mongo
    oxbrook run app:app

Then:

    curl -X POST 127.0.0.1:8000/notes -d '{"title":"first","body":"hello"}'
    curl 127.0.0.1:8000/notes
    curl -X POST 127.0.0.1:8000/notes -d '{"title":"first","body":"again"}'   # 409

`MONGO_URL` and `MONGO_DATABASE` point it somewhere else. The things worth
reading are `worker_lifespan`, `transaction`, and the handler for
`DuplicateKeyError`; everything else is an ordinary route.
"""

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from bson import ObjectId
from oxbrook import App, Depends, HTTPError, Reply, Request
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27099/?directConnection=true")
DATABASE = os.environ.get("MONGO_DATABASE", "notes")

# Connections this process may hold, across all of its worker loops.
CONNECTIONS = 20


@asynccontextmanager
async def lifespan(app):
    # Once per process, before any loop serves: an index belongs to the
    # collection, not to a loop, so one short-lived client makes it.
    client = AsyncMongoClient(MONGO_URL)
    try:
        await client[DATABASE].notes.create_index("title", unique=True)
    finally:
        await client.close()
    yield


@asynccontextmanager
async def worker_lifespan(app):
    # Once per worker loop: PyMongo's async client belongs to the loop that
    # made it, and refuses to be used from another. tz_aware so datetimes come
    # back in UTC rather than naive.
    client = AsyncMongoClient(
        MONGO_URL, maxPoolSize=app.per_worker(CONNECTIONS), tz_aware=True
    )
    try:
        yield {"mongo": client, "db": client[DATABASE]}
    finally:
        await client.close()


app = App(title="Notes", version="0.1.0", lifespan=lifespan, worker_lifespan=worker_lifespan)


async def transaction(request):
    """A session with a transaction open, for the length of one request.

    Committed when the handler returns, aborted when it raises, whether that
    is an HTTPError, a duplicate key or a cancelled request. Pass it to every
    operation as `session=`: an operation without it runs outside the
    transaction.
    """
    async with request.state.mongo.start_session() as session:
        async with await session.start_transaction():
            yield session


@app.exception_handler(DuplicateKeyError)
async def duplicate(request, exc):
    # The transaction has been aborted by the time this runs.
    raise HTTPError(409, "a note with that title already exists")


def object_id(value: str) -> ObjectId:
    """A path segment as an ObjectId; one that cannot be is a note that does
    not exist, not a malformed request."""
    if not ObjectId.is_valid(value):
        raise HTTPError(404, "no such note")
    return ObjectId(value)


def now() -> datetime:
    """The time, to the millisecond. BSON stores no finer, so a reply built
    from the document as written would otherwise disagree with every later
    read of it."""
    at = datetime.now(UTC)
    return at.replace(microsecond=at.microsecond // 1000 * 1000)


#: An ObjectId read into a model as its hex string.
ObjectIdStr = Annotated[str, BeforeValidator(str)]


class Note(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: ObjectIdStr = Field(alias="_id")
    title: str
    body: str = ""
    created_at: datetime


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = ""


class NotePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = None


@app.get("/notes")
async def list_notes(request: Request, limit: int = 50) -> list[Note]:
    cursor = request.state.db.notes.find().sort("_id", -1).limit(min(limit, 200))
    return [Note.model_validate(doc) async for doc in cursor]


@app.get("/notes/{note_id}")
async def get_note(request: Request, note_id: str) -> Note:
    doc = await request.state.db.notes.find_one({"_id": object_id(note_id)})
    if doc is None:
        raise HTTPError(404, "no such note")
    return Note.model_validate(doc)


@app.post("/notes")
async def create_note(request: Request, note: NoteIn, session=Depends(transaction)):
    db = request.state.db
    doc = {**note.model_dump(), "created_at": now()}
    result = await db.notes.insert_one(doc, session=session)
    # A second write in the same transaction: both are kept or neither is.
    await db.activity.insert_one(
        {"note": result.inserted_id, "action": "created", "at": doc["created_at"]},
        session=session,
    )
    return Reply(Note.model_validate({**doc, "_id": result.inserted_id}), status=201)


@app.patch("/notes/{note_id}")
async def update_note(
    request: Request, note_id: str, patch: NotePatch, session=Depends(transaction)
) -> Note:
    changes = patch.model_dump(exclude_none=True)
    doc = await request.state.db.notes.find_one_and_update(
        {"_id": object_id(note_id)},
        {"$set": changes},
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    if doc is None:
        raise HTTPError(404, "no such note")
    return Note.model_validate(doc)


@app.delete("/notes/{note_id}")
async def delete_note(request: Request, note_id: str, session=Depends(transaction)):
    result = await request.state.db.notes.delete_one({"_id": object_id(note_id)}, session=session)
    if result.deleted_count == 0:
        raise HTTPError(404, "no such note")
    # Returning nothing answers 204.


if __name__ == "__main__":
    app.run()
