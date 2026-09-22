"""Durable topics over Redis Streams.

Adds three things an in-memory topic cannot give you:

* **Cross-process fan-out.** A message emitted in one process reaches
  subscribers in another. In-process fan-out still happens directly, so local
  subscribers do not wait for a Redis round trip; the tail skips messages this
  node published so nobody sees a duplicate.
* **Durability and replay.** Messages live in a Redis stream, so a subscriber
  can start from an earlier position instead of only seeing what arrives next.
* **At-least-once delivery.** A consumer group hands each message to one member
  and keeps it pending until acknowledged. A consumer that dies mid-message
  gets it again when it comes back, or another member claims it.

Redis is optional. Oxbrook imports and runs without it; only durable topics
need it.
"""

import asyncio
import json
import os
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ._schema import encode

try:
    import redis.asyncio as aioredis
    from redis.backoff import ExponentialBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import ResponseError
    from redis.exceptions import TimeoutError as RedisTimeoutError
    from redis.retry import Retry

    HAVE_REDIS = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    aioredis = None
    ResponseError = None
    HAVE_REDIS = False


DEFAULT_URL = "redis://127.0.0.1:6379"
#: Entries kept per stream. Redis trims approximately, on whole nodes, so the
#: real length hovers a little above this.
DEFAULT_MAXLEN = 10_000
#: How long a blocking read waits before looping. Only affects how quickly a
#: cancelled tail notices, not latency of delivery.
BLOCK_MS = 5_000
#: Retries for a command that fails on a dropped connection. Without these, a
#: Redis restart turns every in-flight publish into a 500 rather than a pause:
#: the first command after the restart finds a stale pooled connection and
#: redis-py surfaces it instead of reconnecting.
CONNECT_RETRIES = 3


