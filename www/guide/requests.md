# Requests

The first argument to every handler is the request.

```python
@app.get("/me")
async def me(request: Request):
    token = request.header("authorization")        # case-insensitive, None if absent
    theme = request.cookies.get("theme", "light")
    return {"token": token, "theme": theme}
```

| attribute | what it is |
|---|---|
| `method` | the HTTP method, uppercase |
| `path` | the request path |
| `query` | the raw query string |
| `body` | the raw body as `bytes` |
| `headers` | every header, lowercased, as a dict |
| `cookies` | parsed cookies as a dict |
| `header(name, default=None)` | one header by name, case-insensitively |
| `form(max_parts=1000)` | the body parsed as a form; see [Forms and uploads](forms.md) |
| `stream()` | the body as an async iterator of chunks; see [Forms and uploads](forms.md#streaming-a-body) |
| `state` | what the app's lifespans yielded, read-only; see [Lifespan](lifespan.md) |
| `app` | the `App` serving the request |

## Headers are lazy

Headers stay in hyper's own map until Python asks for them. `request.header(name)`
looks one up without building anything; `request.headers` builds the whole dict
and should be avoided on a hot path. A handler that never reads a header pays
nothing for the ones that arrived.

A header that is not valid UTF-8 reads as absent rather than raising, which
keeps a malformed request from becoming a `500`.

## The request is immutable

`Request` is a frozen class. Nothing on the Python side can mutate it, which is
why it needs no locking even when several worker loops are running in the same
process on a free-threaded build. `request.state` is read-only for a related
reason: every request on a worker loop shares it.

## Passing values along: `request.locals`

`request.locals` is a plain dict belonging to this request alone, empty until
something writes to it. It is where middleware leaves something for a handler
or a dependency to read:

```python
@app.middleware
async def request_id(request, call_next):
    request.locals["request_id"] = request.header("x-request-id") or new_id()
    return await call_next(request)

@app.get("/orders")
async def orders(request: Request):
    log.info("listing", extra={"request_id": request.locals["request_id"]})
```

It costs nothing on a request that never touches it. An MCP tool call starts
with a copy of the `/mcp` request's dict, since the app's middleware ran once,
around that request; what the tool writes stays with the tool.
[Authentication](auth.md#who-is-calling) keeps the caller here, under
`"principal"`.

## Reading the body later: `request.read()`

On a route with [`auth=`](auth.md#the-body-waits), the body is read after the
caller is authenticated, so middleware outside that check finds it unread and
`request.body` raises there. `await request.read()` returns the whole body,
reading it first if it has not arrived yet; anywhere `request.body` works, it
returns the same bytes.

## Repeated headers

Repeated headers are joined with `", "`, as HTTP itself defines. `Cookie` is
parsed for you into `request.cookies`.
