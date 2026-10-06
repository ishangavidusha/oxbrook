# Health checks

```python
from oxbrook import App, Health

app = App(health=Health())
```

This serves two probes, for a load balancer or an orchestrator such as
Kubernetes:

- `GET /livez` — is the process able to serve, or should it be restarted
- `GET /readyz` — should it be sent traffic right now

Both answer `200` or `503` with a small JSON body, `Cache-Control: no-store`,
and `HEAD` as well as `GET`. They are public even in an app declared
[`App(auth=...)`](auth.md#declaring-it), they are not in the
[OpenAPI document](../openapi.md), and they are never agent tools.

## Liveness

`/livez` fails in one case: a worker loop has had requests waiting for longer
than `stall_after` seconds (10 by default) without getting back to them. That
is a loop that something is blocking — a handler calling a blocking function
without [`blocking=True`](blocking.md), a CPU-bound loop that never awaits, a
deadlock — and a restart is the fix.

```json
{"status": "stalled", "loops": [2]}
```

It does not fail because the server is busy. Hundreds of requests awaiting a
database are a loaded server, not a broken one, and a liveness probe that fails
under load gets healthy instances restarted, which moves their load onto the
others and can take them down in turn.

The server answers `/livez` itself, without a worker loop, so the probe still
answers when every loop is stuck.

## Readiness

`/readyz` fails while the server is [draining](#draining), when a loop has
stalled, and when a check fails.

```python
health = Health()

@health.check
async def database(state):
    async with state.pool.acquire() as connection:
        await connection.execute("select 1")

app = App(health=health, worker_lifespan=open_pool)
```

A check is an `async` function that takes the loop's state: what
[`lifespan` and `worker_lifespan`](lifespan.md) yielded. It fails by raising or
by returning `False`.

```json
{"status": "unavailable", "checks": {"database": "failed"}}
```

**Every check runs on every worker loop**, each against that loop's own state.
A pool opened in `worker_lifespan` belongs to the loop that opened it, so a
probe that ran the check only on whichever loop it reached would vouch for
pools it never looked at. The checks on all loops run at once and together have
`timeout` seconds (1 by default) to finish; one that does not finish counts as
`"timeout"`. A check's exception is logged under `oxbrook.health` and never
included in the response.

Keep checks cheap and about this instance. A check that calls another service
makes every instance unready when that service is down, and a load balancer
with no ready instance has nowhere to send anything.

Without checks, `/readyz` is answered by the server itself, like `/livez`.

## Draining

```python
app = App(health=Health(drain_delay=5))
```

When a server is asked to stop, it normally stops accepting at once. Behind a
load balancer that loses requests: the balancer takes a few seconds to notice
that the instance is leaving, and the connections it opens in that time are
refused.

With `drain_delay`, a stop first fails readiness and keeps serving for that
many seconds — new connections included — and only then stops accepting and
finishes what is in flight, as [`shutdown_grace`](../running.md#shutdown)
describes. A second Ctrl-C or `SIGTERM` ends the delay early.

On Kubernetes, set `drain_delay` to a little more than the readiness probe's
`periodSeconds` times its `failureThreshold`, and
`terminationGracePeriodSeconds` to more than `drain_delay` plus
`shutdown_grace`:

```yaml
livenessProbe:
  httpGet: {path: /livez, port: 8000}
  periodSeconds: 10
  failureThreshold: 3
readinessProbe:
  httpGet: {path: /readyz, port: 8000}
  periodSeconds: 2
  failureThreshold: 2
terminationGracePeriodSeconds: 30
```

## Options

| option | default | |
|---|---|---|
| `live` | `"/livez"` | liveness path, or `None` for none |
| `ready` | `"/readyz"` | readiness path, or `None` for none |
| `stall_after` | `10.0` | seconds a loop may leave requests waiting before liveness fails |
| `timeout` | `1.0` | seconds all readiness checks together may take |
| `drain_delay` | `0.0` | seconds a stop keeps serving, unready, before it stops accepting |
