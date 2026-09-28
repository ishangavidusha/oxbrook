# Authentication

`auth=` declares who may call a route. The same declaration is enforced on
HTTP, on a WebSocket's handshake and on an agent's tool call, and is described
in the OpenAPI document.

```python
from oxbrook import App, Depends, Request
from oxbrook.auth import APIKey, Principal, principal

async def lookup_key(digest: str) -> Principal | None:
    row = await db.fetchrow("select id, scopes from api_keys where digest = $1", digest)
    return None if row is None else Principal(subject=str(row["id"]), scopes=row["scopes"])

keys = APIKey(header="x-api-key", verify=lookup_key)

app = App(auth=keys)                       # every route needs a key

@app.get("/health", auth=None)             # except this one
async def health(_: Request):
    return {"ok": True}

@app.get("/me")
async def me(_: Request, who: Principal = Depends(principal)):
    return {"id": who.subject}
```

A request without a key gets `401`, with an [error body](errors.md#the-error-shape)
like every other error; one with a key the lookup does not know gets `401` too.
The handler never runs for either, and neither does anything that reads the
request body.

## Three ideas

A **scheme** finds a credential in a request and checks it: `APIKey`, `Bearer`,
`JWT`, `OIDC`, `Basic`, `SessionAuth`, or one you write. A **principal** is who it
found. A **requirement** is what a route demands of that principal:
`keys.requires("admin")`.

Authentication — who is calling — fails with `401`. Authorization — may they do
this — fails with `403`. Keeping them apart is what lets a browser's session
and a script's API key reach the same route under the same rule.

`Principal` is the same shape whichever scheme found it:

| field     | holds |
| --------- | ----- |
| `subject` | who: a user id, a key id, a client id. Always a `str` |
| `scheme`  | the name of the scheme that found it: `"api_key"`, `"jwt"`, ... |
| `scopes`  | a `frozenset` of what the credential grants the client, which `requires` checks |
| `roles`   | a `frozenset` of the roles an identity provider gave the caller, checked by `requires(roles=...)` |
| `claims`  | everything else the credential carried, such as a token's claims |
| `user`    | whatever a verify function looked up, if anything |

## Declaring it

`auth=` goes on the app, a router, a route or a WebSocket, and the nearest
declaration wins:

```python
app = App(auth=tokens)                                          # the default
api = Router(prefix="/api", auth=tokens | keys)                 # either works here
admin = Router(prefix="/admin", auth=tokens.requires("admin"))  # plus a scope

@app.get("/health", auth=None)                                  # public, on purpose
async def health(_: Request): ...
```

`auth=None` makes a route public even inside a protected router. An app with no
`auth=` anywhere behaves as it always did, and one with `App(auth=...)` cannot
forget a route: a public one has to say so. `oxbrook routes` lists each route's
declaration, so a public route in a protected app stands out.

The OpenAPI document and the docs page stay public. Turn them off with
`openapi_url=None` and `docs_url=None` if the shape of the API is not for
everyone.

A declaration is checked where it is written: `auth="bearer"` or a scheme whose
`authenticate` is not `async def` raises at import.

## Who is calling

The principal reaches a handler through a dependency:

```python
from oxbrook.auth import Principal, principal

@app.post("/notes")
async def create(_: Request, note: NoteIn, who: Principal = Depends(principal)):
    return await notes.insert(note, owner=who.subject)
```

`principal` is also a plain function, for middleware:

```python
@app.middleware
async def audit(request, call_next):
    reply = await call_next(request)
    who = principal(request)
    audit_log.info("%s %s by %s", request.method, request.path,
                   who.subject if who else "anonymous")
    return reply
```

It is kept in [`request.locals`](requests.md#passing-values-along-requestlocals),
under `"principal"`. It is None on a route with no `auth=`.

## Anonymous callers too

`optional(...)` lets a request with no credential through, with no principal:

```python
@app.get("/feed", auth=optional(tokens))
async def feed(_: Request, who=Depends(principal)):
    return await posts.latest(personalised_for=who and who.subject)
```

Only a request with **no** credential is anonymous. One with an expired or
forged token is still refused: treating a bad credential as none would hide
from the client that it needs a new token, and on a route that shows more to a
signed-in caller it would show less without saying why.

## Requirements

```python
tokens.requires("notes:write")                  # every scope listed
tokens.requires(any_of=("notes:read", "notes:admin"))   # at least one of them
tokens.requires(roles=("editor",))              # every role listed
tokens.requires(check=is_staff)                 # async def is_staff(principal, request) -> bool
```

A principal that fails one gets `403`. Retrying with the same credential will
not help, which is what separates it from `401`.

Scopes and roles are different things and are checked separately. A scope is
what the user let a client do on their behalf; a role is what an identity
provider says the user is. A role named `write` does not satisfy a requirement
for the scope `write`, or the other way round. Either of two roles is two
requirements joined by `|`, tried as one credential:

```python
staff = tokens.requires(roles=("admin",)) | tokens.requires(roles=("support",))
```

A rule about one particular record — only its owner may edit a note — belongs
in the handler, where the record is. Raise `Forbidden`:

```python
from oxbrook.auth import Forbidden

@app.put("/notes/{note_id}")
async def edit(_: Request, note_id: int, change: NoteIn, who=Depends(principal)):
    note = await notes.get(note_id)
    if note.owner != who.subject:
        raise Forbidden()
    ...
```

## Several schemes on one route

`a | b` accepts either. Schemes are tried in the order written, and the first
one that finds a credential of its kind decides:

```python
app = App(auth=web | keys)      # a browser's session, or a script's key
```

Two rules follow, and both are enforced for you:

**A wrong credential stops the search.** An expired token beside a valid API key
is refused. Falling through to the next scheme, or to anonymous on an optional
route, is the classic bug in multi-method designs: it turns "bad token" into
"no token".

**A requirement belongs to the scheme it is written on.** In
`tokens.requires("admin") | keys`, a key passes without the scope; in
`(tokens | keys).requires("admin")`, both need it. `a.requires("x") |
a.requires("y")` accepts a credential with either scope.

## What a refusal looks like

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer error="invalid_token", error_description="token expired"
WWW-Authenticate: Basic realm="api", charset="UTF-8"
Content-Type: application/problem+json

{"type":"about:blank","title":"Unauthorized","status":401,"detail":"token expired"}
```

Every scheme with a standard challenge sends one, one header each. A `403` from
a bearer scheme says which scopes were missing:
`Bearer error="insufficient_scope", scope="admin"`.

A client learns whether its token is **expired** or **invalid**, because that
decides whether it refreshes. It never learns which check an invalid one
failed: a bad signature, the wrong audience, an unknown key and a missing
claim all read the same, and the real reason goes to the `oxbrook.auth` log.
Credentials themselves never reach a log line or a response.

To change the body, register an [exception handler](errors.md#exception-handlers)
for `Unauthenticated` or `Forbidden`; both are `HTTPError`s. The challenge
headers are on the exception.

## The body waits

Authentication runs before the request body is read. An upload to a protected
route from a caller who is refused is answered before it arrives, so an
anonymous client cannot make the server take in a large body, and a client
that sent `Expect: 100-continue` is refused without sending it at all. A request
that is both unauthenticated and malformed gets `401`, not `422`.

The order around a protected route is:

1. the app's middleware
2. authentication, then the body is read
3. the routers' middleware
4. dependencies, body validation, the handler

So the access log records a refusal, and no router middleware runs for a
caller who was refused. App middleware finds the body unread, and
`request.body` raises there rather than returning empty bytes;
`await request.read()` reads it early if middleware truly needs it.

## The shipped schemes

### API keys

```python
keys = APIKey(header="x-api-key", verify=lookup_key)
keys = APIKey(cookie="api_key", verify=lookup_key)
```

`verify` receives the key's SHA-256 digest, never the key. Store the digest
when issuing a key, and a leaked table is not a list of working keys:

```python
key = APIKey.generate(prefix="ox_")        # 256 random bits
await db.execute("insert into api_keys (digest, owner) values ($1, $2)",
                 APIKey.digest(key), owner)
return {"key": key}                         # shown once, never stored
```

An unsalted hash is the right tool for a random key and the wrong one for a
password; `generate` makes keys long enough for it.

### Bearer tokens

```python
async def check_token(token: str) -> Principal | None:
    record = await sessions_table.find(token)
    return None if record is None else Principal(subject=record.user_id)

bearer = Bearer(verify=check_token)
```

`verify` is for any token Oxbrook cannot check itself: an opaque token in a
database, one introspected at its issuer, or a provider's SDK. A JWT signed with
a key this app holds is `JWT`; one from an identity provider is `OIDC`.

A `verify` that returns None refuses the token with `401`. One that blocks —
most SDKs do — goes through `asyncio.to_thread`, as in any handler.

### JWT

```python
tokens = JWT(key=os.environ["JWT_SECRET"], algorithms=["HS256"], audience="notes")
tokens = JWT(key=public_key_pem, algorithms=["RS256"], audience="notes",
             issuer="https://login.example.com/")
```

For tokens this app can verify with a key it holds. Checked on every request:
the signature, `exp` and `nbf` with 60 seconds of clock skew (`leeway=`), `aud`,
and `iss` when `issuer` is given. The subject is the `sub` claim; scopes are the
`scope` claim, space-separated or a list, and `scopes_claim="scp"` or
`"permissions"` reads another. `roles_claim=` names a claim to read roles from,
and either may be a tuple for a nested claim: `roles_claim=("realm_access",
"roles")`. `claims=` requires claims to have a value, or one of a list:
`claims={"token_use": "access"}`.

A token that verified is remembered until it expires, so a caller's second
request skips the signature check: for an RSA key that is most of the cost of
authentication. Only tokens that verified are kept, up to `cache_size` (4096
by default; `0` turns it off).

Some mistakes cannot be written:

- `algorithms` is required and cannot mix families. An HMAC secret goes with
  `HS*`, a public key with `RS*`, `PS*`, `ES*` or `EdDSA`. Accepting both is how
  a public key gets used as an HMAC secret to forge a token.
- `none` is never accepted.
- An HMAC secret shorter than its hash is refused.
- `audience` has no default. `audience=None` accepts any audience, and has to be
  written out, since it means accepting tokens issued for other services.

Needs PyJWT: `pip install 'oxbrook[auth]'`.

### OpenID Connect providers

```python
from oxbrook.auth import OIDC

users = OIDC("https://sso.example.com/realms/acme", audience="notes-api")
users = OIDC.keycloak("https://sso.example.com", "acme", audience="notes-api")
users = OIDC.auth0("acme.eu.auth0.com", audience="https://notes.example.com")
```

For access tokens issued by an identity provider. `OIDC` reads the provider's
discovery document, `/.well-known/openid-configuration` under the issuer,
fetches the signing keys it names, and checks every token against them: the
signature, `iss`, `aud`, `exp` and `nbf`, as `JWT` does. Claims are read the
same way, with the same `scopes_claim`, `roles_claim` and `claims=`.

The providers whose tokens differ from the defaults have a preset. Each takes
the same keyword arguments as `OIDC`, to override what it sets:

| preset | issuer | scopes | roles |
| ------ | ------ | ------ | ----- |
| `OIDC.keycloak(url, realm, audience=...)` | `<url>/realms/<realm>` | `scope` | realm roles, `realm_access.roles` |
| `OIDC.auth0(domain, audience=...)` | `https://<domain>/` | `scope` | none; `scopes_claim="permissions"` with RBAC |
| `OIDC.entra(tenant_id, audience=...)` | `https://login.microsoftonline.com/<tenant>/v2.0` | `scp` | `roles` |
| `OIDC.okta(domain, audience=...)` | `https://<domain>/oauth2/default` | `scp` | none |
| `OIDC.cognito(region, pool, client_id=...)` | `https://cognito-idp.<region>.amazonaws.com/<pool>` | `scope` | `cognito:groups` |
| `OIDC.google(client_id)` | `https://accounts.google.com` | none | none |
| `OIDC.firebase(project_id)` | `https://securetoken.google.com/<project>` | none | custom claims |

Where each one differs:

- **Keycloak** puts the API in `aud` only through an audience mapper on the
  clients that call it, or in a client scope they share. Without one, every
  token is refused for its audience. A client's own roles, rather than the
  realm's, are `roles_claim=("resource_access", "<client>", "roles")`.
- **Entra ID** takes the tenant's id; `common` and `organizations` name no one
  issuer and are refused. An API receives version 1 tokens, with issuer
  `https://sts.windows.net/<tenant>/`, unless its manifest sets
  `requestedAccessTokenVersion` to 2: pass `version=1` for those.
- **Okta** tokens are checked for a custom authorization server, `default`
  unless `server=` names another. The org server's access tokens are for Okta
  alone.
- **Cognito** access tokens carry no `aud`. The preset checks `client_id`
  instead, and `token_use` being `access`, so an ID token is not accepted in
  place of an access token.
- **Google** and **Firebase** issue ID tokens: they say who signed in, for an
  app's own backend, and carry no scopes.

Any other standards-following provider works with its issuer URL and the
audience it puts in its tokens.

What `OIDC` does so that an app does not have to:

- **Keys are fetched once per process.** Every worker loop shares them, and
  requests that arrive together before the first fetch wait for the same one.
  They are fetched again every five minutes (`keys_max_age=`) in the
  background, so a key the provider withdraws stops working, including for
  tokens already remembered.
- **Rotation arrives by itself.** A token signed with a key id not yet seen
  fetches the keys again at once, at most once a minute, so tokens with
  invented key ids cannot turn into a stream of requests to the provider.
- **Only the provider's public-key algorithms are accepted**, each only with a
  key of its own type. `none` and HMAC are refused whatever the token's header
  says, which closes the attack where a provider's public key is used as a
  shared secret. `algorithms=["RS256"]` narrows the list further.
- **An unreachable provider is `503`, not `401`.** Before any keys have been
  fetched, a token cannot be checked, and it may well be good; `401` tells a
  client to throw it away. After a failure the provider is not asked again for
  five seconds. Once keys are in hand, a provider that goes away changes
  nothing until it comes back.
- **The issuer must be HTTPS**, because keys fetched in clear can be replaced in
  transit; redirects are never followed from HTTPS to plain HTTP. `http://`
  is accepted for `localhost`, and for anything with `allow_http=True`.

The first token fetches the keys. To fetch them at startup instead, and fail
there when the provider is unreachable, load them in a lifespan:

```python
@asynccontextmanager
async def lifespan(app):
    await users.load()
    yield

app = App(auth=users, lifespan=lifespan)
```

An **ID token is not an access token**: it says who signed in to a client, and
its audience is that client. Checking `aud` against the API, as `audience`
requires, is what keeps a client from sending one in place of an access token.

Signing users in — the redirect to the provider and back — is the client's
job, or a library such as Authlib's on a server-rendered app. `OIDC` checks
the tokens that result.

### Basic

```python
async def check_password(username: str, password: str) -> Principal | None:
    user = await users.find(username)
    if user is None or not hasher.verify(user.password_hash, password):
        return None
    return Principal(subject=str(user.id), user=user)

staff = Basic(verify=check_password)
```

Basic sends the password with every request, readable by anything on the
network unless the connection is encrypted, so it is refused over plain HTTP.
HTTPS, a proxy that terminates TLS and sets `X-Forwarded-Proto: https`, and
loopback during development are accepted; `allow_http=True` accepts anything.
Check the password against a slow hash — argon2, bcrypt, scrypt — never a fast
one.

### Sessions

```python
sessions = Sessions(secret=os.environ["SECRET_KEY"])
app.middleware(sessions.middleware)

async def load_user(user_id):
    return await users.get(user_id)          # None if the user is gone

web = SessionAuth(sessions, key="user_id", load=load_user)

@app.post("/login", auth=None)
async def login(_: Request, form: Login = Form(), session=Depends(sessions.load)):
    user = await check(form)
    session["user_id"] = user.id
```

The user a [signed session](sessions.md) says is logged in. A session that does
not verify, has expired, or names a user `load` cannot find is treated as no
session rather than a wrong credential: a browser sends the cookie by itself,
and the person using it has no way to remove a stale one. `load` may return a
`Principal` to set scopes.

A browser sends the session cookie on any request to your site, including one
a hostile page causes it to make — the attack called cross-site request
forgery. `SameSite=Lax`, the default, stops most cross-site posts but not all,
so `SessionAuth` refuses, with `403`, a state-changing request (anything but
`GET`, `HEAD` and `OPTIONS`) that cannot show it came from your own pages:

1. **`Sec-Fetch-Site`**, which browsers set themselves: `same-origin` and
   `none` (typed or bookmarked) pass; `same-site` and `cross-site` go on to
   the next check.
2. **`Origin`**: this server, as the client addressed it (or as a proxy says in
   `X-Forwarded-Host`), or an origin in `trusted_origins`. `null` never passes.
3. **Neither header** — an old browser, mostly — needs an `X-CSRF-Token`
   header equal to the session's token.

`trusted_origins` defaults to the app's [CORS](cors.md) origins: a page allowed
to call the API with credentials is trusted to change things through it.

```python
web = SessionAuth(sessions, key="user_id", load=load_user,
                  trusted_origins=["https://app.example.com"])
```

The token is for clients that send neither header. Put it where the page can
read it, and send it back as a header:

```python
@app.get("/csrf")
async def csrf(_: Request, session=Depends(sessions.load)):
    return {"token": SessionAuth.csrf_token(session)}
```

```js
await fetch("/notes", {method: "POST", headers: {"X-CSRF-Token": token}, body});
```

Only a session is checked this way. An API key or a bearer token is sent by
code that chose to send it, not attached by the browser, so a request carrying
one needs no origin. A route that writes the session without `SessionAuth`
guarding it, such as `/login`, is not checked; `csrf=False` turns the check
off for an app that does its own.

## Writing a scheme

A scheme is anything with an `async def authenticate(request)` that returns a
`Principal`, returns None when the request carries no credential of its kind,
and raises `Unauthenticated` when it carries one that is wrong. Subclass
`Scheme` to get `|` and `.requires(...)`:

```python
import hashlib, hmac
from oxbrook.auth import Principal, Scheme, Unauthenticated

class StripeSignature(Scheme):
    name = "stripe"

    def __init__(self, secret: bytes):
        self.secret = secret

    async def authenticate(self, request):
        signature = request.header("stripe-signature")
        if signature is None:
            return None                               # not mine: try the next scheme
        body = await request.read()
        expected = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise Unauthenticated("bad signature")    # mine, and wrong: stop here
        return Principal(subject="stripe")

hooks = Router(prefix="/webhooks", auth=StripeSignature(STRIPE_SECRET))
```

The body has not been read when a scheme runs, so one that needs it calls
`await request.read()`; the handler still gets it afterwards. Compare secrets
with `hmac.compare_digest`, never `==`, which stops at the first wrong byte and
lets the time it took say how many were right.

Two optional methods finish the job: `challenge(error)` returns the
`WWW-Authenticate` value for a refusal, and `openapi()` the scheme's entry in
the OpenAPI document.

## WebSockets

`auth=` on a WebSocket is checked before the handshake, so a refused client gets
a real `401` or `403` with its challenge, never a socket that opens and closes.
The handler receives the principal the same way:

```python
@app.websocket("/feed", auth=web)
async def feed(_: Request, ws, who=Depends(principal)):
    ...
```

Browsers cannot set headers on a WebSocket, so a page authenticates with a
cookie — `SessionAuth` — or with a **ticket**. A long-lived token in the query
string would end up in proxy logs and browser history, and no shipped scheme
looks for one there. A ticket is safe there because it opens one socket, within
thirty seconds, and nothing else:

```python
from oxbrook.auth import Tickets

tickets = Tickets()

@app.post("/ws-ticket", auth=users)                # an ordinary authenticated call
async def ws_ticket(_: Request, who: Principal = Depends(principal)):
    return {"ticket": tickets.issue(who)}

@app.websocket("/feed", auth=tickets)
async def feed(_: Request, ws, who: Principal = Depends(principal)):
    ...                                            # who is the principal it was issued for
```

```js
const {ticket} = await (await fetch("/ws-ticket", {method: "POST", headers})).json();
const ws = new WebSocket(`wss://api.example.com/feed?ticket=${ticket}`);
```

A ticket is read only from a WebSocket upgrade, is removed the moment it is
presented, and expires after `ttl` seconds. `auth=tickets | web` accepts either
a ticket or a session cookie. Tickets are kept in the process that issued
them, so behind a load balancer with several processes the socket has to
reach the same one.

The check runs once, when the socket opens; a connection can outlive the
credential that opened it. An `authorize=` function runs after `auth=`, and can
read `principal(request)`.

## Agents

A tool is guarded by its route's declaration, checked with the headers the
agent sent: an admin tool called without the scope comes back to the agent as
an error with status `403`, as it would over HTTP. `tools/list` shows an agent
only the tools it may call, each scheme authenticating once per listing.

`/mcp` itself follows the app's declaration, or its own:

```python
app = App(auth=users, mcp_auth=users.requires("agents"))
```

With an `OIDC` provider in either, the endpoint serves the metadata an MCP
client needs to log in by itself, and its `401` points there. See
[Agents that log in](../agents.md#agents-that-log-in).

## OpenAPI

Each scheme is declared under `securitySchemes`, and each operation lists what
it accepts under `security`: `a | b` as alternatives, required scopes by name,
`optional(...)` as the empty alternative `{}`, and `auth=None` in a protected app
as `security: []`. The docs page's **Authorize** button then works without
further setup. A requirement's `check=` function and its roles have no OpenAPI
form and are left out. `OIDC` is an `openIdConnect` scheme, pointing at the
provider's discovery document.

## Testing

Send the credential the way a client would:

```python
with TestClient(app) as client:
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers={"x-api-key": KEY}).json()["id"] == "7"

    async with client.websocket("/feed", headers={"cookie": cookie}) as ws:
        ...

    client.mcp("tools/call", {"name": "admin_tool"}, headers={"x-api-key": ADMIN_KEY})
```

An app that trusts a provider is tested against one that behaves the same:
Keycloak runs in a container and issues real tokens, from a realm file loaded
at startup. `examples/oidc.py` is an app set up for the realm in Oxbrook's own
`tests/keycloak/`, with the commands to get a token from it.
