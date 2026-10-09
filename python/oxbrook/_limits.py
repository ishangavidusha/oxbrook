"""Rate limits, and which proxies to believe about the client's address.

    app = App(rate_limit=RateLimit("600/minute"), trusted_proxies=["10.0.0.0/8"])

    @app.post("/login", rate_limit=RateLimit("5/minute"))
    async def login(request): ...

Decided in Rust before a request reaches a worker loop (see `limit.rs`). The
budget lives in the `_core.Limiter` this object holds, so one `RateLimit`
used on several routes is one budget across all of them.
"""

import ipaddress
import re
from collections.abc import Sequence
from typing import Any

_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_RATE = re.compile(r"^\s*(\d+)\s*/\s*(second|minute|hour|day)s?\s*$")
_HEADER = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

#: Keeps `burst` intervals of the longest period inside a u64 of nanoseconds.
_MAX_BURST = 100_000
#: A microsecond between requests at the shortest period.
_MAX_COUNT = 1_000_000


class RateLimit:
    """At most `rate` requests per client, such as `"100/minute"`.

    `rate` is a count and one of `second`, `minute`, `hour` or `day`. The
    budget refills continuously rather than resetting at the top of each
    period: `"60/minute"` allows a request a second, plus up to `burst`
    saved up while the client was quiet. `burst` defaults to the count (at
    most 100,000), so a client that has been quiet can spend a whole period's
    budget at once.

    `key` is what counts as one client: `"client"`, the address, or
    `"header:<name>"`, the value of that header, such as an API key. A
    request without the header is counted by its address. Addresses come from
    the connection, or through the proxies `App(trusted_proxies=...)` names;
    IPv6 addresses count by their /64, since one host is normally given a
    whole block.

    Over the limit, the answer is `429` with `Retry-After`, before the
    request reaches a worker loop. One instance is one budget: the same
    `RateLimit` on two routes limits them together. Budgets are kept in
    memory, per process, so behind a load balancer each instance counts on its
    own.
    """

    __slots__ = ("_limiter", "burst", "key", "rate")

    def __init__(self, rate: str, *, burst: int | None = None, key: str = "client") -> None:
        from ._core import Limiter

        match = _RATE.match(rate) if isinstance(rate, str) else None
        if match is None:
            raise ValueError(
                f"rate must be like '100/minute': a count and one of second, minute, "
                f"hour or day, not {rate!r}"
            )
        count, unit = int(match[1]), match[2]
        if not 1 <= count <= _MAX_COUNT:
            raise ValueError(f"the count in a rate must be from 1 to {_MAX_COUNT}, not {count}")
        if burst is None:
            burst = min(count, _MAX_BURST)
        if isinstance(burst, bool) or not isinstance(burst, int) or not 1 <= burst <= _MAX_BURST:
            raise ValueError(
                f"burst must be a whole number from 1 to {_MAX_BURST}, not {burst!r}"
            )
        header = None
        if key != "client":
            name = key[len("header:"):] if isinstance(key, str) and key.startswith("header:") \
                else None
            if not name or not _HEADER.match(name):
                raise ValueError(f"key must be 'client' or 'header:<name>', not {key!r}")
            header = name.lower()
        self.rate = f"{count}/{unit}"
        self.burst = burst
        self.key = key if header is None else f"header:{header}"
        interval = _UNITS[unit] * 1_000_000_000 // count
        self._limiter = Limiter(interval, burst, header)

    def __repr__(self) -> str:
        return f"RateLimit({self.rate!r}, burst={self.burst}, key={self.key!r})"


def check(value: Any, where: str) -> Any:
    """A `rate_limit=` argument: a RateLimit, None, or left unset."""
    from ._auth import UNSET

    if value is UNSET or value is None or isinstance(value, RateLimit):
        return value
    raise TypeError(f"{where}: rate_limit must be a RateLimit(...) or None, "
                    f"got {type(value).__name__}")


def proxies(value: int | Sequence[str] | None) -> tuple[int, list[tuple[str, int]]]:
    """`App(trusted_proxies=...)` as the server takes it: (hops, networks)."""
    if value is None:
        return 0, []
    if isinstance(value, bool):
        raise TypeError("trusted_proxies is a number of proxies or a list of networks")
    if isinstance(value, int):
        if value < 1:
            raise ValueError("trusted_proxies as a number must be at least 1")
        return value, []
    if isinstance(value, str):
        raise TypeError("trusted_proxies is a list of networks, not a single string")
    networks = []
    for entry in value:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except (TypeError, ValueError):
            raise ValueError(f"trusted_proxies: {entry!r} is not an address or network") \
                from None
        networks.append((str(network.network_address), network.prefixlen))
    return 0, networks
