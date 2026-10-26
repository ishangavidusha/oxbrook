# Rate limits

```python
from oxbrook import App, RateLimit

app = App(rate_limit=RateLimit("600/minute"), trusted_proxies=["10.0.0.0/8"])

@app.post("/login", rate_limit=RateLimit("5/minute"))
async def login(request): ...
```

A client over a limit is answered `429 Too Many Requests` with `Retry-After`,
before its request reaches a worker loop: no body is read, no handler or
middleware runs. A limit that let the request in first would protect the
handler and nothing else. The one exception is a limit [per
user](#per-user), which has to wait for authentication.

[`max_concurrency`](../running.md#backpressure) protects the server from too
much work in total. A rate limit protects it, and the other clients, from one
client taking that capacity for itself.

## Rates

`RateLimit("100/minute")` is a count and one of `second`, `minute`, `hour` or
`day`. The budget refills continuously rather than resetting at the top of each
period: `"60/minute"` gives a client a request a second, and it can save up to
`burst` of them while it is quiet. `burst` defaults to the count (at most
100,000), so a client that has been away can spend a whole period's budget at
once.

```python
RateLimit("10/second", burst=1)   # evenly spaced, no bursts
RateLimit("1000/hour", burst=50)  # a thousand an hour, at most fifty at once
```

`Retry-After` is the whole number of seconds until the next request would be
allowed, rounded up.

## Where a limit applies

- `App(rate_limit=...)` covers every request a client makes, to any route,
  including requests that match no route: a scanner walking paths is the
  client it is for.
- `rate_limit=` on a route, or on a [router](routers.md), adds a limit of its
  own on top. The nearest declaration wins, as with `auth=`, and
  `rate_limit=None` exempts a route from its router's.

**One `RateLimit` is one budget.** The same object on two routes limits them
together; a router's limit is shared by all its routes. For separate budgets,
make separate objects.

```python
writes = RateLimit("30/minute")

@app.post("/posts", rate_limit=writes)
async def create(request): ...

@app.delete("/posts/{post_id}", rate_limit=writes)
async def remove(request, post_id: int): ...
```

A request refused by its route's limit has still spent from the app's: it was a
request.

A route's limit applies however it is reached. An agent calling the route as an
[MCP tool](../agents.md) spends from the same budget as an HTTP client at
the same address, or as the same user for a [per-user](#per-user) limit, and
over the limit gets an error result carrying the same `429` problem.

[Health probes](health.md), the [metrics](metrics.md) endpoint and CORS
preflights are never limited.

## Who counts as one client

By default, an address. An IPv6 address counts by its /64, the block a single
host or customer is normally given: counted by the full address, one machine
could rotate through more addresses than any limit.

`key="header:<name>"` counts by a header's value instead, such as an API key:

```python
@app.get("/search", rate_limit=RateLimit("100/minute", key="header:x-api-key"))
async def search(request, q: str): ...
```

A request without the header is counted by its address. Combine a header key
with an app-wide limit by address: anyone can invent a new header value, so
the address limit is what stops a client that sends a fresh one each time.

### Per user

`key="principal"` counts by who [`auth=`](auth.md) found: the scheme and the
principal's subject. One user is one budget whichever of their API keys,
tokens or sessions a request carries, and from whatever address:

```python
import os

from oxbrook.auth import JWT

auth = JWT(key=os.environ["JWT_SECRET"], algorithms=["HS256"], audience="notes")
app = App(auth=auth, rate_limit=RateLimit("600/minute"))

@app.post("/reports", rate_limit=RateLimit("20/hour", key="principal"))
async def create_report(request): ...
```

A request with no principal is counted by its address: an anonymous caller on
an [`optional`](auth.md) route, a request to a route without `auth=`, and a
credential that was refused, so guessing keys or tokens is limited as well.
A caller authenticated but refused a scope or role has still spent from their
own budget.

Who the caller is is known only once authentication has run, so this limit is
checked on a worker loop, after authentication and before the body is read or
any route middleware runs. A refused request has cost that much, which is why
the example keeps an app-wide limit by address in front of it: that one is
still checked before the loop.

## Behind a proxy

Behind a proxy or load balancer, every request arrives from the proxy's
address, and one limit by address would cover every client at once.
`trusted_proxies` says which peers to believe about the real one:

```python
App(trusted_proxies=["10.0.0.0/8", "fd00::/8"])   # these networks are proxies
App(trusted_proxies=1)                             # one proxy, address unknown
```

With networks, a request from a trusted peer is credited to the nearest
address in `X-Forwarded-For` outside them. Each proxy appends to that header,
so it is read from the right; anything further left is what the client chose
to send, and is ignored. With a number, the client is that many entries from
the right, for a platform whose proxy addresses are not known in advance.
A request with fewer entries than that did not come through the proxies, and
is credited to the connection.

Without `trusted_proxies`, the header is ignored entirely. Setting it while
the server is also reachable directly lets anyone choose their own address.

The same address is `request.client` in a handler.

## Across instances

Budgets are kept in memory, in each process. Behind a load balancer with
several instances each counts on its own, so a client can make up to the
limit times the number of instances, and a restart starts every budget
afresh. A limit kept in Redis is one budget everywhere:

```python
api = RateLimit("600/minute", redis_url="redis://redis:6379", name="api")
app = App(rate_limit=api)
```

Every process that declares a limit with the same `name` spends from the same
budget, by the same key: address, header or caller. `name` is required, so two
limits that happen to share a rate never share a budget by accident. Redis is
an optional dependency: `pip install 'oxbrook[redis]'`.

Each process still checks its own in-memory budget first, before a worker loop
sees the request, so a client already over the limit there costs nothing
more. A request that passes then spends from the shared budget on a worker
loop, before the handler runs and before the body of an authenticated route
is read. That is one Redis round trip per allowed request, and it means the
shared half applies to requests that reach a route: an unknown path or a
static file is counted only by the in-memory half. The budget is kept against
Redis's clock, so instances whose clocks disagree still agree on it.

If Redis cannot be reached, the request is let through and the failure is
logged, at most once a minute per limit. A limit that refused every request
while Redis was down would turn a Redis outage into an outage of the whole
API; the in-memory half still applies meanwhile.

## Limits of the limits

Memory follows the clients seen recently, not every client ever seen: a
client's entry is dropped once its budget is full again.

A `429` from a limit is counted in the [metrics](metrics.md) under its route,
and in `oxbrook_requests_limited_total`.
