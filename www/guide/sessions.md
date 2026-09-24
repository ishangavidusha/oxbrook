# Sessions

A session is a dict in a signed cookie.

```python
import os
from oxbrook import App, Depends, Request, Sessions

sessions = Sessions(secret=os.environ["SECRET_KEY"])

app = App()
app.middleware(sessions.middleware)

@app.get("/count")
async def count(_: Request, session = Depends(sessions.load)):
    session["views"] = session.get("views", 0) + 1
    return {"views": session["views"]}
```

Two halves, and both are needed: the dependency reads the cookie and hands the
handler a `Session`, and the middleware writes it back out.

The cookie is rewritten only when the handler actually changed the session, so
a read-only request sends no `Set-Cookie` at all. `Session` is a dict that
knows whether it was modified; `clear`, `pop` and `update` count as
modifications.

## Signed, not encrypted

!!! warning

    The client cannot forge or edit a session, but it **can read it**. The
    payload is base64, not ciphertext.

    Put an identifier in a session and look the rest up. Never put a password,
    a token, or anything you would not show the person holding the cookie.

Signing is HMAC-SHA256, compared in constant time. A tampered or expired cookie
is treated as no session at all rather than as an error, because a client with
a stale cookie should get a fresh session, not a `400`.

To make a session a way of logging in — a route that needs a signed-in user —
use [`SessionAuth`](auth.md#sessions).

## Options

```python
Sessions(
    secret=os.environ["SECRET_KEY"],
    cookie="oxbrook_session",
    max_age=1209600,      # two weeks
    secure=True,          # HTTPS only
    same_site="Lax",
    path="/",
)
```

The cookie is always `HttpOnly`, so page scripts cannot read it.

`secure=True` is the default, which means the cookie is not sent over plain
HTTP. Turn it off for local development over `http://localhost`, and turn it
back on for anything else.

## Rotating the secret

Changing the secret invalidates every existing session; each client simply gets
a new one. There is no multi-key rotation window yet.
