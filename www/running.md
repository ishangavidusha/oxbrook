# Running a server

```bash
oxbrook run main:app
```

The target is `module:attribute` — here, the `app` in `main.py` — imported from
the working directory. With no attribute, `app` is assumed. The same command is
installed as `oxb`, and runs as `python -m oxbrook`.

From Python, the same server with the same options:

```python
app.run(host="0.0.0.0", port=8000)
```

Every option can also be passed to `app.build_server(...)`, which prepares a
server without starting it — that is what the [test client](guide/testing.md)
uses.

| command line | `app.run` | default | what it does |
|---|---|---|---|
| `--host` | `host` | `127.0.0.1` | interface to bind |
| `--port` | `port` | `8000` | port to bind |
| `--workers` | `workers` | detected | Python worker loops |
| `--max-concurrency` | `max_concurrency` | 1024 | requests per worker, queued plus in-flight |
| `--max-connections` | `max_connections` | 2048 | sockets held open |
| `--max-body` | `max_body` | 16 MiB | largest request body, in bytes |
| `--max-message` | `max_message` | 16 MiB | largest WebSocket message, in bytes |
| `--request-timeout` | `request_timeout` | 30.0 | seconds to a handler's first response, then `504` and the handler is cancelled |
| `--shutdown-grace` | `shutdown_grace` | 10.0 | seconds a stop waits for in-flight requests |
| `--tls-cert` | `tls_cert` | none | PEM certificate chain; serve HTTPS |
| `--tls-key` | `tls_key` | none | PEM private key for that certificate |
| `--no-http2` | `http2=False` | HTTP/2 on | serve HTTP/1.1 only |

The command also takes `--access-log`, to log every request, and
`--log-level`, which sets the level of Oxbrook's own loggers.

## Reloading during development

```bash
oxbrook run main:app --reload
```

Saving a Python file under the working directory restarts the server. Each
restart is a new process, not modules re-imported in place: the Rust extension
cannot be reloaded inside a running interpreter, and a fresh process runs the
[lifespans](guide/lifespan.md) again, so after a reload the app is in exactly
the state a deploy would leave it in.

- `--reload-dir DIR` watches another directory, and can be repeated.
- `--reload-include '*.html'` restarts for other files too.
- Hidden directories, `__pycache__`, `node_modules`, `venv`, `target`, `site`,
  `dist` and `build` are never watched.

A syntax error stops the server, not the watcher: the error is printed, and the
next save starts it again. A server being replaced gets one second to finish its
requests rather than the usual ten, so an open SSE stream in a browser does not
hold every restart; `--shutdown-grace` overrides that.

`--reload` is for development. It polls files for changes and runs a second
process to do it.

## Inspecting an app

```bash
oxbrook routes main:app          # every route, including the built-in ones
oxbrook routes main:app --json
oxbrook openapi main:app -o openapi.json
```

`routes` marks tools, WebSockets, streaming bodies, forms and router middleware,
and lists `/openapi.json`, `/docs` and `/mcp` as the server would serve them.
`openapi` writes the document without starting a server, for generating clients
in CI.

For an app built by a function, pass `--factory`: `oxbrook run main:create_app
--factory`. A target that cannot be loaded exits with status 2 and says why;
an exception raised while importing the app shows its traceback.

## Worker loops

Each worker is one OS thread running one asyncio loop. They share the process,
which is what lets a topic reach subscribers on all of them.

The default is measured, not guessed:

- **On a GIL build, always one.** One loop was best or tied at every handler
  cost in the sweep; extra loops only cost throughput.
- **On a free-threaded build, one is never best.** Even a handler that does
  nothing gains from more loops, and a handler doing 500µs of work gains 3.4x.
  Throughput peaks around the performance-core count and falls off once the
  efficiency cores are oversubscribed.