def _encode(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    return encode(value)


def _decode(raw: bytes | None, model: Any = None) -> Any:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        # Not JSON: hand back the bytes rather than lose the message.
        return raw
    if model is not None:
        return model.model_validate(value)
    return value


@dataclass(slots=True)
class Message:
    """One message from a consumer group, awaiting acknowledgement."""

    id: str
    data: Any
    _consumer: "Consumer | None" = field(default=None, repr=False)

    async def ack(self) -> None:
        """Mark it done. Until this is called the message stays pending and
        will be redelivered if this consumer dies."""
        if self._consumer is not None:
            await self._consumer.ack(self.id)


class Consumer:
    """A member of a consumer group. Async-iterable over `Message`.

    Each message goes to exactly one member of the group, and stays pending
    until acknowledged, which is what makes delivery at-least-once rather than
    at-most-once.
    """

    __slots__ = (
        "_buffer",
        "_closed",
        "_recovering",
        "backend",
        "block_ms",
        "claim_after_ms",
        "count",
        "group",
        "model",
        "name",
        "topic",
    )

    def __init__(
        self,
        backend: "RedisBackend",
        topic: str,
        group: str,
        name: str,
        *,
        count: int = 32,
        block_ms: int = BLOCK_MS,
        claim_after_ms: int | None = 60_000,
        model: Any = None,
    ) -> None:
        self.backend = backend
        self.topic = topic
        self.group = group
        self.name = name
        self.count = count
        self.block_ms = block_ms
        self.claim_after_ms = claim_after_ms
        self.model = model
        self._buffer: deque[Message] = deque()
        # First pass re-reads anything this consumer was holding when it died.
        self._recovering = True
        self._closed = False

    async def start(self) -> "Consumer":
        await self.backend.ensure_group(self.topic, self.group)
        return self

    async def ack(self, message_id: str) -> None:
        client = self.backend.client()
        await client.xack(self.backend.key(self.topic), self.group, message_id)

    async def pending(self) -> int:
        """How many messages this group has delivered but not had acked."""
        client = self.backend.client()
        info = await client.xpending(self.backend.key(self.topic), self.group)
        return int(info["pending"]) if info else 0

    def close(self) -> None:
        self._closed = True

    def _absorb(self, entries: Any) -> int:
        added = 0
        for entry_id, fields in entries or ():
            if fields is None:
                # Claimed an entry that has since been deleted from the stream.
                continue
            self._buffer.append(
                Message(
                    id=entry_id.decode() if isinstance(entry_id, bytes) else entry_id,
                    data=_decode(fields.get(b"d"), self.model),
                    _consumer=self,
                )
            )
            added += 1
        return added

    async def _fill(self) -> None:
        client = self.backend.client()
        key = self.backend.key(self.topic)

        if self._recovering:
            # "0" returns this consumer's own pending entries, which is how a
            # restarted worker picks up what it never acked.
            result = await client.xreadgroup(
                self.group, self.name, {key: "0"}, count=self.count
            )
            for _stream, entries in result or ():
                if self._absorb(entries):
                    return
            self._recovering = False

        if self.claim_after_ms:
            # Take over messages another member has held too long, which is what
            # makes at-least-once survive a consumer that never comes back.
            claimed = await client.xautoclaim(
                key, self.group, self.name, self.claim_after_ms, start_id="0-0",
                count=self.count,
            )
            entries = claimed[1] if len(claimed) > 1 else ()
            if self._absorb(entries):
                return

        result = await client.xreadgroup(
            self.group, self.name, {key: ">"}, count=self.count, block=self.block_ms
        )
        for _stream, entries in result or ():
            self._absorb(entries)

    def __aiter__(self) -> "Consumer":
        return self

    async def __anext__(self) -> Message:
        while True:
            if self._buffer:
                return self._buffer.popleft()
            if self._closed:
                raise StopAsyncIteration
            await self._fill()

    async def __aenter__(self) -> "Consumer":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        self.close()


class RedisBackend:
    """Connection and stream handling for durable topics."""

    __slots__ = ("_clients", "_lock", "maxlen", "node_id", "prefix", "url")

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        prefix: str = "oxbrook:",
        maxlen: int | None = DEFAULT_MAXLEN,
    ) -> None:
        if not HAVE_REDIS:
            raise RuntimeError(
                "durable topics need the redis package: pip install 'oxbrook[redis]'"
            )
        self.url = url
        self.prefix = prefix
        self.maxlen = maxlen
        # Identifies this process so the tail can skip what it published
        # itself, which local subscribers already received directly.
        self.node_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}".encode()
        self._clients: dict[Any, Any] = {}
        self._lock = threading.Lock()

    def key(self, topic: str) -> str:
        return f"{self.prefix}{topic}"

    def client(self):
        """A client bound to the calling event loop.

        redis-py's async connections belong to the loop that opened them, and
        Oxbrook runs several worker loops, so each gets its own pool rather than
        sharing one that would break the moment a second loop touched it.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            existing = self._clients.get(loop)
            if existing is None:
                existing = aioredis.Redis.from_url(
                    self.url,
                    decode_responses=False,
                    retry=Retry(ExponentialBackoff(cap=1.0, base=0.05), CONNECT_RETRIES),
                    retry_on_error=[RedisConnectionError, RedisTimeoutError],
                )
                self._clients[loop] = existing
            return existing

    async def ensure_group(self, topic: str, group: str, start: str = "0") -> None:
        """Create the group if it does not exist. `start` of "0" means a new
        group sees everything still in the stream; "$" means only new messages.
        """
        try:
            await self.client().xgroup_create(
                self.key(topic), group, id=start, mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def publish(self, topic: str, value: Any) -> str:
        fields = {b"d": _encode(value), b"n": self.node_id}
        kwargs: dict[str, Any] = {}
        if self.maxlen:
            kwargs = {"maxlen": self.maxlen, "approximate": True}
        entry_id = await self.client().xadd(self.key(topic), fields, **kwargs)
        return entry_id.decode() if isinstance(entry_id, bytes) else entry_id

    async def tail(self, topic: str, start: str = "$", *, skip_own: bool = True):
        """Yield (id, value) for messages appended after `start`.

        Runs forever; cancel the task to stop it.
        """
        key = self.key(topic)
        last = start
        client = self.client()
        while True:
            result = await client.xread({key: last}, block=BLOCK_MS, count=64)
            for _stream, entries in result or ():
                for entry_id, fields in entries:
                    last = entry_id
                    if skip_own and fields.get(b"n") == self.node_id:
                        continue
                    ident = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                    yield ident, _decode(fields.get(b"d"))

    async def history(self, topic: str, count: int = 100, start: str = "-") -> list:
        """Recent messages, oldest first. For replay and for tests."""
        entries = await self.client().xrange(self.key(topic), min=start, count=count)
        return [
            (
                (eid.decode() if isinstance(eid, bytes) else eid),
                _decode(fields.get(b"d")),
            )
            for eid, fields in entries
        ]

    async def pending(self, topic: str, group: str) -> int:
        """Messages this group has been given but not had acked.

        A group-level question, so it needs no consumer. Creating one just to
        ask would add a member to the group that never reads anything.
        """
        try:
            info = await self.client().xpending(self.key(topic), group)
        except ResponseError:
            return 0  # the group does not exist yet
        return int(info["pending"]) if info else 0

    async def length(self, topic: str) -> int:
        return await self.client().xlen(self.key(topic))

    async def trim(self, topic: str, maxlen: int) -> int:
        return await self.client().xtrim(self.key(topic), maxlen=maxlen, approximate=False)

    async def close(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
