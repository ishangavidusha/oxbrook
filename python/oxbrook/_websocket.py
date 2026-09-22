"""The handler's view of a WebSocket connection.

A tokio task owns the socket and does the framing. This wraps the Rust bridge
in the async API a handler actually wants:

    @app.websocket("/ws")
    async def echo(request, ws):
        async for message in ws:
            await ws.send(message)

Text arrives as `str` and binary as `bytes`, so the two are told apart without
a wrapper object per message. Iteration ends when the peer closes.

Waiting costs a callback only when the handler is idle. A socket delivering
faster than the handler reads never registers one, which is the same
coalescing rule used for request dispatch and for topics.
"""

import asyncio
import json
from typing import Any

from ._schema import encode


class WebSocketClosed(Exception):
    """A send was attempted after the peer went away."""


def _resolve(future: asyncio.Future) -> None:
    if not future.done():
        future.set_result(None)


class WebSocket:
    """An open connection. Async-iterable over incoming messages."""

    __slots__ = ("_core", "_loop")

    def __init__(self, core: Any) -> None:
        self._core = core
        self._loop = asyncio.get_running_loop()

    @property
    def closed(self) -> bool:
        return self._core.closed

    async def receive(self) -> str | bytes | None:
        """Next message, or None once the peer has closed."""
        while True:
            message = self._core.try_receive()
            if message is not None:
                return message
            if self._core.closed:
                return None

            waiter = self._loop.create_future()
            # `notify` fires immediately if something arrived in the meantime,
            # so this cannot miss a message that landed during the check above.
            # The lambda captures `waiter`, which the loop rebinds — safe here
            # because the coroutine cannot reach the rebinding until the await
            # below returns, and it only returns once this callback has run.
            self._core.notify(self._loop, lambda: _resolve(waiter))  # noqa: B023
            await waiter

    async def receive_json(self) -> Any:
        message = await self.receive()
        if message is None:
            raise WebSocketClosed("connection closed while waiting for a message")
        if isinstance(message, bytes):
            message = message.decode()
        return json.loads(message)

    async def send(self, data: Any) -> None:
        """Send a message.

        `str` goes as text and `bytes` as binary. Anything else is serialized
        to JSON, which covers dicts and pydantic models.
        """
        if isinstance(data, str):
            sent = self._core.send_text(data)
        elif isinstance(data, (bytes, bytearray, memoryview)):
            sent = self._core.send_bytes(bytes(data))
        else:
            # Text, not binary, for a model and a dict alike: both are JSON,
            # and frame type should not depend on which was sent.
            sent = self._core.send_text(encode(data).decode())

        if not sent:
            raise WebSocketClosed("socket is closed or its send buffer is full")

    async def send_json(self, data: Any) -> None:
        await self.send(data)

    async def close(self) -> None:
        """Start a clean close handshake."""
        self._core.close()

    def __aiter__(self) -> "WebSocket":
        return self

    async def __anext__(self) -> str | bytes:
        message = await self.receive()
        if message is None:
            raise StopAsyncIteration
        return message
