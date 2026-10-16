"""A small notes service on Firebase: notes in Firestore, a view counter in the
Realtime Database, both through the Admin SDK.

The Admin SDK is synchronous, so its routes are `blocking=True` and run on the
shared threadpool. Its clients are thread-safe and bound to no event loop, so
there is one Firebase app for the process, made in `lifespan`.

Runs against the emulators with this repository's containers:

    make up
    cd examples/firebase
    FIRESTORE_EMULATOR_HOST=127.0.0.1:8299 \\
    FIREBASE_DATABASE_EMULATOR_HOST=127.0.0.1:9099 oxbrook run app:app

Then:

    curl -X POST 127.0.0.1:8000/notes -d '{"title":"first","body":"hello"}'
    curl 127.0.0.1:8000/notes
    curl -X POST 127.0.0.1:8000/notes -d '{"title":"first","body":"again"}'   # 409

Against a real project, leave the emulator variables unset and provide
credentials the usual Google way (`GOOGLE_APPLICATION_CREDENTIALS`, or the
platform's service account), with `FIREBASE_PROJECT` and
`FIREBASE_DATABASE_URL` set.
"""

import hashlib
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import firebase_admin
from firebase_admin import credentials, db, firestore
from oxbrook import App, HTTPError, Reply, Request
from pydantic import BaseModel, Field

PROJECT = os.environ.get("FIREBASE_PROJECT", "demo-oxbrook")
DATABASE_URL = os.environ.get(
    "FIREBASE_DATABASE_URL", f"http://127.0.0.1:9099?ns={PROJECT}"
)


class Emulator(credentials.Base):
    """No credentials at all, which is what the emulators accept. Only ever
    used when an emulator is configured."""

    def get_credential(self):
        from google.auth.credentials import AnonymousCredentials

        return AnonymousCredentials()


def credential() -> credentials.Base:
    if os.environ.get("FIRESTORE_EMULATOR_HOST") or os.environ.get(
        "FIREBASE_DATABASE_EMULATOR_HOST"
    ):
        return Emulator()
    return credentials.ApplicationDefault()


@asynccontextmanager
async def lifespan(app):
    # Once per process. The Admin SDK's clients are synchronous and
    # thread-safe, so one app serves every worker loop and every thread of
    # the blocking pool; a name of its own keeps it apart from any other
    # Firebase app in the process.
    firebase = firebase_admin.initialize_app(
        credential(),
        options={"projectId": PROJECT, "databaseURL": DATABASE_URL},
        name=f"notes-{id(app)}",
    )
    try:
        yield {"firestore": firestore.client(firebase), "firebase": firebase}
    finally:
        firebase_admin.delete_app(firebase)


app = App(title="Notes", version="0.1.0", lifespan=lifespan)


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = ""


class NotePatch(BaseModel):
    body: str


def title_key(title: str) -> str:
    """A title as a document id. Hashed, because a title may hold `/`, which
    a document id cannot."""
    return hashlib.sha256(title.encode()).hexdigest()


def note(snapshot) -> dict:
    return {"id": snapshot.id, **snapshot.to_dict()}


@app.get("/notes", blocking=True)
def list_notes(request: Request, limit: int = 50):
    query = (
        request.state.firestore.collection("notes")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(min(limit, 200))
    )
    return [note(snapshot) for snapshot in query.stream()]


@app.get("/notes/{note_id}", blocking=True)
def get_note(request: Request, note_id: str):
    snapshot = request.state.firestore.collection("notes").document(note_id).get()
    if not snapshot.exists:
        raise HTTPError(404, "no such note")
    # Counted in the Realtime Database, in a transaction of its own: two
    # readers at once both count.
    views = db.reference(f"views/{note_id}", app=request.state.firebase)
    return {**note(snapshot), "views": views.transaction(lambda n: (n or 0) + 1)}


@app.post("/notes", blocking=True)
def create_note(request: Request, data: NoteIn):
    client = request.state.firestore
    notes = client.collection("notes")
    # Firestore has no unique index. A document per title, written in the
    # same transaction as the note, makes the title unique: of two creates
    # racing for one title, the transaction that commits second retries,
    # sees the claim, and is refused.
    claim = client.collection("titles").document(title_key(data.title))
    created = notes.document()
    doc = {**data.model_dump(), "created_at": datetime.now(UTC)}

    @firestore.transactional
    def write(transaction):
        if claim.get(transaction=transaction).exists:
            raise HTTPError(409, "a note with that title already exists")
        transaction.set(claim, {"note": created.id})
        transaction.set(created, doc)

    write(client.transaction())
    return Reply({"id": created.id, **doc}, status=201)


@app.patch("/notes/{note_id}", blocking=True)
def update_note(request: Request, note_id: str, patch: NotePatch):
    ref = request.state.firestore.collection("notes").document(note_id)
    if not ref.get().exists:
        raise HTTPError(404, "no such note")
    ref.update({"body": patch.body})
    return note(ref.get())


@app.delete("/notes/{note_id}", blocking=True)
def delete_note(request: Request, note_id: str):
    client = request.state.firestore
    ref = client.collection("notes").document(note_id)

    @firestore.transactional
    def remove(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            raise HTTPError(404, "no such note")
        transaction.delete(client.collection("titles").document(title_key(snapshot.get("title"))))
        transaction.delete(ref)

    remove(client.transaction())
    db.reference(f"views/{note_id}", app=request.state.firebase).delete()
    # Returning nothing answers 204.


if __name__ == "__main__":
    app.run()
