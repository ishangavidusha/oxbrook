"""Response compression.

    app = App(compression=Compression())

Applied in Rust, after the handler has answered, so it costs the worker loops
nothing. The client's `Accept-Encoding` picks between brotli and gzip; a
reply goes out compressed only when it is whole (not streamed), at least
`min_size` bytes, of a text-like content type, and not already encoded.
"""

from collections.abc import Iterable
from dataclasses import dataclass

_KNOWN = ("br", "gzip")


@dataclass(frozen=True, slots=True)
class Compression:
    """Compress responses for clients that accept it.

    `min_size` is the smallest body, in bytes, worth compressing. Below about a
    kilobyte the saving is a fraction of one network packet, and the work is
    not free.

    `encodings` lists the codings to offer, most preferred first. The client's
    weights decide; this order settles a tie. Brotli comes first because on
    JSON it comes out 12 to 15 percent smaller than gzip, for about half as
    much time again.

    `gzip_level` (1 to 9) and `brotli_quality` (0 to 11) trade time for size.
    The defaults suit compressing on every request. Brotli above 4 is meant
    for files compressed once ahead of time: from 5 on it costs ten times
    more for a few percent.

    A single reply opts out with a `Cache-Control: no-transform` header. Use
    it on a response that puts a secret and text an attacker controls in the
    same body, such as a page with a CSRF token that echoes a query parameter:
    compression lets the attacker learn the secret from the response's size
    (the BREACH attack). JSON APIs authenticated by a header are not exposed
    that way.
    """

    min_size: int = 1024
    encodings: Iterable[str] = _KNOWN
    gzip_level: int = 6
    brotli_quality: int = 4

    def __post_init__(self) -> None:
        if isinstance(self.encodings, str):
            raise TypeError("encodings is a list of codings, such as ['br', 'gzip']")
        encodings = tuple(e.lower() for e in self.encodings)
        unknown = [e for e in encodings if e not in _KNOWN]
        if unknown:
            raise ValueError(f"unknown encodings {unknown}; offer 'br', 'gzip' or both")
        if not encodings:
            raise ValueError("encodings is empty; offer 'br', 'gzip' or both")
        if isinstance(self.min_size, bool) or not isinstance(self.min_size, int):
            raise TypeError("min_size is a number of bytes")
        if self.min_size < 0:
            raise ValueError("min_size cannot be negative")
        if not 1 <= self.gzip_level <= 9:
            raise ValueError(f"gzip_level is 1 to 9, not {self.gzip_level}")
        if not 0 <= self.brotli_quality <= 11:
            raise ValueError(f"brotli_quality is 0 to 11, not {self.brotli_quality}")
        object.__setattr__(self, "encodings", tuple(dict.fromkeys(encodings)))

    def as_spec(self) -> tuple:
        """The tuple the Rust server takes."""
        return (self.min_size, self.gzip_level, self.brotli_quality, list(self.encodings))
