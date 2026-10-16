"""Cross-origin resource sharing.

    app = App(cors=CORS(allow_origins=["https://app.example.com"]))

Applied in Rust, to every response the server sends — including the `404`,
`413`, `503` and `504` it answers without waking a worker, which a middleware
would never see. A preflight is answered in Rust too.

`allow_origins` is the decision that matters and has no default. Methods and
request headers default to any, because permitting them does not widen what a
foreign site can do once its origin is allowed; the origin list and
`allow_credentials` are the boundary.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

#: A host label with one `*`: what a pattern's first label may be.
_WILD_LABEL = re.compile(r"[a-z0-9-]*\*[a-z0-9-]*")


def check_origin(value: str) -> str:
    if value == "*":
        return value
    if not isinstance(value, str) or "://" not in value or value.endswith("/"):
        raise ValueError(
            f"CORS origin {value!r} must be a scheme and host with no trailing "
            f"slash, like 'https://app.example.com', or '*'"
        )
    if "*" in value:
        _check_pattern(value)
    return value


def _check_pattern(value: str) -> None:
    """A `*` in an origin is allowed in one place: the first label of the
    host, where it stands for one label's worth of letters, digits and
    hyphens. Refused anywhere else, and with too little after it to say whose
    domain it is."""
    scheme, _, authority = value.partition("://")
    host = authority.rsplit(":", 1)[0] if authority.count(":") == 1 else authority
    labels = host.lower().split(".")
    if (scheme.count("*") or not _WILD_LABEL.fullmatch(labels[0])
            or any("*" in label for label in labels[1:]) or len(labels) < 3
            or not all(labels[1:])):
        raise ValueError(
            f"origin pattern {value!r}: a '*' may stand only in the first label of the "
            f"host, with at least two labels after it, like 'https://*.example.dev' or "
            f"'https://app-*.example.dev'"
        )


@dataclass(frozen=True, slots=True)
class CORS:
    """Which other origins may call this app from a browser.

    `allow_origins` lists exact origins — scheme, host and port as the browser
    sends them — or `["*"]` for any. An origin may hold one `*` in the first
    label of its host, for preview deployments: `https://*.example.dev`
    matches `https://pr-12.example.dev`, and `https://app-*.example.dev`
    matches `https://app-pr-12.example.dev`. The `*` covers one label, never
    a dot, so neither matches `https://example.dev.attacker.com`. On a
    shared domain such as a hosting platform's, a pattern admits whoever can
    get a matching name there: keep the part that is yours in it.

    `allow_credentials` lets the browser send cookies and `Authorization` on
    those requests. It cannot be combined with `"*"`: that would let every
    website make authenticated requests as a signed-in user, so it raises here
    rather than being quietly made to work.

    `expose_headers` lists response headers a page's script may read, beyond
    the few a browser always exposes. `max_age` is how long, in seconds, a
    browser may cache a preflight answer.
    """

    allow_origins: Iterable[str]
    allow_methods: Iterable[str] = ("*",)
    allow_headers: Iterable[str] = ("*",)
    allow_credentials: bool = False
    expose_headers: Iterable[str] = field(default_factory=tuple)
    max_age: int | None = 600

    def __post_init__(self) -> None:
        if isinstance(self.allow_origins, str):
            raise TypeError("allow_origins is a list of origins, not a single string")
        origins = tuple(check_origin(o) for o in self.allow_origins)
        if not origins:
            raise ValueError("allow_origins is empty; list at least one origin")
        if "*" in origins and self.allow_credentials:
            raise ValueError(
                "allow_credentials=True with any origin would let every website make "
                "authenticated requests as your users. List the origins instead"
            )
        if self.max_age is not None and self.max_age < 0:
            raise ValueError("max_age cannot be negative")
        object.__setattr__(self, "allow_origins", origins)
        object.__setattr__(self, "allow_methods", tuple(m.upper() for m in self.allow_methods))
        object.__setattr__(self, "allow_headers", tuple(h.lower() for h in self.allow_headers))
        object.__setattr__(self, "expose_headers", tuple(self.expose_headers))

    def as_spec(self) -> tuple:
        """The tuple the Rust server takes."""
        return (
            list(self.allow_origins),
            list(self.allow_methods),
            list(self.allow_headers),
            self.allow_credentials,
            list(self.expose_headers),
            self.max_age,
        )
