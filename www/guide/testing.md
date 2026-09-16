# Testing

```python
from oxbrook.testing import TestClient

def test_users():
    with TestClient(app) as client:
        assert client.get("/users/1").json() == {"id": 1}
        assert client.get("/users/abc").status_code == 422
        assert client.delete("/users/1").status_code == 405
```

`TestClient` starts the real server on a free port, on a background thread, and
stops it when the block ends. It exposes `get`, `post`, `put`, `patch`,
`delete`, `head`, `options` and `request`, each returning an `httpx.Response`.

The app's [lifespans](lifespan.md) run exactly as they do under `app.run`:
entering the block runs startup, leaving it runs shutdown. If startup raises,
entering the block raises that same exception, rather than the client reporting
a refused connection.

## Why a real server

A client that called handlers directly would skip routing, parameter coercion,
body limits, header handling, the `405` and `413` paths, HEAD, middleware and
the whole Rust half — which is most of the behaviour worth testing.

The cost is a real socket and a real thread per client. Reuse one across a
suite rather than opening one per assertion.

## Streams and sockets

```python
with TestClient(app) as client:
    with client.stream("GET", "/events") as response:
        for line in response.iter_lines():
            ...

    async with client.websocket("/ws") as ws:      # async: it is a real client
        await ws.send("hello")
        assert await ws.recv() == "hello"
```

`client.websocket` returns an open connection from the `websockets` library, so
that half of a test is `async`. The HTTP methods are synchronous.

## Agents

The client speaks MCP too, so an agent-facing capability can be asserted in the
same test as the endpoint it came from.

```python
with TestClient(app) as client:
    tools = client.mcp("tools/list")["tools"]
    assert client.call_tool("read_note", {"note_id": 1}) == {"id": 1, "title": "hello"}
```

## Server options

Anything `app.run` accepts can be passed through:

```python
TestClient(app, max_concurrency=1, request_timeout=1.0)
```

Which is how the backpressure and timeout paths are tested: set the limit to
something you can reach from one test.

With `tls_cert` and `tls_key`, the client talks HTTPS and trusts that
certificate file, so a test certificate issued for `127.0.0.1` works without
touching the system trust store:

```python
with TestClient(app, tls_cert="tests/cert.pem", tls_key="tests/key.pem") as client:
    assert client.base_url.startswith("https://")
    assert client.get("/").status_code == 200
```

The client itself speaks HTTP/1.1. For HTTP/2, use `httpx.Client(http2=True)`
against `client.base_url`, with `httpx[http2]` installed.

## Forms, uploads and CORS

httpx builds form bodies itself, so a form handler is tested the way a browser
would call it:

```python
client.post("/signup", data={"email": "a@b.c"}, files={"avatar": ("me.png", png, "image/png")})
client.put("/uploads/big", content=chunk_generator())     # streamed, never held whole
client.options("/items", headers={"origin": "https://app.example.com",
                                  "access-control-request-method": "POST"})
```