So the default is "how many cores can actually run Python in parallel", which
is not `os.cpu_count()`: that counts efficiency cores, and inside a container it
reports the host's cores rather than the cgroup quota — which would start
dozens of loops for a two-CPU limit. Oxbrook probes cgroup quotas, CPU affinity
and performance-core counts, takes the most constrained answer, and caps it at
8.

The cap is a guard against an absurd probe result, not a measured ceiling.
Raise it explicitly on a large homogeneous machine with CPU-heavy handlers:

```python
app.run(workers=16)
```

!!! note "More loops is for CPU, not for I/O"

    Awaiting I/O does not need more loops. An `await` yields the loop, so one
    loop holds thousands of them. What needs more loops is CPU time spent
    inside the handler.

## Backpressure

Each worker accepts at most `max_concurrency` requests at once, counting both
those queued and those already running. Past that the request tries another
worker, and if every worker is full the server answers `503` with
`Retry-After: 1`.

```python
app.run(max_concurrency=1024)   # the default, per worker loop
```

**Counting in-flight requests is the part that matters.** The drain callback
empties the queue into asyncio tasks immediately, so a handler that awaits I/O
leaves the queue near empty while thousands of requests pile up inside the
loop. Bounding the queue alone would look like backpressure and protect
nothing.

Lower it for slow handlers, where a deep backlog only adds latency before an
inevitable client timeout. Raise it to absorb larger bursts of fast requests.

`max_connections` is a separate limit, because an idle keep-alive connection
costs a file descriptor without ever reaching a worker. An open WebSocket
counts as the connection it was upgraded from, until it closes. At that limit the
server stops accepting rather than refusing, so the wait lands in the OS
backlog where a client's own connect timeout governs it.

## HTTPS and HTTP/2

```bash
oxbrook run main:app --host 0.0.0.0 --port 443 \
    --tls-cert /etc/certs/fullchain.pem --tls-key /etc/certs/privkey.pem
```

```python
app.run(host="0.0.0.0", port=443,
        tls_cert="/etc/certs/fullchain.pem", tls_key="/etc/certs/privkey.pem")
```

The certificate file holds the chain, the server's own certificate first, as
Let's Encrypt's `fullchain.pem` does; the key is PKCS#8, PKCS#1 or SEC1 PEM.
Both are read once, when the server starts, and checked against each other: a
missing file, an empty one or a key that belongs to another certificate stops
the server with a message naming the file, before it binds. TLS 1.2 and 1.3
are served; nothing older.

With TLS, the port speaks HTTPS only. A plain `http://` request to it fails,
so redirecting HTTP to HTTPS takes a second listener or a proxy.

**HTTP/2 is on by default**, alongside HTTP/1.1, and needs nothing from the
app: routes, middleware, streaming bodies, SSE and static files behave the
same. Over TLS a client asks for it during the handshake (ALPN), which is what
every browser does. On a plain port the server recognises HTTP/2 from a
connection's first bytes, but only clients configured for HTTP/2 without TLS,
such as `curl --http2-prior-knowledge` or `httpx.Client(http1=False,
http2=True)`, will use it; browsers never do. `http2=False`, or `--no-http2`,
serves HTTP/1.1 alone and stops offering HTTP/2 during the handshake.

The `:authority` of an HTTP/2 request is copied into the `host` header when the
client sent none, so a handler reads the host the same way over either
protocol.

What HTTP/2 does not change:

- **WebSockets use HTTP/1.1.** The server does not offer WebSockets over
  HTTP/2, so a browser opens a separate HTTP/1.1 connection for a socket, as it
  does for any server that does not offer them. A socket route asked over
  HTTP/2 answers `426`.
- **Capacity limits are the same.** One HTTP/2 connection can carry many
  requests at once, and each one still counts against `max_concurrency`.

Limits specific to connections:

| limit | value | why |
|---|---|---|
| TLS handshake | 15 s | a client that connects and never finishes the handshake would hold a slot |
| request headers, HTTP/1.1 | 15 s | the same for headers sent slowly; also closes an idle keep-alive connection |
| idle HTTP/2 connection | 15–20 s | with nothing in flight, the server sends `GOAWAY`; a client that does not acknowledge it is dropped 15 s later |
| streams per HTTP/2 connection | 200 | concurrent requests one client may have open |
| handlers per HTTP/2 connection | 200 | including handlers whose stream the client reset; beyond it, new requests get `503` |

The last row exists because a client can reset a stream the moment it opens
it (CVE-2023-44487, "rapid reset"). The reset [cancels the
handler](guide/errors.md#when-the-client-leaves), but only once its worker gets
to it, and a handler with `cancel_on_disconnect=False` is not cancelled at all;
counting handlers until they actually end keeps one connection from starting
more of them than its share.

Not supported: reloading a renewed certificate without a restart, client
certificates, HTTP/3, and the `Upgrade: h2c` handshake from HTTP/1.1. A
certificate renewed on disk takes effect at the next start; with `--reload` in
development, `--reload-include '*.pem'` does that.

## In front of it

A terminating proxy in front — nginx, Caddy, a cloud load balancer — is still
the usual arrangement in production: it renews certificates, redirects plain
HTTP, and holds slow clients. Oxbrook's own TLS suits a service with no proxy
at all, an internal service that must still be encrypted, and development
against a browser feature that requires HTTPS.

WebSocket upgrades are accepted from the app's own origin, which is recognised
through `Host` or `X-Forwarded-Host`. A proxy that rewrites `Host` without
setting `X-Forwarded-Host` makes every browser socket look cross-origin and
refused with `403`; forward the original host, or list the public origin in
`websocket_origins`.

A proxy or CDN can serve [static files](guide/static.md) itself, with its own
cache, and pass everything else through; for heavy static traffic that is still
the better arrangement.

If the proxy also adds CORS headers, configure CORS in one place only: two
`Access-Control-Allow-Origin` headers on one response make a browser reject it.
A proxy that buffers request bodies defeats a [streaming
upload](guide/forms.md#streaming-a-body); turn request buffering off for those
routes (in nginx, `proxy_request_buffering off`).

## Containers

The repository ships a multi-stage `Dockerfile` that installs free-threaded
3.14 with `uv`, builds the wheel, and installs it into a slim runtime image.
The official Python images carry no free-threaded interpreter, so installing it
explicitly keeps the container and the development machine on the same
interpreter.

```bash
make image     # build oxbrook:dev
make stack     # two nodes against one redis
make down      # stop everything
```

Bind to `0.0.0.0` inside a container. Anything listening only on loopback is
unreachable from outside it.

```dockerfile
CMD ["oxbrook", "run", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Use the exec form, as above, so the server is the process that receives the
container's stop signal. Wrapped in a shell, the shell receives it instead.

!!! warning "Benchmark on the Docker network, not through a published port"

    Traffic from macOS into Docker Desktop's VM through `-p` measured **3.3x
    slower** than native, and the cost is the port forwarding, not the
    container: with the load generator in a second container on the same
    network, the same image measured faster than native. See
    [performance](design/performance.md#containers).

## Shutdown

Ctrl-C (`SIGINT`) and `SIGTERM` both stop the server gracefully: it stops
accepting, waits up to `shutdown_grace` for in-flight requests, runs the
lifespan teardown, then exits with status 0. Streams and sockets are closed.

On Windows, which has no `SIGTERM`, the same drain runs on Ctrl-C, on
Ctrl-Break — what `oxbrook run --reload` sends its server — and when the
console window closes, though Windows allows only a few seconds for the last
of these before it ends the process regardless.

`SIGTERM` is what `docker stop`, Kubernetes and systemd send, so a deploy drains
requests rather than cutting them off. Set the orchestrator's own grace period —
`terminationGracePeriodSeconds`, `docker stop --time` — longer than
`shutdown_grace`, or it kills the process before the drain ends.
