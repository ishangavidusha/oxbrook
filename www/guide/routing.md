# Routing

Routes are registered with a decorator per method.

```python
@app.get("/users")
async def list_users(_: Request): ...

@app.post("/users")
async def create_user(_: Request): ...

@app.put("/users/{user_id}")
async def replace_user(_: Request, user_id: int): ...

@app.patch("/users/{user_id}")
async def update_user(_: Request, user_id: int): ...

@app.delete("/users/{user_id}")
async def delete_user(_: Request, user_id: int): ...
```

`app.route(method, path)` handles anything else. To declare routes in more than
one module, use a [router](routers.md).

Every handler takes the request as its first argument. Name it `_` when you do
not need it; it is still passed.

## Path parameters

Declare them in the path, type them in the signature.

```python
@app.get("/users/{user_id}")
async def get_user(_: Request, user_id: int):
    return {"user_id": user_id}
```

`{*name}` captures the rest of the path, slashes included.

```python
@app.get("/files/{*rest}")
async def get_file(_: Request, rest: str):
    return {"path": rest}
```

!!! warning "A catch-all is client input, not a path"

    `{*rest}` hands over what the client sent. `..` is not stripped, in either
    its plain or its encoded form. A handler that joins it to a directory must
    sanitise it first — this is a router, not a file server.

## Percent-encoding

Path parameters are percent-decoded before they are coerced, the same as query
parameters.

```
GET /items/a%20b        ->  "a b"
GET /items/a%2Fb        ->  "a/b"
GET /items/caf%C3%A9    ->  "café"
GET /items/a+b          ->  "a+b"
```

`+` is a space only in a query string; in a path it is a literal plus.

Routing happens against the raw path, so a `%2F` that decodes to a slash cannot
change which route matched. Bytes that are not valid UTF-8 become replacement
characters rather than an error, which is what the query side has always done.

Supported annotations are `str`, `int`, `float`, `bool`, `uuid.UUID`,
`datetime.date` and `datetime.datetime`. Anything else is a `TypeError` at
import time, as is a path parameter the handler does not accept, or a handler
argument that names a path segment that does not exist.

## Coercion happens in Rust

A path parameter is parsed and validated on the tokio thread that read the
request, on a radix tree built per method. A request that cannot possibly
succeed is answered without waking a Python worker at all.

```
GET    /users/42    ->  200  {"user_id": 42}
GET    /users/abc   ->  422  {"type": "about:blank", ..., "errors": [...]}
DELETE /users/42    ->  405  Allow: GET
HEAD   /users/42    ->  200  headers only, Content-Length as GET would send
```

The three richer types are validated with real calendar and format checking, so
`/events/2026-02-30` is a 422 and never becomes an exception inside a handler.

!!! note "Validated in Rust, canonicalised in Rust"

    Rust accepts ISO forms that Python's own constructors reject — `2026-01-02T03:04:05Z`,
    or a UUID without hyphens. So whatever Rust validates, it also rewrites
    into the form Python will accept before building the object. Input already
    judged valid must never raise on the other side and turn into a 500.

## Matching order

Static segments win over dynamic ones, and a catch-all is the last resort, so
`/users/me` and `/users/{user_id}` can coexist and the literal wins, whichever
order they were registered in. This is the radix tree's own precedence, not a
scan down a list of patterns, which is why routing cost does not grow with the
number of routes.

## Trailing slashes

`/orders` and `/orders/` are different routes. Neither redirects to the other,
and registering one does not create the other: a request for the path you did
not register gets a `404`.

Nothing is normalised, because the alternatives are worse. A redirect has to
choose a status code, and the choice changes whether a `POST` body survives it.
Silently accepting both hides the typo in a client that keeps sending the wrong
one.

## Conflicting routes

Two routes the router cannot tell apart are refused when the second one is
registered, so the error names the decorator that caused it rather than
appearing when the server starts.

```python
@app.get("/items/{item_id}")
async def read(_: Request, item_id: int): ...

@app.get("/items/{other}")          # ValueError at import
async def also_read(_: Request, other: int): ...
```

That covers registering the same method and path twice, two paths that differ
only in parameter names, a catch-all against a parameter in the same position,
and a WebSocket route on a path a `GET` already has — a socket route is
registered as a `GET`, so only one of the two could ever run.

Routes that differ in any way the router can see are fine, including the same
path under different methods.

## Methods you do not write

`HEAD` is answered wherever `GET` is: the same handler runs, the headers it
would have produced are sent, including `Content-Length`, and the body is
dropped. A method with no route on an existing path returns `405` with an
`Allow` header listing what that path does accept. A plain `GET` to a WebSocket
route returns `426`.
