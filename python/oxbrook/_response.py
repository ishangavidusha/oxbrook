"""Explicit responses, for when returning a value is not enough.

Returning a dict or a model covers most handlers. This covers the rest: a
chosen status code, a content type that is not JSON, or bytes that are already
encoded and should not be touched.
"""

from dataclasses import dataclass, field
from typing import Any


def header_pairs(headers: dict[str, Any]) -> list[tuple[str, str]] | None:
    """Headers as the (name, value) pairs sent, None for none.

    A value that is a list or tuple sends the header once per item. That is
    how two `WWW-Authenticate` challenges go out: one header each, which every
    client parses, rather than one header holding both, which many do not.
    """
    if not headers:
        return None
    pairs: list[tuple[str, str]] = []
    for name, value in headers.items():
        if isinstance(value, (list, tuple)):
            pairs.extend((name, item) for item in value)
        else:
            pairs.append((name, value))
    return pairs


@dataclass(frozen=True, slots=True)
class Response:
    """A ready-to-send response.

    `body` may be bytes or str; str is encoded as UTF-8. `headers` are sent in
    addition to the content type; a list as a value sends that header once
    per item.
    """

    body: bytes | str = b""
    status: int = 200
    content_type: str = "application/json"
    headers: dict[str, Any] = field(default_factory=dict)

    def encoded(self) -> bytes:
        return self.body.encode() if isinstance(self.body, str) else bytes(self.body)

    def header_list(self) -> list[tuple[str, str]] | None:
        return header_pairs(self.headers)
