"""Streaming request bodies.

    @app.put("/uploads/{name}")
    async def upload(request: Request, name: str, body: BodyStream):
        size = 0
        with open(safe_path(name), "wb") as out:
            async for chunk in body:
                out.write(chunk)
                size += len(chunk)
        return {"stored": size}

An argument annotated `BodyStream` makes the route stream its body: the server
hands the request to the handler before reading it, and chunks arrive as the
client sends them. Every other route receives its body whole.

**Nothing is read until the first chunk is asked for.** A handler that checks
authorisation first and answers `401` never makes the client send the body.

**The body is bounded as it arrives.** Past `max_body` the iterator raises
`HTTPError(413)`. A client that stops sending for longer than `request_timeout`
raises `HTTPError(408)`; a connection that ends early raises `HTTPError(400)`.
Unhandled, each becomes that response.

**Reading is back-pressured.** About a megabyte waits between the socket and
the handler; beyond that the server stops reading until the handler catches up,
and TCP pushes back on the client.

**The timeout follows progress.** For a streaming route `request_timeout` counts
from the last chunk that moved, not from the start, so a large upload that keeps
going is not cut off.

Read the body before responding. Once the response is sent the request is over,
and a handler still reading gets `HTTPError(400)`.
"""

import asyncio
from typing import Any

from ._errors import HTTPError

#: Mirrors the constants in src/body.rs.
_DATA, _PENDING, _END, _FAILED = 0, 1, 2, 3


class BodyStream:
    """The request body, as an async iterator of `bytes`.

    Use as a handler argument's annotation to make a route stream, or call
    `request.stream()`. On a route that did not stream, it yields the
    already-collected body once.
    """

    __slots__ = ("_buffered", "_reader")

    def __init__(self, reader: Any, buffered: bytes = b"") -> None:
        self._reader = reader
        self._buffered = buffered

    def __aiter__(self) -> "BodyStream":
        return self

    async def __anext__(self) -> bytes:
        if self._reader is None:
            if self._buffered:
                chunk, self._buffered = self._buffered, b""
                return chunk
            raise StopAsyncIteration

        while True:
            state, value = self._reader.poll()
            if state == _DATA:
                return value
            if state == _END:
                raise StopAsyncIteration
            if state == _FAILED:
                status, detail = value
                raise HTTPError(status, detail)

            loop = asyncio.get_running_loop()
            ready = loop.create_future()

            def wake(future: asyncio.Future = ready) -> None:
                if not future.done():
                    future.set_result(None)

            self._reader.notify(wake)
            await ready

    async def read(self) -> bytes:
        """The whole remaining body. Bounded by `max_body`, like any body."""
        return b"".join([chunk async for chunk in self])

    def __repr__(self) -> str:
        return f"<BodyStream {'streaming' if self._reader is not None else 'buffered'}>"


async def read_body(request: Any) -> bytes:
    """`request.read()`: the whole body, reading it first if it is still due.

    A deferred body is collected in Rust and the worker woken once, when it
    has all arrived, rather than once per chunk as iterating would.
    """
    reader = request._reader()
    if reader is None:
        return request.body
    loop = asyncio.get_running_loop()
    while True:
        state, value = reader.take()
        if state == _END:
            request._fill(value)
            return value
        if state == _FAILED:
            status, detail = value
            raise HTTPError(status, detail)

        ready = loop.create_future()

        def wake(future: asyncio.Future = ready) -> None:
            if not future.done():
                future.set_result(None)

        reader.collect(wake)
        await ready
