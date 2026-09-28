"""A notes app people log in to through an identity provider.

    make up                                    # Keycloak, with the test realm
    oxbrook run examples.login:app

Open http://127.0.0.1:8000/auth/login?next=/me and log in as `ada` with
`ada-password`: Keycloak shows its login page, sends the browser back to
`/auth/callback`, and the app keeps who that was in its session cookie. From
then on the browser is logged in, `/notes` works, and a `POST` from another
site's page is refused. `POST /auth/logout` ends it.

Use 127.0.0.1, not localhost: the realm's `notes-login` client accepts only
redirects to 127.0.0.1, as a provider accepts only the redirect URIs
registered with it.

Logging in with Google instead changes one line:

    login = OAuthLogin.google(client_id=..., client_secret=..., sessions=sessions,
                              on_login=remember)

and `OAuthLogin.github(...)` and `OAuthLogin.microsoft(...)` are the same.
"""

import os
from contextlib import asynccontextmanager

from oxbrook import App, Depends, Request, Sessions
from oxbrook.auth import Login, OAuthLogin, Principal, SessionAuth, principal
from pydantic import BaseModel

KEYCLOAK = os.environ.get("OXBROOK_KEYCLOAK", "http://localhost:8199")

sessions = Sessions(secret=os.environ.get("SECRET_KEY", "change-me-" + "x" * 32))

#: The app's users, by the provider's id for them. A database table in a real
#: app, looked up and inserted through a pool from `request.state`.
USERS: dict[str, dict] = {}


async def remember(_: Request, who: Login) -> str:
    """Find or create the user; what this returns is kept in the session."""
    key = f"{who.provider}:{who.subject}"
    USERS.setdefault(key, {"id": key, "name": who.name, "email": who.email})
    return key


async def load_user(user_id: str) -> Principal | None:
    user = USERS.get(user_id)
    return None if user is None else Principal(subject=user_id, user=user)


login = OAuthLogin.keycloak(
    KEYCLOAK, "oxbrook", client_id="notes-login", client_secret="notes-login-secret",
    sessions=sessions, on_login=remember,
)
web = SessionAuth(sessions, key="user_id", load=load_user)


@asynccontextmanager
async def lifespan(_: App):
    # Fetch the provider's discovery document and keys now, so a Keycloak
    # that is not running is found at startup rather than at the first login.
    await login.load()
    yield


app = App(title="Notes", auth=web, lifespan=lifespan)
app.middleware(sessions.middleware)
app.include(login.routes(prefix="/auth"))  # /auth/login, /auth/callback, /auth/logout

NOTES: list[dict] = []


class NoteIn(BaseModel):
    title: str


@app.get("/", auth=None)
async def home(_: Request):
    return {"login": "/auth/login?next=/me"}


@app.get("/me")
async def me(_: Request, who: Principal = Depends(principal)):
    return who.user


@app.get("/notes")
async def list_notes(_: Request, who: Principal = Depends(principal)):
    return [n for n in NOTES if n["owner"] == who.subject]


@app.post("/notes")
async def create_note(_: Request, note: NoteIn, who: Principal = Depends(principal)):
    created = {"id": len(NOTES) + 1, "title": note.title, "owner": who.subject}
    NOTES.append(created)
    return created
