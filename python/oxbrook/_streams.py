"""In-process pub/sub topics.

A topic is a named fan-out point. Producers emit, subscribers iterate:

    topic = app.topic("orders")

    await topic.emit({"id": 1})

    async for order in topic.subscribe():
        ...

Subscribers on *different* worker loops all receive every message. That works
because free-threaded CPython lets those loops share one process, which is why
3.14t is the primary target. Under a multiprocess server each worker would
hold its own private copy of every topic and a message emitted in one would
never reach the others.

**Why this is Python and not Rust.** The rest of the hot path is Rust because
tokio threads must never touch the interpreter. That argument does not apply
here: both ends of a topic are already Python, so moving the buffer into Rust
would add an FFI crossing on emit *and* on receive to replace a deque operation
that is cheaper than the crossing itself.

Waking a subscriber costs a `call_soon_threadsafe` only when it is actually
idle. A subscriber that is keeping up never blocks, so a busy stream coalesces
naturally, the same principle that made request dispatch fast.
"""

import asyncio
import re
import threading
from collections import deque
from typing import Any

from ._logging import logger
from ._redis import Message

DROP_OLDEST = "drop_oldest"
"""Overflow policy: drop the oldest buffered message. A slow subscriber loses
history rather than stalling every producer. The default."""

DROP_NEWEST = "drop_newest"
"""Overflow policy: drop the message being emitted, for that subscriber only."""

BLOCK = "block"
"""Overflow policy: wait until the subscriber has room. Guarantees delivery, at
the cost of one slow subscriber holding up the producer."""

ERROR = "error"
"""Overflow policy: raise `TopicFull` at the producer."""

POLICIES = frozenset({DROP_OLDEST, DROP_NEWEST, BLOCK, ERROR})

DEFAULT_MAXSIZE = 1024

#: Entries read per round trip while a resumed subscription catches up. Read
#: as the reader asks for them, so a reader far behind holds one page, not the
#: whole gap.
REPLAY_PAGE = 100

#: A full Redis stream id: milliseconds, a dash, a sequence number.
STREAM_ID = re.compile(r"\d{1,20}-\d{1,20}")


def _position(ident: str) -> tuple[int, int]:
    ms, _, seq = ident.partition("-")
    return int(ms), int(seq)


def _since(since: Any, topic: str) -> str:
    """Normalise `subscribe(since=...)`: "$", or a full id to replay after.

    Usually this is a `Last-Event-ID` header, which the client sends back as it
    was given. Anything else came from a client inventing one, so it starts
    from new messages with a warning rather than failing a request whose
    worst case is having nothing to replay. A non-string is the application's
    mistake and is refused.
    """
    if not isinstance(since, str):
        raise TypeError(f"since is a stream id string, got {type(since).__name__}")
    if since in ("$", ""):
        return "$"
    if since == "0":
        return "0-0"
    if STREAM_ID.fullmatch(since):
        return since
    logger.warning(
        "subscribe: since=%r is not a stream id; starting from new messages",
        since, extra={"topic": topic},
    )
    return "$"


class TopicFull(Exception):
    """A subscriber's buffer is full and its policy is `error`."""


def _resolve(future: asyncio.Future) -> None:
    # Already-done covers the case where the waiter was cancelled, which happens
    # routinely when an SSE connection drops mid-await.
    if not future.done():
        future.set_result(None)


