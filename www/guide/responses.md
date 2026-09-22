# Responses

Return whatever the handler has. Oxbrook decides how to send it.

| returned | sent as |
|---|---|
| `dict`, `list`, `int`, `float`, `bool` | JSON |
| a pydantic model | JSON, through pydantic's own serializer |
| `str` | `text/plain; charset=utf-8` |
| `bytes` | `application/octet-stream` |
| `None` | `204 No Content` |
| [`Response`](../reference/http.md#oxbrook.Response) | exactly what it says |
| [`SSE`](../streams/sse.md) | a `text/event-stream` that stays open |

## How values become JSON

One set of rules applies wherever Oxbrook writes JSON: a handler's return, a
`Reply`, an `HTTPError`'s detail, an SSE event, a WebSocket message, a durable
topic, and an MCP tool result. They are pydantic's, so a value encodes the same
inside a model as outside one.

| value | JSON |
|---|---|
| `datetime` | ISO 8601 string, `"2026-09-23T10:00:00.123000Z"`; a naive one has no offset |
| `date`, `time` | ISO 8601 string, `"2026-09-23"`, `"10:00:00"` |
| `timedelta` | ISO 8601 duration, `"PT1M30S"` |
| `UUID` | `"12345678-1234-5678-1234-567812345678"` |
| `Decimal` | string, `"12.50"`, so no precision is lost to a float |
| `Enum` | its value |
| `set`, `frozenset`, `tuple`, a generator | array |
| `bytes` inside a value | UTF-8 string |
| a dict with non-string keys | the keys as strings |
| NaN, infinity | `null`, since JSON has no way to write them |
| a pydantic model, at any depth | what the model serializes to |
| a mapping-like object, such as a database row | object, from its `keys()` |

Anything else is refused with a `500` and a logged `TypeError` naming the type,
rather than sent as its `repr`: a repr in a response reaches the client looking
like data, and can carry what it should not. Convert it first, or give it a
pydantic model.

Values that are already plain JSON are encoded in Rust without building a
Python string. Anything the table above had to convert takes pydantic's
encoder, which is also native code.

## Explicit responses

Return a `Response` when you need a specific status code, a content type that
is not JSON, or bytes that are already encoded and should not be touched.

```python
from oxbrook import Response

@app.get("/teapot")
async def teapot(_: Request):
    return Response(b"short and stout", status=418, content_type="text/plain")
```

Custom headers go alongside:

```python
Response(b"...", headers={"x-request-id": "abc"})
```

`body` may be `bytes` or `str`; a `str` is encoded as UTF-8.

## Status codes you get for free

- `204` when a handler returns `None`
- `404` when nothing matches the path
- `405`, with an `Allow` header, when the path exists for another method
- `413` when the body is over the limit
- `422` when a parameter or body fails validation
- `426` on a plain `GET` to a WebSocket route
- `500` when the handler raises, with no detail in the body
- `503`, with `Retry-After`, when every worker is at its concurrency limit
- `504` when a handler does not respond within `request_timeout`
