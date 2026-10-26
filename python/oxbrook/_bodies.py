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

**A multipart upload can be read a part at a time.** `body.parts()` parses
`multipart/form-data` as it arrives, so a file goes to disk without the body
or the file ever being held whole:

    @app.post("/uploads")
    async def upload(request: Request, body: BodyStream):
        async for part in body.parts():
            if part.filename is None:
                fields[part.name] = await part.text()
                continue
            with open(safe_path(part.filename), "wb") as out:
                async for chunk in part:
                    out.write(chunk)
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

    __slots__ = ("_buffered", "_content_type", "_reader")

    def __init__(
        self, reader: Any, buffered: bytes = b"", content_type: str | None = None
    ) -> None:
        self._reader = reader
        self._buffered = buffered
        self._content_type = content_type

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

    def parts(self, *, max_parts: int = 1000) -> "Parts":
        """The body as `multipart/form-data`, one `Part` at a time as it arrives.

        Parsed by the same parser as `request.form()`, so the same bodies are
        accepted. A body that is not multipart raises `HTTPError(415)`, a
        malformed one `HTTPError(400)`, and more than `max_parts` parts
        `HTTPError(413)`. Iterating the parts consumes the body.
        """
        from ._core import PartReader

        return Parts(self, PartReader(self._content_type, max_parts))

    def __repr__(self) -> str:
        return f"<BodyStream {'streaming' if self._reader is not None else 'buffered'}>"


class Part:
    """One part of a multipart body read by `BodyStream.parts()`.

    Its data is an async iterator of `bytes`, valid until the next part is
    asked for; whatever was not read by then is skipped.
    """

    __slots__ = ("_parts", "content_type", "filename", "headers", "name")

    def __init__(
        self, parts: "Parts", name: str, filename: str | None,
        content_type: str | None, headers: dict[str, str],
    ) -> None:
        #: The form field's name.
        self.name = name
        #: None for an ordinary field. For a file, as the client sent it: never
        #: use it as a filesystem path unchecked, since it may be empty or
        #: contain `..` and slashes.
        self.filename = filename
        self.content_type = content_type
        #: The part's own headers, names lowercased.
        self.headers = headers
        self._parts = parts

    def __aiter__(self) -> "Part":
        return self

    async def __anext__(self) -> bytes:
        parts = self._parts
        if parts._current is not self:
            raise RuntimeError(
                f"part {self.name!r} was read after the next part was asked for; "
                f"read each part before moving on"
            )
        while True:
            state, value = parts._reader.read()
            if state == _READY:
                return value
            if state == _GONE:
                raise StopAsyncIteration
            await parts._pull()

    async def read(self) -> bytes:
        """The rest of this part. Held in memory, bounded by `max_body`."""
        return b"".join([chunk async for chunk in self])

    async def text(self, encoding: str = "utf-8") -> str:
        """The rest of this part as text. Undecodable bytes become replacement
        characters, as they do for a field in `request.form()`."""
        return (await self.read()).decode(encoding, errors="replace")

    def __repr__(self) -> str:
        return f"Part(name={self.name!r}, filename={self.filename!r})"


#: Mirrors the states in src/form.rs.
_READY, _WAITING, _GONE = 0, 1, 2


class Parts:
    """The parts of a multipart body, as an async iterator of `Part`."""

    __slots__ = ("_body", "_current", "_ended", "_reader")

    def __init__(self, body: BodyStream, reader: Any) -> None:
        self._body = body
        self._reader = reader
        self._current: Part | None = None
        self._ended = False

    def __aiter__(self) -> "Parts":
        return self

    async def __anext__(self) -> Part:
        self._current = None
        while True:
            state, value = self._reader.next_part()
            if state == _READY:
                name, filename, content_type, headers = value
                self._current = Part(self, name, filename, content_type, dict(headers))
                return self._current
            if state == _GONE:
                raise StopAsyncIteration
            await self._pull()

    async def _pull(self) -> None:
        """Feed the parser the body's next chunk, or tell it there are none."""
        if self._ended:
            # The parser was told the body ended and still wants more: it
            # should have refused the body itself, but must not spin here.
            raise HTTPError(400, "malformed form body: incomplete")
        try:
            chunk = await self._body.__anext__()
        except StopAsyncIteration:
            self._ended = True
            self._reader.finish()
            return
        self._reader.feed(chunk)


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
        # Another read of this request, awaited at the same time, may have
        # taken the body while this one waited: what it kept is the answer.
        if request._reader() is None:
            return request.body
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
