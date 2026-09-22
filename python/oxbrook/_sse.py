"""Server-Sent Events.

Return an `SSE` from a handler and Oxbrook streams it: headers go out
immediately, then every item the source yields becomes an event.

    @app.get("/feed")
    async def feed(_: Request):
        return SSE(app.topic("orders").subscribe())

The source is any async iterable, so a topic subscription is the common case
but an async generator works just as well.
"""

import re
from dataclasses import dataclass
from typing import Any

from ._schema import encode

#: `send_chunk` return codes, matching ChunkResult in src/responder.rs.
SENT = 0
FULL = 1
CLOSED = 2

SSE_HEADERS = [
    ("cache-control", "no-cache"),
    ("connection", "keep-alive"),
    # Tells nginx not to buffer the response, which would defeat the point.
    ("x-accel-buffering", "no"),
]


#: Every line terminator the event-stream format recognises. A client ends a
#: line at any of them, so all three have to be treated the same way here.
LINE_BREAK = re.compile(r"\r\n|\r|\n")


def _field(name: str, value: str) -> str:
    """Reject a field value that could break out of its own line.

    An `id` of `"1\n\ndata: ..."` does not produce an odd-looking id: the blank
    line ends the event and the rest becomes a second event the application
    never sent. Any value carrying a line break or a NUL is invalid in the
    format, so it is refused rather than quietly mangled.
    """
    if LINE_BREAK.search(value) or "\0" in value:
        raise ValueError(
            f"SSE {name} may not contain a line break or NUL: {value!r}. "
            f"Such a value would inject fields into the stream"
        )
    return value


@dataclass(slots=True)
class Event:
    """One event, when the defaults are not enough.

    Yield plain values for the common case; yield this to set a name, an id for
    resumption, or a client retry hint.

    `event` and `id` may not contain a line break or a NUL; both raise
    `ValueError`. Checked here so the traceback points at the code that built
    the event, and again at render time, because this is a mutable dataclass
    and the fields can be reassigned afterwards.
    """

    data: Any
    event: str | None = None
    id: str | None = None
    retry: int | None = None

    def __post_init__(self) -> None:
        if self.event is not None:
            _field("event", self.event)
        if self.id is not None:
            _field("id", str(self.id))


def _encode_data(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    return encode(data).decode()


def format_event(item: Any) -> bytes:
    """Render one item in the `text/event-stream` wire format."""
    if isinstance(item, Event):
        data, name, ident, retry = item.data, item.event, item.id, item.retry
    else:
        data, name, ident, retry = item, None, None, None

    lines: list[str] = []
    if name:
        lines.append(f"event: {_field('event', name)}")
    if ident is not None:
        lines.append(f"id: {_field('id', str(ident))}")
    if retry is not None:
        # Coerced rather than interpolated: the annotation says int, and a str
        # here would carry a line break straight onto the wire.
        lines.append(f"retry: {int(retry)}")
    # A payload containing line breaks has to become several data: lines, or
    # the break would terminate the event early and everything after it would
    # be parsed as a new one. Splitting on all three terminators matters: a
    # lone carriage return ends a line for the client too.
    for line in LINE_BREAK.split(_encode_data(data)):
        lines.append(f"data: {line}")
    return ("\n".join(lines) + "\n\n").encode()


class SSE:
    """A streaming `text/event-stream` response."""

    __slots__ = ("ping", "source", "status")

    def __init__(self, source: Any, *, ping: float | None = 15.0, status: int = 200) -> None:
        """`ping` sends a comment line when idle that long, which stops proxies
        and load balancers from closing an idle connection. None disables it."""
        if not hasattr(source, "__aiter__"):
            raise TypeError(
                f"SSE needs an async iterable, got {type(source).__name__}. "
                f"A topic subscription or an async generator both work"
            )
        self.source = source
        self.ping = ping
        self.status = status
