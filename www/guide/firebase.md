# Firebase

Firestore and the Realtime Database work through the Firebase Admin SDK. The
SDK is synchronous, so its routes are [`blocking=True`](blocking.md) and run on
the shared threadpool. Its clients are thread-safe and bound to no event loop,
so one Firebase app serves the whole process. A complete service, tested
against the emulators, is in `examples/firebase/`.

```bash
pip install firebase-admin
```

```python
from contextlib import asynccontextmanager

import firebase_admin
from firebase_admin import credentials, db, firestore
from oxbrook import App, HTTPError, Request

@asynccontextmanager
async def lifespan(app):
    firebase = firebase_admin.initialize_app(
        credentials.ApplicationDefault(),
        options={"projectId": PROJECT, "databaseURL": DATABASE_URL},
    )
    try:
        yield {"firestore": firestore.client(firebase), "firebase": firebase}
    finally:
        firebase_admin.delete_app(firebase)

app = App(lifespan=lifespan)

@app.get("/notes/{note_id}", blocking=True)
def get_note(request: Request, note_id: str):
    snapshot = request.state.firestore.collection("notes").document(note_id).get()
    if not snapshot.exists:
        raise HTTPError(404, "no such note")
    return {"id": snapshot.id, **snapshot.to_dict()}

@app.post("/notes/{note_id}/like", blocking=True)
def like(request: Request, note_id: str):
    likes = db.reference(f"likes/{note_id}", app=request.state.firebase)
    return {"likes": likes.transaction(lambda n: (n or 0) + 1)}
```

## On the free-threaded build

The Realtime Database and Authentication parts of the SDK talk HTTP and keep
the GIL off.

**Firestore turns the GIL back on.** Its client is built on grpcio, whose
extension does not yet declare support for free-threading, so importing
`firebase_admin.firestore` re-enables the GIL for the whole process, with a
`RuntimeWarning`. Everything still works, but the worker loops no longer run
Python in parallel. Until grpcio supports free-threading:

- On the free-threaded build, an app that uses Firestore runs as a GIL app
  would. Check `sys._is_gil_enabled()` after your imports.
- On the standard build, nothing changes: Oxbrook runs one worker loop there
  anyway.
- Do not force the GIL off with `PYTHON_GIL=0`. That is unsafe with an
  extension that has not declared support.

## One app per process

`initialize_app` belongs in `lifespan`, which runs once per process, not in
`worker_lifespan`, which runs once per loop. Pass its clients to handlers
through `request.state`. The SDK keeps a registry of apps by name, so an app
that may be built twice in one process, as in tests, gives each its own `name=`.

Threads are the budget here, not connections. Every Firebase call holds a
thread of the blocking pool for its length. Size the pool with
`app.run(blocking_threads=...)` for the number of calls you expect at once.

## Transactions

Firestore transactions run a function that Firestore may call again if
another write got there first. Raise inside it and nothing is written,
`HTTPError` included:

```python
@app.post("/notes", blocking=True)
def create(request: Request, data: NoteIn):
    client = request.state.firestore
    claim = client.collection("titles").document(title_key(data.title))
    created = client.collection("notes").document()

    @firestore.transactional
    def write(transaction):
        if claim.get(transaction=transaction).exists:
            raise HTTPError(409, "a note with that title already exists")
        transaction.set(claim, {"note": created.id})
        transaction.set(created, data.model_dump())

    write(client.transaction())
    return {"id": created.id}
```

Firestore has no unique index. A document per unique value, written in the
same transaction, is how to get one. Of two requests racing for the same
title, one commits, and the other retries, sees the claim, and gets the `409`.
The title is hashed for the document id, since a document id cannot contain
`/`.

In the Realtime Database, `reference.transaction(fn)` does the same for one
location, retrying `fn` until its write lands. Concurrent increments are all
counted.

## Returning documents

`snapshot.to_dict()` is a plain dict and can be returned as it is. Firestore
timestamps are `datetime` subclasses and become ISO 8601. Firestore's own
types, `GeoPoint` and `DocumentReference`, have no JSON form: convert them
first, to coordinates and to a path. An unconverted one is a `500`, with the
type named in the log.

## Credentials and the emulators

In production, `credentials.ApplicationDefault()` finds the platform's
service account, or the key file named by `GOOGLE_APPLICATION_CREDENTIALS`.

The emulators need no credentials. Point the SDK at them with
`FIRESTORE_EMULATOR_HOST` and `FIREBASE_DATABASE_EMULATOR_HOST`, use a project
id starting with `demo-`, which cannot reach real resources, and give the app
anonymous credentials:

```python
from google.auth.credentials import AnonymousCredentials

class Emulator(credentials.Base):
    def get_credential(self):
        return AnonymousCredentials()
```

A separate `demo-` project per test run keeps runs apart without clearing
anything. Drive the app through [`TestClient`](testing.md) with more than one
worker loop.
