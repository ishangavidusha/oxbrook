# Work after the response

```python
@app.post("/users")
async def create_user(request: Request, user: NewUser):
    created = await save(request.state.db, user)
    request.after_response(send_welcome_email, created.email)
    return Reply(created, status=201)
```

`request.after_response(fn, *args, **kwargs)` sets work aside to run once the
response has been sent: an email, an audit row, a cache to warm, a webhook to
call. The client gets its answer without waiting for any of it.

## How it runs

- **On the handler's own worker loop**, after the handler has returned and its
  response is on its way. What `request.state` holds — a connection pool from
  [`worker_lifespan`](lifespan.md) — can be used, since it belongs to that
  loop.
- **An `async` function is awaited. A plain function runs on the app's
  [blocking threadpool](blocking.md)**, so a synchronous SMTP client or HTTP
  library does not freeze the loop.
- **In order, one after another.** Several calls run in the order they were
  added.
- **A failure is logged and the rest still run.** An exception from one piece
  of work is logged under `oxbrook.after`, with its traceback, and the next
  one runs. The response has already gone, so nothing reaches the client.
- **For a streamed response**, after the stream ends; **for a WebSocket**,
  after the handler returns.

Middleware can set work aside too, and it runs with the handler's. A route
called by an agent as an [MCP tool](../agents.md) runs its work after the
`/mcp` response, as it would after its own.

## When it does not run

If the handler raises, nothing it set aside runs — whether the exception
reaches the client as a `500` or an [exception handler](errors.md) turns it
into a reply. The work belonged to a request that failed: a welcome email for
a user whose insert was rolled back is the bug this prevents. The same holds
for a handler cancelled because its client left before it answered.

A handler that *returns* an error, such as `Reply(..., status=404)`, has
answered as it meant to, and its work runs.

## Limits

**It counts as in flight.** Until its work finishes, a request still holds its
place under [`max_concurrency`](../running.md#backpressure). A burst of slow
work therefore sheds new requests with `503` rather than piling up without
bound, and a [graceful shutdown](../running.md#shutdown) waits for it within
`shutdown_grace`. In the [metrics](metrics.md), it is part of
`oxbrook_loop_requests`.

**The client leaving does not cancel it.** The response was already sent.

**It does not survive the process.** Work still running when the shutdown
grace runs out, or when the process dies, is lost. Work that must happen —
a payment capture, an email that cannot go missing — belongs in a queue that
outlives the process, such as a [durable topic](../streams/durable.md) with a
consumer group, where another process picks it up and acknowledges it.

**Nothing times it out.** A piece of work that hangs holds its slot until the
server stops. Bound anything that calls the network:

```python
async def notify(url: str, payload: dict) -> None:
    async with asyncio.timeout(10):
        await http.post(url, json=payload)
```
