"""Four ways in to one notes service, and the rules that hold across them.

    OXBROOK_JWT_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') \\
        oxbrook run examples.auth:app

* People exchange a password for a token at `POST /token` (`Basic`), and use
  the token everywhere else (`JWT`).
* Scripts use an API key (`APIKey`), looked up by its digest.
* A payment provider calls `POST /webhooks/payments` with a signature over the
  body: a scheme of the app's own, twenty lines long.
* `GET /health` is public, on purpose.

Then:

    curl -u ada:correct-horse -X POST 127.0.0.1:8000/token
    curl -H "authorization: Bearer <token>" 127.0.0.1:8000/notes
    curl -H "x-api-key: ox_demo_key" 127.0.0.1:8000/notes
    curl -X POST -H "authorization: Bearer <token>" 127.0.0.1:8000/notes \\
         -d '{"title":"first"}'                           # 403 without notes:write

The stores are dicts so the example runs as it is; each is one query against a
real database, and the comments say which.
"""

import asyncio
import hashlib
import hmac
import os
import secrets
import time

import jwt
from oxbrook import App, Depends, Request, Router
from oxbrook.auth import (
    JWT,
    APIKey,
    Basic,
    Forbidden,
    Principal,
    Scheme,
    Unauthenticated,
    principal,
)
from pydantic import BaseModel

SECRET = os.environ.get("OXBROOK_JWT_SECRET") or secrets.token_urlsafe(32)
WEBHOOK_SECRET = os.environ.get("OXBROOK_WEBHOOK_SECRET", "whsec_demo").encode()
ISSUER = "https://notes.example.com/"

# ---- stores ------------------------------------------------------------------

#: username -> (password hash, scopes). Use argon2 or bcrypt for real
#: passwords; scrypt is here because it is in the standard library.
SALT = b"example-salt"
USERS = {
    "ada": (hashlib.scrypt(b"correct-horse", salt=SALT, n=2**14, r=8, p=1),
            {"notes:read", "notes:write"}),
    "bob": (hashlib.scrypt(b"battery-staple", salt=SALT, n=2**14, r=8, p=1),
            {"notes:read"}),
}

#: digest -> (owner, scopes). `select owner, scopes from api_keys where digest = $1`.
KEYS = {APIKey.digest("ox_demo_key"): ("reporting-job", {"notes:read"})}

NOTES: list[dict] = []

# ---- schemes -----------------------------------------------------------------


async def check_password(username: str, password: str) -> Principal | None:
    record = USERS.get(username)
    if record is None:
        return None
    stored, scopes = record
    # A slow hash is slow on purpose, tens of milliseconds of CPU: off the
    # worker loop, or every other request on it waits too.
    offered = await asyncio.to_thread(
        hashlib.scrypt, password.encode(), salt=SALT, n=2**14, r=8, p=1
    )
    if not hmac.compare_digest(stored, offered):
        return None
    return Principal(subject=username, scopes=scopes)


def lookup_key(digest: str) -> Principal | None:
    record = KEYS.get(digest)
    if record is None:
        return None
    owner, scopes = record
    return Principal(subject=owner, scopes=scopes)


class PaymentSignature(Scheme):
    """`x-signature: <hex hmac of the body>`, as payment providers send."""

    name = "payment_signature"

    async def authenticate(self, request: Request) -> Principal | None:
        signature = request.header("x-signature")
        if signature is None:
            return None
        body = await request.read()
        expected = hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise Unauthenticated("bad signature")
        return Principal(subject="payments")


passwords = Basic(verify=check_password)
tokens = JWT(key=SECRET, algorithms=["HS256"], audience="notes", issuer=ISSUER)
keys = APIKey(header="x-api-key", verify=lookup_key)

# ---- the app -----------------------------------------------------------------

app = App(title="Notes", auth=tokens | keys)


class NoteIn(BaseModel):
    title: str


@app.get("/health", auth=None)
async def health(_: Request):
    return {"ok": True}


@app.post("/token", auth=passwords)
async def token(_: Request, who: Principal = Depends(principal)):
    """Exchange a username and password for a token that lasts an hour."""
    now = int(time.time())
    claims = {
        "sub": who.subject, "aud": "notes", "iss": ISSUER, "iat": now, "exp": now + 3600,
        "scope": " ".join(sorted(who.scopes)),
    }
    return {"access_token": jwt.encode(claims, SECRET, algorithm="HS256"),
            "token_type": "Bearer", "expires_in": 3600}


@app.get("/notes", auth=(tokens | keys).requires("notes:read"))
async def list_notes(_: Request):
    return NOTES


@app.post("/notes", auth=tokens.requires("notes:write"))
async def create_note(_: Request, note: NoteIn, who: Principal = Depends(principal)):
    created = {"id": len(NOTES) + 1, "title": note.title, "owner": who.subject}
    NOTES.append(created)
    return created


@app.delete("/notes/{note_id}", auth=tokens.requires("notes:write"))
async def delete_note(_: Request, note_id: int, who: Principal = Depends(principal)):
    for note in NOTES:
        if note["id"] == note_id:
            # Which notes a person may delete is about the note, so it is
            # decided here rather than in the declaration.
            if note["owner"] != who.subject:
                raise Forbidden("only the owner may delete a note")
            NOTES.remove(note)
            return None
    return None


webhooks = Router(prefix="/webhooks", auth=PaymentSignature())


class Payment(BaseModel):
    note_id: int
    amount: int


@webhooks.post("/payments")
async def payment(_: Request, event: Payment):
    return {"received": event.note_id}


app.include(webhooks)
