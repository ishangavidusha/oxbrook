# Performance

## Method

`bench/run.py` starts each target as a subprocess, waits for the port, warms it
up for 2 seconds, then measures with [`oha`](https://github.com/hatoo/oha) at 64
connections for 8 seconds. `raw` targets are a bare ASGI callable returning a
pre-encoded body, which is the best case for the comparison servers. `Nw` means
one OS process per CPU.

**Machine.** The numbers below come from Apple Silicon, macOS 25.6, 10 cores
(4 performance + 6 efficiency). Python 3.14.7, both builds, installed by uv.
Rust 1.92, release profile with fat LTO. uvicorn 0.52.4, granian 2.8.2,
FastAPI 0.141.1.

Every result file records the host that produced it — CPU model, core counts,
memory, virtualisation, descriptor limits, and what worker-count detection made
of all of it — so results from different machines can be compared without
guessing at what the difference was.

```bash
make bench        # hello world, free-threaded
make bench-gil    # hello world, GIL build
make bench-cpu    # CPU-bound handler scaling
make sweep        # handler cost against loop count
make bench-all    # all of the above, both builds, collected into one archive
make machine      # print the host fingerprint and the preflight checks
```

The worker-count ladders are derived from the machine rather than fixed, so a
larger host measures the larger loop counts that only exist there.

**Preflight.** Four conditions make a measurement worthless and none of them is
visible in the number it produced: a `powersave` CPU governor, existing load, a
hypervisor stealing CPU from the guest, and a file-descriptor limit too low for
the connections being opened. Each runner checks all four, records the verdict
in the result file, and with `--strict` refuses to measure at all. A benchmark
host is provisioned with `bench/provision.sh`, which sets the first and third
of those straight and disables background package updates.

!!! warning "Read this before quoting any number here"

    **Hello world measures dispatch, not a framework.** It answers whether a
    dispatch design is worth building. It answers nothing about a real
    application, where a single database call dwarfs everything measured below.

    **Record the load average.** A machine still busy from a previous run
    reports regressions that do not exist; one such 3.5% drop traced entirely
    to leftover benchmark load. Every result carries the load average before
    and after, and a run that started above 2.0 is marked untrustworthy.
    Absolute numbers compare across sessions only at similar starting load;
    ratios inside one run are always sound, since every target faces the same
    machine.

    **A shared vCPU cannot produce a reproducible number.** Steal time is
    measured across every run and reported; a host that gives away CPU
    mid-measurement invalidates it, and no amount of averaging recovers it.

    **Never benchmark through a published port on Docker Desktop.** Traffic
    crossing from macOS into the Linux VM through `-p` measured 3.3x slower
    than native, and that cost is the port forwarding, not the container. A
    load generator in a second container on the same Docker network avoids it
    entirely. Container numbers are compared with container numbers; across
    the boundary only ratios measured in the same session mean anything.

## Containers

`bench/container.py` runs the same hello-world server natively and in Docker
in one session, with native measured first and last so drift is visible. On the
machine above, with Docker Desktop's VM given all ten cores:

| scenario | loops | req/s | vs native | p99 ms |
|---|---:|---:|---:|---:|
| native (macOS) | 4 | 182,378 | 1.00x | 2.41 |
| container, load generator on the Docker network | 8 | 230,347 | 1.26x | 0.79 |
| container, load generator on the host via `-p` | 8 | 55,199 | 0.30x | 2.48 |
| container, `--cpus 4` | 4 | 231,177 | 1.27x | 0.64 |
| container, `--cpus 2` | 2 | 166,270 | 0.91x | 0.55 |
| container, `--cpus 1` | 1 | 123,288 | 0.68x | 0.50 |

Linux in the VM is faster than macOS natively at the same loop count, with a
far tighter tail: the difference is the operating system's network stack and
scheduler, not the framework. A CPU quota sets the loop count, because worker
detection reads the cgroup limit rather than the host's core count.

Inside a VM on Apple Silicon, detection cannot see which cores are efficiency
cores and starts eight loops where four perform the same.

```bash
make image-bench      # the runtime image plus oha and the bench scripts
make bench-container  # the table above, for this machine
```

## Hello world

Free-threaded 3.14.7, 64 connections:

| target | req/s | p50 ms | p99 ms |
|---|---:|---:|---:|
| Oxbrook, 4 loops | 181,397 | 0.28 | 1.46 |
| granian raw ASGI | 135,846 | 0.46 | 0.72 |
| granian + FastAPI | 29,674 | 2.14 | 2.51 |
| uvicorn raw ASGI, 10 workers | 66,474 | 0.65 | 4.61 |
| uvicorn raw ASGI | 19,493 | 3.30 | 3.42 |
| uvicorn + FastAPI | 12,411 | 5.17 | 5.36 |

Granian is the closest comparison: same shape, a Rust server driving a Python
event loop, and therefore the number to watch when this design changes. The
FastAPI rows answer a different question, since they include a framework doing
framework work.

The same benchmark on the standard GIL build, where the default is one worker
loop:

| target | req/s | p50 ms | p99 ms |
|---|---:|---:|---:|
| Oxbrook, 1 loop | 192,054 | 0.33 | 0.54 |
| granian raw ASGI | 128,733 | 0.50 | 0.78 |
| granian + FastAPI | 35,238 | 1.79 | 2.20 |
| uvicorn + FastAPI | 11,572 | 5.55 | 5.72 |

Dispatch is not what free-threading buys. A single loop performs the same on
either build; what the free-threaded build adds is the ability to run several
loops usefully, which matters once handlers do work.

## What the design was worth

Dispatch was rewritten from `call_soon_threadsafe` per request to a lock-free
queue with coalesced wakeups.

| build / loops | before | after | change |
|---|---:|---:|---:|
| free-threaded, 1 loop | 35,158 | 189,015 | **5.4x** |
| free-threaded, 10 loops | 14,114 | 171,304 | **12.1x** |
| GIL, 1 loop | 63,163 | 192,054 | **3.0x** |

The original numbers showed three things at once, and each of them was a
design error: throughput *fell* as loops were added, because every request woke
an idle loop and no wakeup coalesced; the free-threaded build was slower than
the GIL build, because refcounting is atomic on 3.14t and tokio threads were
touching Python objects; and the whole thing sat far below granian, which meant
the gap was dispatch rather than HTTP.

## Free-threading actually delivers

A CPU-bound handler — a 20,000-iteration Python loop, about 376µs — at 32
connections:

| loops | free-threaded | speedup | GIL | speedup |
|---:|---:|---:|---:|---:|
| 1 | 2,659 | 1.00x | 2,308 | 1.00x |
| 2 | 5,205 | 1.96x | 2,372 | 1.03x |
| 4 | 8,380 | **3.15x** | 2,375 | 1.03x |
| 8 | 7,201 | 2.71x | 2,357 | 1.02x |

The GIL build is flat, exactly as predicted. The ceiling on the free-threaded
build is the performance-core count, not the core count: 8 loops on a 4+6
machine oversubscribes the efficiency cores and loses ground.

## How many loops

Handler CPU cost against loop count, free-threaded:

| handler µs | 1 loop | 2 loops | 4 loops | 8 loops | gain over 1 |
|---:|---:|---:|---:|---:|---:|
| 0 | 184,539 | 190,349 | 193,172 | 173,880 | 1.05x |
| 10 | 95,933 | 116,977 | 114,443 | 137,985 | 1.44x |
| 50 | 19,159 | 43,052 | 53,605 | 47,497 | 2.80x |
| 100 | 10,127 | 20,946 | 30,461 | 27,205 | 3.01x |
| 500 | 2,154 | 4,202 | 7,347 | 6,031 | 3.41x |

**There is no crossover.** Extra loops win at every handler cost, including
zero, which is why one loop is never the default on a free-threaded build. The
same sweep on the GIL build is flat and extra loops only cost throughput, which
is why one loop is always the default there.

The sweep also contradicts an intuitive prediction: that dispatch should prefer
fewer loops, because wakeup coalescing is diluted when arrivals spread across
many of them. The measurements do not support it. The only significant effect is
that oversubscribing the performance cores hurts.

## What features cost

| change | cost |
|---|---|
| typed path parameter | nothing measurable |
| query parameters | nothing measurable on routes that declare none |
| pydantic body validation | ~13% against hello world |
| `request_timeout=30` | 5-8% |
| headers on every request, explicit slot release | the rest of ~12% at M6 |

The request timeout is on by default despite that cost. A handler that hangs
otherwise holds a connection and a concurrency slot indefinitely. The same
trade-off governs body limits and error detail: fail safely by default, and set
`request_timeout=0` once you have measured your own workload.

Numbers taken before that change describe a different server and are not
comparable with the ones above.

## Slow handlers

A handler that computes rather than awaits holds its worker loop until it
returns. Each request goes to the least-loaded worker — queued plus in-flight —
and a held loop cannot drain, so requests route around it. `bench/imbalance.py`
runs a fixed-rate stream of trivial requests alongside handlers that hold a loop
for 50 ms, with latency correction so a stalled request counts from when it
should have been sent. Four loops, 2,000 req/s:

| alongside | p90 ms, round-robin | p90 ms, least-loaded | p99 ms, round-robin | p99 ms, least-loaded |
|---|---:|---:|---:|---:|
| nothing | 0.29 | 0.29 | 0.56 | 0.46 |
| 1 CPU-bound handler | 29.68 | 0.14 | 47.72 | 0.28 |
| 2 CPU-bound handlers | 39.39 | 0.15 | 48.82 | 36.61 |
| 3 CPU-bound handlers | 55.53 | 0.20 | 95.23 | 48.89 |
| 4 I/O-bound handlers | 0.32 | 0.30 | 0.50 | 0.45 |

Round-robin gave a held loop its full share of requests: with one of four held,
a quarter of all requests waited up to the whole 50 ms. The residual tail under
least-loaded assignment is the few requests that arrive before a loop's load
reflects the handler holding it.

On hello world, least-loaded assignment is 2–5% faster than round-robin on
Linux with the same or lower p99. On macOS it is 3% faster with a p99 about
1 ms higher.

## Streams

`bench/streams.py` measures SSE fan-out and WebSocket echo against a Python
load generator on the same machine. It records the server's CPU per thousand
operations, which holds whatever the client's speed, and flags a run where the
client processes were saturated.

| SSE subscribers | events each | deliveries/s | missing | server CPU ms per 1,000 |
|---:|---:|---:|---:|---:|
| 10 | 20,000 | 79,061 | 0 | 39.2 |
| 100 | 2,000 | 138,737 | 0 | 29.8 |
| 1,000 | 200 | 145,943 | 0 | 29.9 |

The topic uses the `block` policy, so every event reaches every subscriber and
the rate is lossless. At low fan-out the single publishing handler is the limit
rather than delivery.

| WebSocket connections | round trips/s | server CPU ms per 1,000 |
|---:|---:|---:|
| 10 | 66,967 | 40.6 |
| 100 | 142,715 (client-bound) | 28.9 |
| 1,000 | 128,975 (client-bound) | 30.6 |

Above 100 connections the Python client saturates first, so those rates are a
floor for the server rather than its ceiling.

## Static files

A 2 KiB file and a 1 MiB file from a static mount, beside a Python handler
returning the same 2 KiB from memory, 64 connections:

| target | req/s | p99 ms | throughput |
|---|---:|---:|---:|
| Python handler, bytes in memory | 181,852 | 2.04 | 355 MB/s |
| static file, 2 KiB | 95,495 | 4.02 | 186 MB/s |
| static file, 1 MiB | 5,983 | 26.16 | 5,975 MB/s |

Serving from disk is slower than returning bytes already in memory. Every
filesystem call for a request happens in one task on the blocking pool: resolved
per call through the async file API, the same small file measured 22,670 req/s.
Symlink containment checks only the path components inside the mount, rather
than canonicalising the whole path, which walks every directory from `/`.

## HTTPS and HTTP/2

The hello-world route five ways, 64 requests in flight (HTTP/2: 8 connections
of 8 streams), medians of five interleaved rounds, `bench/protocols.py`:

| mode | req/s | vs HTTP/1.1 only | p99 ms |
|---|---:|---:|---:|
| HTTP/1.1, `http2=False` | 190,726 | 1.00x | 1.76 |
| HTTP/1.1, HTTP/2 enabled | 190,425 | 1.00x | 1.75 |
| HTTP/2 cleartext | 221,704 | 1.16x | 1.33 |
| HTTPS, HTTP/1.1 | 181,676 | 0.95x | 2.00 |
| HTTPS, HTTP/2 | 213,864 | 1.12x | 1.38 |

Leaving HTTP/2 enabled costs a plain HTTP/1.1 client nothing measurable: the
first bytes are read once per connection, and HTTP/1.1 requests are not counted
by the idle check. TLS costs about 5% on established connections; handshakes
are not in these numbers, since the connections are reused. HTTP/2 is faster
here because many requests share a connection's writes.

## What has not been measured

These are open, not assumed. An unmeasured claim is not a result:

- **A handler that awaits** rather than burns CPU — a 1-5ms database call. The
  sweep covers CPU cost only, and an `await` yields the loop, so the two should
  behave very differently.
- **Latency under sustained overload**, now that backpressure exists.
- **Memory per worker loop.**
- **Scaling past 8 loops** on a large homogeneous Linux machine. The cap of 8
  is a guard against an absurd probe result, not a measured ceiling; this
  machine has four performance cores and cannot answer the question.
- **Form parsing and streaming uploads.** Both are verified for correctness and
  for memory — a 200 MB upload to a slow reader stays within a few megabytes —
  but neither has a throughput number.
- **TLS handshakes per second**, which is what a server with many short-lived
  clients pays.
- **The WebSocket ceiling.** Above 100 connections the Python load generator
  saturates before the server does; finding the server's limit needs a faster
  client.
