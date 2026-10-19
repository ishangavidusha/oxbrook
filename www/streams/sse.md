# Server-Sent Events

Return an `SSE` and Oxbrook streams it as `text/event-stream`.

```python
@app.get("/events")
async def events(_: Request):
    return SSE(app.topic("feed").subscribe())
```

The source is any async iterable, so a topic subscription is the common case
but a generator works just as well:

```python
@app.get("/clock")
async def clock(_: Request):
    async def ticks():
        while True:
            yield datetime.datetime.now().isoformat()
            await asyncio.sleep(1)

    return SSE(ticks())
```

## What gets sent

Values are encoded as JSON, or sent as-is if they are already strings. A
pydantic model goes through pydantic's serializer.

Yield an `Event` to set a name, an id for resumption, or a client retry hint:

```python
from oxbrook import Event

yield Event({"price": 42}, event="tick", id="1051", retry=3000)
```

A payload containing line breaks becomes several `data:` lines, as the wire
format requires — a raw break would otherwise end the event early. Carriage
returns count: a client ends a line at `\r`, `\n` or `\r\n` alike.

!!! warning "`event` and `id` reject line breaks"

    Both raise `ValueError` if given a value containing `\r`, `\n` or a NUL,
    and the error is raised when the `Event` is built rather than when it is
    written.

    This matters when an id comes from user data. The format has no escape for
    a line break inside a field, so a value carrying one does not produce an
    odd-looking id — it ends the event and everything after it is parsed as
    further events the application never sent.

## Resuming after a reconnect

A `Message` from a durable topic goes out with its stream id. A browser
reconnecting after a dropped connection sends the last id back in
`Last-Event-ID`, and a subscription opened with `since` replays from there:

```python
@app.get("/feed")
async def feed(request: Request):
    since = request.headers.get("last-event-id", "$")
    return SSE(app.topic("orders", durable=True).subscribe(since=since))
```

See [Durable topics](durable.md#resuming) for what the replay guarantees.

## Keep-alive

Idle connections get a comment line every `ping` seconds, 15 by default, so
proxies and load balancers do not close a quiet stream.

```python
SSE(source, ping=30)      # or ping=None to disable
```

## Slow clients

A client that reads more slowly than the source produces will not miss events.
The connection buffers a small number of chunks; once that is full the stream
waits for room rather than discarding what it cannot send immediately.

Waiting is what puts a slow reader under the [topic's own backpressure
policy](topics.md#backpressure). While the connection is blocked, the
subscription feeding it fills up, and `drop_oldest`, `block` or `error` decides
what happens next — a decision that belongs to whoever created the topic, not
to the code writing bytes to one socket.

The consequence is worth stating plainly: with `block`, one slow reader can
hold up a producer. That is what `block` means. The default, `drop_oldest`,
loses history for the slow subscriber alone and leaves everyone else
untouched.

## Disconnects

A disconnected client is detected without polling. The response body carries a
guard that hyper drops when the connection ends, and the pump races that
against the next message.

This matters more than it sounds. A stream waiting on a quiet topic has nothing
to write and therefore nothing that would fail, so without the guard it would
never notice its client had left — and would hold its subscription open
forever.

When the client goes away, the source is closed: a topic subscription is
released, and a generator gets its `close`.

!!! note "The concurrency slot is released explicitly"

    A finished stream frees its worker concurrency slot at the moment it
    finishes, not whenever the garbage collector breaks the reference cycle
    holding it. Relying on collection looks deterministic and is not, and the
    resulting leak is invisible under light load. A resource limit is never
    released by garbage collection.