class Subscription:
    """One subscriber's view of a topic. Async-iterable, and closeable."""

    __slots__ = (
        "_after",
        "_backlog",
        "_buffer",
        "_closed",
        "_getter",
        "_ids",
        "_lock",
        "_loop",
        "_putters",
        "_replaying",
        "dropped",
        "maxsize",
        "policy",
        "topic",
    )

    def __init__(
        self, topic: "Topic", maxsize: int, policy: str, loop, since: str | None = None
    ) -> None:
        self.topic = topic
        self.maxsize = maxsize
        self.policy = policy
        self.dropped = 0
        self._buffer: deque[Any] = deque()
        self._loop = loop
        # Producers may run on other worker loops, so buffer and waiter state
        # are guarded rather than relying on any atomicity of deque itself.
        self._lock = threading.Lock()
        self._getter: asyncio.Future | None = None
        self._putters: deque[tuple[Any, asyncio.Future]] = deque()
        self._closed = False
        # With `since`, items are Messages carrying their stream id, and
        # `_after` is the newest id read: the replay pages on from it, and a
        # live copy of something the replay already gave is dropped by it.
        self._ids = since is not None
        self._after: tuple[int, int] | None = None
        self._backlog: deque[tuple[str, Any]] = deque()
        self._replaying = False
        if since is not None and since != "$":
            self._after = _position(since)
            self._replaying = True

    @property
    def closed(self) -> bool:
        """True once the subscription has ended: closed by the reader, or by its topic."""
        return self._closed

    @property
    def pending(self) -> int:
        """Messages delivered to this subscription and not yet read from it."""
        return len(self._buffer)

    def _try_offer(self, item: Any, ident: str | None = None) -> bool:
        """Deliver without waiting.

        Returns False only when the policy is `block` and the buffer is full,
        which tells the caller to await `_offer` instead.
        """
        if not self._ids:
            return self._put(item)
        after = self._after
        if after is not None and _position(ident) <= after:
            # Already read from the replay. `_after` is the reader's and may
            # be a step behind here; the reader checks again, so this only
            # keeps such copies out of the buffer and out of `pending`.
            return True
        return self._put(Message(ident, item))

    def _put(self, item: Any) -> bool:
        with self._lock:
            if self._closed:
                return True  # nothing to deliver to; not the producer's problem
            if len(self._buffer) >= self.maxsize:
                if self.policy == DROP_NEWEST:
                    self.dropped += 1
                    return True
                if self.policy == DROP_OLDEST:
                    self._buffer.popleft()
                    self.dropped += 1
                elif self.policy == ERROR:
                    raise TopicFull(
                        f"subscriber of topic {self.topic.name!r} is full "
                        f"({self.maxsize} buffered)"
                    )
                else:
                    return False
            self._buffer.append(item)
            getter, self._getter = self._getter, None

        if getter is not None:
            self._loop.call_soon_threadsafe(_resolve, getter)
        return True

    async def _offer(self, item: Any) -> None:
        """Deliver, waiting for room if the policy says to. Plain subscribers
        only: one opened with `since` is fed by the tail, which never waits."""
        if self._put(item):
            return
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                if self._closed:
                    return
                if len(self._buffer) < self.maxsize:
                    self._buffer.append(item)
                    getter, self._getter = self._getter, None
                    break
                waiter = loop.create_future()
                self._putters.append((loop, waiter))
            await waiter

        if getter is not None:
            self._loop.call_soon_threadsafe(_resolve, getter)

    def close(self) -> None:
        """Stop the iterator and release anyone waiting on it."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            getter, self._getter = self._getter, None
            putters = list(self._putters)
            self._putters.clear()

        if getter is not None:
            self._loop.call_soon_threadsafe(_resolve, getter)
        for loop, waiter in putters:
            loop.call_soon_threadsafe(_resolve, waiter)
        self.topic._remove(self)

    def __aiter__(self) -> "Subscription":
        return self

    async def _replayed(self) -> Any:
        """The next message from before the subscription began, or None once
        the replay has met live delivery.

        The subscription is registered before the first page is read, so live
        delivery already covers everything the replay might miss after its
        last page, provided the topic's tail had fixed its position before
        that page was read: `live` is taken first for that reason. Until then
        an empty page is retried, because entries appended between it and the
        tail starting would reach neither.
        """
        topic = self.topic
        while not self._backlog:
            if self._closed:
                self._replaying = False
                return None
            live = topic._live
            ms, seq = self._after
            page = await topic.backend.history(
                topic.name, count=REPLAY_PAGE, start=f"({ms}-{seq}"
            )
            if page:
                self._backlog.extend(page)
            elif live:
                self._replaying = False
                self._prune()
                return None
            else:
                await asyncio.sleep(0.01)
        ident, value = self._backlog.popleft()
        self._after = _position(ident)
        return Message(ident, value)

    def _prune(self) -> None:
        """Drop buffered live copies of what the replay already gave.

        The tail delivers while the replay reads, so entries the replay
        reaches can be waiting in the buffer as well. The reader would skip
        them, but until then they inflate `pending`, which a caller reads as
        messages it has yet to see.
        """
        after = self._after
        with self._lock:
            self._buffer = deque(m for m in self._buffer if _position(m.id) > after)

    async def __anext__(self) -> Any:
        if self._replaying:
            replayed = await self._replayed()
            if replayed is not None:
                return replayed
        while True:
            with self._lock:
                if self._buffer:
                    item = self._buffer.popleft()
                    waiting = self._putters.popleft() if self._putters else None
                    ready = True
                elif self._closed:
                    raise StopAsyncIteration
                else:
                    getter = self._loop.create_future()
                    self._getter = getter
                    ready = False

            if ready:
                if waiting is not None:
                    loop, waiter = waiting
                    loop.call_soon_threadsafe(_resolve, waiter)
                if self._ids:
                    position = _position(item.id)
                    if self._after is not None and position <= self._after:
                        continue  # delivered live while the replay also read it
                    self._after = position
                return item
            await getter

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.close()


class Topic:
    """A named fan-out point shared by every worker loop in the process.

    With a backend it is also shared across processes: emitting appends to a
    Redis stream, and a tail task in every other process feeds its local
    subscribers. The publishing process delivers locally itself and the tail
    skips its own node, so nobody sees a message twice.
    """

    __slots__ = ("_live", "_lock", "_subs", "_tail", "backend", "maxsize", "name", "policy")

    def __init__(
        self,
        name: str,
        maxsize: int = DEFAULT_MAXSIZE,
        policy: str = DROP_OLDEST,
        backend: Any = None,
    ) -> None:
        if policy not in POLICIES:
            raise ValueError(
                f"unknown policy {policy!r}; choose one of {', '.join(sorted(POLICIES))}"
            )
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        self.name = name
        self.maxsize = maxsize
        self.policy = policy
        self.backend = backend
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()
        self._tail: Any = None
        # Set once the tail has a definite position in the stream, from when
        # on every entry appended reaches local subscribers live.
        self._live = False

    @property
    def subscribers(self) -> int:
        """Local subscribers only. Other processes are not visible from here."""
        return len(self._subs)

    @property
    def durable(self) -> bool:
        """True when the topic is backed by Redis, False for an in-memory topic."""
        return self.backend is not None

    def _fan_out(self, item: Any, ident: str, own: bool) -> int:
        """Hand an entry from the stream to every local subscriber that takes
        it, without waiting.

        This node's own entries go only to subscribers opened with `since`:
        `emit` already gave everyone else theirs directly, without waiting on
        Redis. Those subscribers take everything from here instead, because
        this is the one place entries arrive in stream order. Fed from two
        paths, an own entry could be offered after a later one from another
        node, and an id-ordered reader could not tell that from a repeat.
        """
        delivered = 0
        for sub in self._snapshot():
            if own and not sub._ids:
                continue
            if not sub._try_offer(item, ident):
                # A `block` subscriber with a full buffer. The tail must not
                # stall on one slow reader, so this behaves as drop_newest.
                sub.dropped += 1
            delivered += 1
        return delivered

    async def _pump(self) -> None:
        """Feed local subscribers from the stream, skipping this node's own messages.

        Reconnects rather than dying. A tail that gave up on the first dropped
        connection would leave the process silently deaf to every other node,
        with nothing to indicate it: local delivery would keep working, so the
        failure would only show as messages that never arrive.

        Follows from a definite id rather than "$", and resumes from the last
        one it delivered after a reconnect, so what other nodes publish during
        an outage arrives late rather than not at all. A resumed subscription
        relies on the first of those: its replay hands over to this.
        """
        delay = 0.5
        broken = False
        position = None
        while True:
            try:
                if position is None:
                    position = await self.backend.last_id(self.name)
                    self._live = True
                async for entry_id, value, own in self.backend.follow(self.name, position):
                    if broken:
                        logger.info("topic reconnected", extra={"topic": self.name})
                        broken = False
                    self._fan_out(value, entry_id, own)
                    position = entry_id
                    delay = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - connection lost, retry
                # One line per outage, not per attempt: a full traceback every
                # half second during a Redis restart buries everything else.
                if not broken:
                    logger.warning(
                        "topic lost its backend, retrying",
                        extra={"topic": self.name, "error": f"{type(exc).__name__}: {exc}"},
                    )
                    broken = True
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    def _ensure_tail(self) -> None:
        if self.backend is None or self._tail is not None:
            return
        with self._lock:
            if self._tail is not None:
                return
            # Started on whichever worker loop subscribes first. Which loop it
            # is does not matter: delivery to subscriptions on other loops
            # already crosses loops safely.
            self._tail = asyncio.get_running_loop().create_task(self._pump())

    async def history(self, count: int = 100) -> list:
        """Recent messages, oldest first. Durable topics only."""
        if self.backend is None:
            raise RuntimeError(f"topic {self.name!r} is not durable, so it has no history")
        return await self.backend.history(self.name, count=count)

    def consumer(self, group: str, name: str, **options: Any):
        """A member of a consumer group, for at-least-once processing.

        Unlike `subscribe`, which is a broadcast to everyone, each message goes
        to exactly one member of the group and stays pending until acked.
        """
        if self.backend is None:
            raise RuntimeError(
                f"topic {self.name!r} is not durable; consumer groups need a backend"
            )
        from ._redis import Consumer

        return Consumer(self.backend, self.name, group, name, **options)

    def subscribe(
        self,
        maxsize: int | None = None,
        policy: str | None = None,
        *,
        since: str | None = None,
    ) -> Subscription:
        """Start receiving. Must be called from inside a running event loop.

        The subscription binds to the loop it was created on, which is how a
        producer on another worker knows where to deliver.

        `since` resumes a durable topic from a position: a `Message.id` this
        topic handed out, `"0"` for everything the stream still holds, or `"$"`
        for new messages only. Messages after it are replayed, then live ones
        follow with none missed or repeated across the switch. With `since`,
        the subscription yields `Message` objects rather than bare values, so
        the reader always knows its position; an `SSE` response sends each id,
        and a reconnecting browser sends the last one back as `Last-Event-ID`:

            since = request.headers.get("last-event-id", "$")
            return SSE(topic.subscribe(since=since))

        A malformed id, which only a client inventing one can send, starts from
        new messages and logs a warning. The stream is trimmed to about
        `maxlen` entries, so a reader away for longer than that resumes from the
        oldest one kept. Refused on an in-memory topic, which keeps no history.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                "subscribe() needs a running event loop; call it inside a handler"
            ) from None

        policy = policy or self.policy
        if policy not in POLICIES:
            raise ValueError(
                f"unknown policy {policy!r}; choose one of {', '.join(sorted(POLICIES))}"
            )
        if since is not None:
            if self.backend is None:
                raise RuntimeError(
                    f"topic {self.name!r} is not durable, so there is nothing to resume from"
                )
            since = _since(since, self.name)
        sub = Subscription(self, maxsize or self.maxsize, policy, loop, since)
        with self._lock:
            self._subs.append(sub)
        self._ensure_tail()
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def _snapshot(self) -> list[Subscription]:
        with self._lock:
            return list(self._subs)

    async def emit(self, item: Any) -> int:
        """Deliver to every local subscriber. Returns how many received it.

        On a durable topic the message is appended to the stream *first*, so
        that returning means it is recorded and other processes will see it.
        That costs a round trip, which is the trade being made by asking for
        durability.

        Only awaits on a local subscriber when it uses the `block` policy and
        is full.
        """
        if self.backend is not None:
            await self.backend.publish(self.name, item)

        delivered = 0
        for sub in self._snapshot():
            # One opened with `since` gets this from the tail, in stream order.
            if not sub._ids and not sub._try_offer(item):
                await sub._offer(item)
            delivered += 1
        return delivered

    def emit_nowait(self, item: Any) -> int:
        """Deliver to local subscribers without ever waiting.

        A `block` subscriber that is full is treated as `drop_newest`, because
        the alternative here would be blocking a thread that must not block.

        Refused on a durable topic: appending to the stream is an await, so this
        could only ever deliver locally, and a call named `emit` that silently
        skipped durability is worse than an error.
        """
        if self.backend is not None:
            raise RuntimeError(
                f"topic {self.name!r} is durable; use `await emit()` so the "
                f"message is recorded, not just delivered in this process"
            )
        delivered = 0
        for sub in self._snapshot():
            if not sub._try_offer(item):
                sub.dropped += 1
            delivered += 1
        return delivered

    def close(self) -> None:
        """End every local subscription and stop following Redis. Messages
        emitted afterwards reach no one here."""
        with self._lock:
            tail, self._tail = self._tail, None
            self._live = False
        if tail is not None:
            # A task is cancelled from its own loop, and the tail's is
            # whichever worker subscribed first, not necessarily this one.
            tail.get_loop().call_soon_threadsafe(tail.cancel)
        for sub in self._snapshot():
            sub.close()

    async def _stop_tail(self) -> None:
        """Stop the tail if it runs on the calling loop, and wait for it.

        Called on every worker loop as the server stops: the loop is about
        to close, and a task still pending on it would be destroyed with a
        connection half-read. Subscriptions are left alone; their handlers
        are ending with the server.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            tail = self._tail
            if tail is None or tail.get_loop() is not loop:
                return
            self._tail = None
            self._live = False
        tail.cancel()
        try:
            await tail
        except asyncio.CancelledError:
            pass

    def __repr__(self) -> str:
        kind = "durable" if self.durable else "memory"
        return f"<Topic {self.name!r} {kind} subscribers={self.subscribers}>"
