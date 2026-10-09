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
handler and nothing else.

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
the same address, and over the limit gets an error result carrying the same
`429` problem.

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

## Limits of the limits

Budgets are kept in memory, in each process. Behind a load balancer with
several instances, each counts on its own, so a client can make up to the
limit times the number of instances; set the limit with that in mind. A
restart starts every budget afresh.

Memory follows the clients seen recently, not every client ever seen: a
client's entry is dropped once its budget is full again.

A `429` from a limit is counted in the [metrics](metrics.md) under its route,
and in `oxbrook_requests_limited_total`.
