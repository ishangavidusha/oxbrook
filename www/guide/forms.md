# Forms and uploads

Three ways to receive something other than JSON:

| need | use |
|---|---|
| an HTML form, or a small file upload | `request.form()`, or a model bound with `Form()` |
| an upload larger than `max_body`, or one to refuse before it arrives | a `BodyStream` argument, and `body.parts()` for a multipart form |
| the raw bytes of an ordinary body | `request.body` |

## Reading a form

```python
@app.post("/profile")
async def profile(request: Request):
    form = request.form()
    name = form["name"]              # first value of a field
    tags = form.getlist("tag")       # every value of a repeated field
    avatar = form.get("avatar")      # an UploadFile, or None
    return {"name": name, "tags": tags}
```

`request.form()` parses `application/x-www-form-urlencoded` and
`multipart/form-data`. Parsing happens in Rust, on the worker thread, only when
it is called; a route that never reads a form pays nothing.

A `FormData` behaves like a read-only mapping from each name to its first value,
and keeps every value in order:

| | |
|---|---|
| `form["name"]`, `form.get("name")` | the first value |
| `form.getlist("name")` | every value, in order |
| `form.multi_items()` | every `(name, value)` pair |
| `form.files` | every `UploadFile` |

Text fields are `str`. Bytes that are not valid UTF-8 become replacement
characters, as they do in a query string, rather than failing the request.

## Files

A multipart part with a filename is an `UploadFile`:

| | |
|---|---|
| `filename` | as the client sent it |
| `content_type` | as the client sent it, or `None` |
| `size` | in bytes |
| `read()` | the content as `bytes` |
| `text(encoding="utf-8")` | the content decoded |

!!! warning "A filename is client input"

    `filename` may be empty, contain `..`, or contain slashes. Never join it
    onto a directory without sanitising it first.

A form is held in memory while it is read, like any other body, and is bounded
by `max_body`. For files larger than that, use a `BodyStream` and
[read the form a part at a time](#streaming-a-multipart-upload).

## Binding a form to a model

```python
from oxbrook import Form, UploadFile
from pydantic import BaseModel

class Signup(BaseModel):
    email: str
    age: int
    interests: list[str] = []
    avatar: UploadFile | None = None

@app.post("/signup")
async def signup(_: Request, data: Signup = Form()):
    ...
```

Fields are validated by pydantic, and a failure is a `422` in the same shape as
a JSON body's. A field declared as a `list` collects every value of a repeated
form field; any other field takes the first. A model with an `UploadFile` field
is documented in OpenAPI as `multipart/form-data`, and without one as
`application/x-www-form-urlencoded`.

A handler takes one body: a JSON model, a `Form()` model, or a `BodyStream`.

## When a form cannot be read

| | status |
|---|---|
| the body is not a form content type | `415` |
| malformed multipart, or no boundary | `400` |
| more than `max_parts` fields and files | `413` |

`max_parts` defaults to 1,000 and bounds how many objects one request can make
the worker build: `request.form(max_parts=50)`, or `Form(max_parts=50)`.

## Streaming a body

```python
from oxbrook import BodyStream

@app.put("/uploads/{name}")
async def upload(request: Request, name: str, body: BodyStream):
    if not allowed(request):
        raise HTTPError(403)                 # before a byte of the body is read
    size = 0
    with open(storage_path(name), "wb") as out:
        async for chunk in body:
            out.write(chunk)
            size += len(chunk)
    return {"stored": size}
```

An argument annotated `BodyStream` makes the route stream: the server hands the
request to the handler before reading the body, and chunks arrive as the client
sends them. `await body.read()` collects whatever remains.

**Nothing is read until the handler asks.** The server does not touch the body
until the first chunk is requested, so a client that sent
`Expect: 100-continue` is never told to continue if the handler refuses first.
The upload does not happen.

**Memory stays flat.** About a megabyte waits between the socket and the
handler. Past that the server stops reading until the handler catches up, and
TCP pushes back on the client. A 200 MB upload to a handler that reads slowly
grows the process by a few megabytes, not two hundred.

**Limits apply as the body arrives.**

| | the iterator raises |
|---|---|
| more than `max_body` bytes, or a declared `Content-Length` over it | `HTTPError(413)` |
| the client stops sending for longer than `request_timeout` | `HTTPError(408)` |
| the connection ends before the body does | `HTTPError(400)` |

Unhandled, each becomes that response. A declared length over the limit is
refused without reading.

**The timeout follows progress.** For a streaming route, `request_timeout`
counts from the last chunk that moved rather than from when the request
arrived. An upload that takes ten minutes and keeps moving is not cut off. A
handler that stops reading, or reads everything and then never answers, still
gets `504`.

**Read the body before responding.** When the response is sent the request is
over; a handler still reading gets `HTTPError(400)`.

On a route without a `BodyStream`, `request.stream()` yields the
already-collected body once, so code that reads a stream works on both. On a
streaming route, `request.body` is empty.

## Streaming a multipart upload

A browser uploads a file as `multipart/form-data`, with the file between the
form's other fields. `body.parts()` parses that as it arrives, so each file can
go to disk chunk by chunk:

```python
@app.post("/uploads")
async def upload(request: Request, body: BodyStream):
    fields, stored = {}, []
    async for part in body.parts():
        if part.filename is None:
            fields[part.name] = await part.text()
            continue
        with open(storage_path(part.filename), "wb") as out:
            async for chunk in part:
                out.write(chunk)
        stored.append(part.filename)
    return {"fields": fields, "stored": stored}
```

| `Part` | |
|---|---|
| `name` | the form field's name |
| `filename` | the file's name as the client sent it, or `None` for an ordinary field |
| `content_type` | the part's declared type, or `None` |
| `headers` | the part's own headers, names lowercased |
| `async for chunk in part` | the data, as it arrives |
| `await part.read()` | the rest of the data, held in memory |
| `await part.text()` | the rest decoded, undecodable bytes replaced |

Parts arrive in the order the client sent them, and a part's data is readable
only until the next part is asked for: whatever is left unread then is skipped,
and reading the old part raises `RuntimeError`. The parser is the one
`request.form()` uses, so a body is accepted or refused the same way either
way, with the same statuses: `415` for a body that is not multipart, `400` for
a malformed one or one that ends early, and `413` past `max_parts`
(`body.parts(max_parts=50)`). `max_body` and `request_timeout` apply to the
whole body as they do to any stream.

A route that binds a form with `Form()` can be an [agent tool](../agents.md),
as long as the form has no files: the model's fields become the tool's
arguments, and a call reaches the handler, and any router middleware reading
`request.form()`, as a URL-encoded body. A list becomes a repeated field, a
boolean `true` or `false`, and a `None` is left out. A form with files, and a
route that streams its body, cannot be tools: tool arguments arrive as JSON,
and `tool=True` raises at registration.
