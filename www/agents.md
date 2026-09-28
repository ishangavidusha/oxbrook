# Agents

The same handler that serves HTTP can be a capability an agent calls, over the
[Model Context Protocol](https://modelcontextprotocol.io).

```python
@app.get("/notes/{note_id}", tool=True)
async def read_note(_: Request, note_id: int) -> Note:
    """Read one note by its id."""
    return Note(**NOTES[note_id])
```

Point an MCP client at `/mcp` and it sees a tool named `read_note`, with a
typed `note_id` argument, that docstring as its description, `Note` as its
output schema, and a read-only hint inferred from the fact that it is a `GET`.

**Nothing is declared twice.** The name, description, argument schema and
output schema all come from the handler that already exists. This is the same
`RouteInfo` the router and the OpenAPI generator use, which is why the three
cannot drift apart.

## Opt-in, deliberately

A route without `tool=True` is still a perfectly good endpoint. It simply is
not offered to agents.

!!! danger "Do not make this a default"

    Every route being agent-callable by default would mean an administrative
    delete endpoint is agent-callable by default. Opting a route in is a
    decision someone makes about that route.

## Arguments

Body fields are flattened into the argument list, so an agent calls
`write_note(title=..., body=...)` rather than nesting an object whose shape it
has to infer.

```python
@app.post("/notes", tool=True)
async def write_note(_: Request, body: NoteIn) -> Note:
    """Create a note."""
```

A body field that collides with a path or query parameter is an error at import
time, not a silent overwrite at call time.

## Middleware, errors and auth

A tool call arrives as a `POST` to `/mcp`, so the app's middleware runs around
it the way it runs around any request.

A tool is guarded by its route's [`auth=`](guide/auth.md#agents), checked with
the headers the agent sent. An admin tool called without the scope comes back
to the agent as an error with status `403`, exactly as the route would answer
over HTTP. `tools/list` shows an agent only the tools its credentials would
pass, so an agent that cannot delete is not offered a delete tool.

`/mcp` itself follows the app's declaration unless `App(mcp_auth=...)` gives
it one of its own. That declaration decides who may connect, list tools and
read topics; each tool still answers to its route's. With an
[OpenID Connect provider](guide/auth.md#openid-connect-providers), an agent can
log in by itself — see [Agents that log in](#agents-that-log-in).

The route's own [router](guide/routers.md) middleware runs around the tool call
as well, with the headers the agent sent. An admin router that refuses a request
without credentials refuses the tool call without them too. App middleware is
not run a second time.

[Exception handlers](guide/errors.md#exception-handlers) apply. A tool that
answers with an error status — an `HTTPError`, a validation failure, middleware
refusing the call — comes back to the agent with `isError: true` and the body
as its text. A tool that raises anything unhandled comes back as
`internal error`, and the traceback goes to the log: an agent is a client, and
exception text is not returned to clients. `App(debug=True)` includes it.

Routes that read a [form or a streamed body](guide/forms.md) cannot be tools:
tool arguments arrive as JSON, and marking one `tool=True` raises at
registration.

## Agents that log in

When `/mcp`'s declaration, or a tool's, uses an `OIDC` provider, the app
serves OAuth protected-resource metadata (RFC 9728) for the endpoint:

```http
GET /.well-known/oauth-protected-resource/mcp

{"resource": "https://api.example.com/mcp",
 "authorization_servers": ["https://sso.example.com/realms/acme"],
 "scopes_supported": ["notes:read", "notes:write"],
 "bearer_methods_supported": ["header"]}
```

and a `401` or `403` from `/mcp` points at it:

```http
WWW-Authenticate: Bearer resource_metadata="https://api.example.com/.well-known/oauth-protected-resource/mcp"
```

An MCP client that receives the `401` reads the metadata, asks the provider
for a token with the scopes listed — every scope any tool requires — and
retries. Nothing else has to be configured for that. The metadata is public,
since a client reads it precisely because it has no token yet.

`/mcp` has to refuse a caller without a token for the login to start, so an
app with public tools and protected ones declares `mcp_auth=` with the
provider. The provider must put the MCP endpoint's audience in its tokens, as
with any `OIDC` audience.

The resource URL is built from the `Host` the client used, with `https` when
the server serves TLS or a proxy sends `X-Forwarded-Proto: https`.

## Topics as resources

Topics show up as readable resources at `topic://<name>`. A durable one returns
recent messages. They are read under `/mcp`'s declaration, so `mcp_auth=None`
makes every topic readable by any agent.

An agent can also **follow** a topic instead of polling it. Subscribing to
`topic://orders` means the server tells the agent when there is something new,
over the same stream machinery that serves browsers and WebSocket clients — the
topic is declared once and all three read it.

Notifications carry no payload: the protocol says a resource changed and the
client reads it. So the server holds what arrived between reads. That buffer is
bounded and drops the oldest, because an agent that stops reading must not be
able to grow a server's memory by staying subscribed. Reads are also what
re-arm the notification, so a busy topic produces one "there is something to
read" per quiet period rather than one per message.

Durable topics are read from the stream itself and need no buffer.

## Inspecting without serving

```python
capabilities = app.capabilities()
```

Built from the routes marked `tool=True` without starting a server, so it can
be asserted in a test — a useful thing to pin, since the set of capabilities is
the surface an agent is allowed to reach.

## Transport

MCP has two wires, and `/mcp` answers both. Which one a client gets depends on
what it asks for, so neither has to be configured.

**2026-07-28.** A client probes with `server/discover` and, on a real answer,
uses this wire: every request is self-contained, carrying its protocol version
in `_meta`, with no handshake and no session. Server-to-client messages do not
ride a separate connection — a client sends `subscriptions/listen` and the
response to that request *is* the stream. Nothing is resumable; a dropped
stream is re-listened, not replayed.

On this wire a followed in-memory topic delivers the notification but not the
messages behind it, because there is no session to hold them between reads.
Follow a durable topic where the payload matters.

**2025-11-25 and earlier.** The handshake wire, and what a client falls back to
when the probe finds nothing. `initialize` returns an `MCP-Session-Id` that
every later message carries; a `GET` on the same path opens a stream the server
sends on, and a `DELETE` ends the session. A missing session id is `400` and an
unknown or expired one is `404`, which is how a client is told to start a new
session rather than retry into one that is gone.

Sessions expire after five minutes of silence and are capped per process; a
session holding an open stream is never idle. Event ids are not attached, so no
client attempts to resume a broken stream — resumption would need a per-stream
replay buffer, and an unsupported feature is better than one that loses
messages quietly.

`Origin` is validated on every request, as the specification requires: a
request from a browser page on another origin is refused with `403` unless
[CORS](guide/cors.md) already allows that origin. Requests without an `Origin`
header — which is every non-browser client — are unaffected.

Turn the endpoint off entirely with `App(mcp_url=None)`.

## Verified against a real client

`tests/capabilities.py` drives the official MCP SDK client against a running
server, and `tests/agents.py` drives the transport underneath it on both
wires. `tests/oidc.py` gives the same client nothing but the endpoint's URL and
a client id and secret, and it finds Keycloak through the metadata, gets a
token and calls a tool.

A constant exported by an SDK is not the same thing as a version a client will
negotiate, and nothing short of a real handshake distinguishes the two. The same
test asserts the wire format separately: the SDK exposes `snake_case` names
while the protocol on the wire is `camelCase`, so testing through the SDK alone
would not detect a serialization error.

`examples/agent_service.py` serves one set of declarations to curl, to an
OpenAPI client and to an agent.
