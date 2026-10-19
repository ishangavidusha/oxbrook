# Durable topics

A topic backed by a Redis stream persists, replays, and reaches subscribers in
other processes.

```python
app = App(redis_url="redis://localhost:6379")

@app.post("/orders")
async def place(_: Request, body: Order):
    # Returning means Redis has it, not just this process.
    await app.topic("orders", durable=True).emit(body)
    return {"ok": True}
```

Redis is optional. Oxbrook imports and runs without it; only durable topics need
it. Run it in a container:

```bash
make up
```

## What durability adds

**Cross-process fan-out.** A message emitted in one process reaches subscribers
in another. Local subscribers are still served directly, so they do not wait
for a round trip, and a tail task in every *other* process feeds its subscribers
from the stream. The tail skips messages its own node published, so nobody sees
a message twice.

**Replay.** Messages live in the stream, so a subscriber can start from an
earlier position rather than only seeing what arrives next; see
[Resuming](#resuming). For a one-off look back:

```python
recent = await app.topic("orders", durable=True).history(count=50)
```

**At-least-once delivery**, through consumer groups.

## Resuming

`subscribe(since=...)` starts from a position in the stream. Everything after
it is replayed, then live messages follow, with none missed or repeated across
the switch. A subscription opened this way yields `Message` objects carrying the
stream id, so the reader always knows where it got to:

```python
async with topic.subscribe(since=last_seen) as sub:
    async for message in sub:
        render(message.data)
        last_seen = message.id
```

`since` is an id this topic handed out, `"0"` for everything the stream still
holds, or `"$"` for new messages only.

Over SSE this needs no bookkeeping. An `SSE` response sends each `Message`'s id
as the event id, and a browser's `EventSource` reconnects with the last one in
a `Last-Event-ID` header, so a feed that resumes after a dropped connection is:

```python
@app.get("/orders/feed")
async def feed(request: Request):
    since = request.headers.get("last-event-id", "$")
    return SSE(app.topic("orders", durable=True).subscribe(since=since))
```

The first connection has no header and starts with new messages; every
reconnect picks up after the last event the browser received.

A few things follow from how this works:

- **Order is the stream's.** A resumed subscription takes every message from
  the stream, its own process's included, rather than the direct local
  delivery a plain subscription gets. That costs one Redis round trip of
  latency on locally emitted messages, and buys ids that only ever increase.
- **A malformed id starts from new messages**, with a warning in the log. Only
  a client inventing a `Last-Event-ID` can send one, and failing its request
  would gain nothing.
- **History is bounded by [trimming](#trimming).** A reader away for longer
  than the stream holds resumes from the oldest message kept.
- **In-memory topics refuse `since`**: they keep no history to resume from.

## Consumer groups

For work that must not be lost. Each message goes to exactly one member of the
group and stays pending until acknowledged.

```python
topic = app.topic("orders", durable=True)

async with topic.consumer("billing", "worker-1") as c:
    async for message in c:
        await charge(message.data)
        await message.ack()          # only now is it done
```

Kill that worker mid-message and the message comes back when it restarts,
because it was never acknowledged. If it never restarts, another member claims
it after `claim_after_ms` (60s by default).

```python
topic.consumer("billing", "worker-1", claim_after_ms=30_000, count=64)
```

That is at-least-once, not exactly-once: a worker that crashes *after* charging
and *before* acknowledging will see the message again. Make the work
idempotent.

`examples/durable_queue.py` demonstrates it. Killing the server with eight jobs
unacknowledged recovered all eight.

## Emitting is an await

`emit_nowait` is **refused** on a durable topic. Appending to a stream is an
await, so a synchronous call could only satisfy the signature by skipping
durability, which the name does not admit.

Emitting while Redis is down raises, for the same reason: a publish that
returned successfully without persisting anything is the failure mode durable
topics exist to prevent.

## Outages

A dropped connection is retried with backoff, and the tail task reconnects
rather than dying silently — a tail that stopped without saying so would leave
a node quietly deaf to every other node. One `WARNING` per outage, one line
when it comes back. It resumes after the last message it delivered, so what
other nodes published while this one was cut off arrives late rather than not
at all.

## Trimming

Streams are trimmed to roughly 10,000 entries. Redis trims approximately, on
whole nodes, so the real length hovers a little above that.

## Two nodes, one Redis

```bash
make stack
```

Builds the app image and runs two nodes against one Redis, which is the only
way to exercise cross-process fan-out the way it actually ships.
