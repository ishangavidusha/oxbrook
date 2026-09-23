# WebSocket

```python
@app.websocket("/ws")
async def echo(request, ws):
    async for message in ws:
        await ws.send(message)
```

Oxbrook performs the handshake, so the socket is already open when the handler
runs, and the connection closes when the handler returns.

The handler takes the request and the socket. Path and query parameters work
exactly as they do on an ordinary route:

```python
@app.websocket("/rooms/{room}")
async def room(request, ws, room: str, verbose: bool = False):
    ...
```

## Sending and receiving

Text arrives as `str`, binary as `bytes`. Sending follows the value rather than
its class:

| sent value | frame |
|---|---|
| `str` | text |
| `bytes` | binary |
| anything else — dicts, pydantic models | JSON, in a text frame |

```python
await ws.send("hello")
await ws.send(b"\x00\x01")
await ws.send({"type": "tick"})
await ws.send_json(payload)          # explicit
message = await ws.receive()         # None when the peer closed
data = await ws.receive_json()
await ws.close()
```

Ping and Pong are answered underneath and never reach the handler.

## Which sites may connect

A browser attaches the user's cookies to a WebSocket handshake, and does not
apply CORS to it. Without a check, a page on any website could open a socket to
the app and act as the signed-in user. So every upgrade is checked against its
`Origin` header before the authorizer or the handler runs, and the server
answers `403` to one it does not accept.

| the handshake | accepted |
|---|---|
| has no `Origin` header | yes: it is not from a browser |
| comes from the app's own origin | yes |
| comes from an origin listed in `websocket_origins` | yes |
| comes from anywhere else | no |

"The app's own origin" means the host in `Origin` matches the request's `Host`,
or `X-Forwarded-Host` when a proxy has rewritten `Host`. Neither header can be
set by a web page, so neither can be used to get around the check.

`websocket_origins` defaults to the app's [CORS](../guide/cors.md) origins, so a
frontend already allowed to call the API can open sockets too:

```python
app = App(cors=CORS(allow_origins=["https://app.example.com"]))
# sockets: the app's own origin, and https://app.example.com
```

Set it to choose the list independently. It replaces the CORS origins rather
than adding to them:

```python
app = App(websocket_origins=["https://app.example.com", "http://localhost:5173"])
```

A CORS policy of `["*"]` does not open sockets to every origin. `*` is the CORS
setting that forbids credentials, and a socket handshake always carries them.
To accept sockets from any origin deliberately — a public feed with no
authentication — say so:

```python
app = App(websocket_origins=["*"])
```

The origin check is not authentication. It stops other websites from using a
visitor's cookies; it does nothing against a client that is not a browser,
which can send any `Origin` it likes. Authenticate in the authorizer.

## Refusing a connection

`authorize` runs **before** the handshake, which the handler cannot do: by the
time a handler runs, the `101` has been sent and the client believes it is
connected.

```python
async def members_only(request):
    if not valid(request.header("authorization")):
        raise HTTPError(401, headers={"www-authenticate": "Bearer"})

@app.websocket("/feed", authorize=members_only)
async def feed(request, ws): ...
```

Return `None` or `True` to accept. Raise `HTTPError` to refuse with a
[problem-details](../guide/errors.md#the-error-shape) response, or return a
`Response` or a `Reply` to refuse with exactly that. Return anything else falsy
and the client gets `403`. The
authorizer may be sync or async, and [middleware wraps
it](../guide/middleware.md#websocket-routes).

## Disconnects cancel the handler

A handler is cancelled when its peer goes away. That matters for the common
pattern of a socket fed by a topic:

```python
@app.websocket("/feed")
async def feed(request, ws):
    async with app.topic("orders").subscribe() as sub:
        async for order in sub:
            await ws.send(order)
```

That handler is blocked on the topic, not on the socket, so it has no way to
notice the browser closed. Cancelling it unwinds the `async with`, which
releases the subscription. Without that, every closed tab would leak a
subscription until the next message happened to arrive.

## Other details

A plain `GET` to a socket route returns `426`. Socket routes are left out of
the OpenAPI document, because OpenAPI 3.1 has no vocabulary for them.

`examples/live_feed.py` is a working chat page serving one topic over both SSE
and WebSocket: open it in several tabs and post a message.
