"""A notes API that trusts an identity provider for its users.

    make up                                    # Keycloak, with the test realm
    oxbrook run examples.oidc:app

Keycloak issues the tokens; this app only checks them. Get one for Ada, who
is an editor, and use it:

    TOKEN=$(curl -s localhost:8199/realms/oxbrook/protocol/openid-connect/token \\
        -d grant_type=password -d client_id=notes-web \\
        -d username=ada -d password=ada-password \\
        -d scope="notes:read notes:write" \\
        | python -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')

    curl -H "authorization: Bearer $TOKEN" 127.0.0.1:8000/me
    curl -H "authorization: Bearer $TOKEN" 127.0.0.1:8000/notes -d '{"title":"first"}'

Bob is a viewer: the same request with his token is `403`. A script uses the
`reporter` client's own credentials, and gets read access and nothing else:

    curl -s localhost:8199/realms/oxbrook/protocol/openid-connect/token \\
        -d grant_type=client_credentials -d client_id=reporter -d client_secret=reporter-secret

Agents use the same routes as tools, at `/mcp`. An MCP client given only that
URL gets a `401` pointing at `/.well-known/oauth-protected-resource/mcp`,
which names Keycloak; it gets a token there and retries, and its tool list
holds what its token may call.

Pointing this at another provider changes one line: `OIDC.auth0(...)`,
`OIDC.entra(...)`, or `OIDC(issuer, audience=...)` for any other.
"""

import os
from contextlib import asynccontextmanager

from oxbrook import App, Depends, Request
from oxbrook.auth import OIDC, Principal, principal
from pydantic import BaseModel

KEYCLOAK = os.environ.get("OXBROOK_KEYCLOAK", "http://localhost:8199")

#: `notes-api` is what the realm's audience mapper puts in `aud` for the
#: clients allowed to call this API. A token for any other client is refused.
users = OIDC.keycloak(KEYCLOAK, "oxbrook", audience="notes-api")


@asynccontextmanager
async def lifespan(_: App):
    # Fetch the realm's keys now, so a Keycloak that is not running is found
    # at startup rather than on the first request.
    await users.load()
    yield


app = App(title="Notes", auth=users, lifespan=lifespan)

NOTES: list[dict] = []


class NoteIn(BaseModel):
    title: str


@app.get("/health", auth=None)
async def health(_: Request):
    return {"ok": True}


@app.get("/me")
async def me(_: Request, who: Principal = Depends(principal)):
    return {"subject": who.subject, "name": who.claims.get("preferred_username"),
            "scopes": sorted(who.scopes), "roles": sorted(who.roles)}


@app.get("/notes", auth=users.requires("notes:read"), tool=True)
async def list_notes(_: Request):
    """List every note."""
    return NOTES


# The client must hold the scope, and the person the role: a scope is what
# they let the client do, a role is what the realm says they are.
@app.post("/notes", auth=users.requires("notes:write", roles=("editor",)), tool=True)
async def create_note(_: Request, note: NoteIn, who: Principal = Depends(principal)):
    """Add a note, owned by the caller."""
    created = {"id": len(NOTES) + 1, "title": note.title, "owner": who.subject}
    NOTES.append(created)
    return created
