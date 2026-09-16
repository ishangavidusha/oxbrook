# Contributing

Oxbrook is a personal project rather than a maintained product, so open an issue
before writing anything substantial — the answer may be that a change is out of
scope, and that is cheaper to hear first.

Small fixes, missing tests and documentation corrections are welcome without
ceremony.

## Requirements

- **Python 3.13 or newer.** Free-threaded CPython 3.14 (`python3.14t`) is the
  primary target; the standard GIL build must keep working too.
- **A Rust toolchain**, for the extension module.
- **[uv](https://docs.astral.sh/uv/)**, for the environments.
- **Docker**, for Redis. Services run in containers, never on the host.
- **[oha](https://github.com/hatoo/oha)**, only for benchmarks.

## Build

```bash
make venvs     # .venv (3.14t) and .venv-gil (3.14)
make build     # maturin develop --release into both
```

Rebuild after any change under `src/`; the `make` targets that need it already
do. `cargo check` is a fast compile check, but `cargo build` fails to link,
because this is a Python extension module rather than a binary.

## Tests

```bash
make verify        # twenty-six suites, free-threaded
make verify-gil    # the same suites on the GIL build
```

Both must pass. Each suite is a standalone script that exits non-zero on
failure and runs against a real server on a real socket.

**Start Redis first.** `tests/durable.py` prints `SKIP` and still passes when
Redis is unreachable, so a green run on a machine with no containers has not
tested durable topics.

Coverage of both halves:

```bash
make coverage        # Python, branch coverage, fails under 85%
make coverage-rust   # Rust, via LLVM instrumentation
```

`coverage-rust` needs `rustup component add llvm-tools-preview`. It builds an
instrumented extension, runs the suites against it, and rebuilds release
afterwards so a benchmark never measures the instrumented build.

```bash
make up            # redis
make stack         # two nodes against one redis, for cross-process behaviour
```

## What a change needs

**A correctness fix needs a test that fails without it.** `tests/hardening.py`
is the pattern: each case names a defect that was demonstrated against a running
server before it was fixed.

**A performance claim needs a benchmark**, with the machine and the method
recorded. The runners do the recording: every result file carries a fingerprint
of the host and a preflight verdict, and a run that started on a busy machine,
a `powersave` governor, or a host stealing CPU from its guest is marked
untrustworthy rather than quietly kept.

```bash
make machine          # what this host is, and whether it is fit to measure on
make bench-all        # the whole battery, both builds, into one archive
make bench-imbalance  # slow handlers against worker assignment
make bench-streams    # SSE fan-out and WebSocket echo
make bench-container  # native against Docker, in one session
```

`make bench-all` refuses to run on a host that fails preflight; pass
`STRICT=` to override, and `PROFILE=quick` for a two-minute sanity pass.
A fresh Linux benchmark host is set up by `bench/provision.sh`.

Numbers from a shared vCPU are not reproducible and no amount of averaging
makes them so. Measure on dedicated CPU.

In Docker Desktop, put the load generator in a container on the same Docker
network, never on the host through a published port: the port forwarding alone
costs more than 3x and swamps anything being measured. Compare container
numbers with container numbers, and native with container only as a ratio taken
in one session.

**Protocol work is verified against a real client**, not against a reading of
the specification. The OpenAPI document goes through `openapi-spec-validator`;
the MCP endpoint is driven by the official SDK client. An SDK constant is not
the same thing as what a client will negotiate.

**Both interpreter builds must work.** A change that only works free-threaded
is not finished.

**Rust is formatted and linted, and CI enforces both.**

```bash
make lint          # cargo fmt --check, clippy with warnings as errors, ruff
```

Three Rust lints are silenced in the source, each at its own definition with a
comment saying why. Silence a new one the same way — never crate-wide, and
never without the reason. The same goes for a `# noqa` in Python.

`ruff format` is not run. The Python here is already hand-formatted
consistently, and reformatting it would be 1162 changed lines for no defect
caught.

## Invariants

These are load-bearing. Breaking one silently destroys performance or
deadlocks, and none of them fails loudly. [Internals](www/design/internals.md)
explains each in context; the short form:

1. **Tokio threads never touch the interpreter.** No attach, no object
   creation, no refcount, on any tokio thread. This is worth 5.4x.
2. **Never block in native code while attached.** Wrap a blocking wait in
   `py.detach`; blocking while attached deadlocks free-threaded CPython at a
   stop-the-world point, and on the GIL build it stops every other thread.
   To wake Python from a tokio thread, push onto the worker queue — never call
   `loop.call_soon_threadsafe`, which can block on the loop's self-pipe.
3. **Wakeups coalesce.** At most one wake byte in flight, and the flag is
   cleared *before* draining — clearing it after loses a racing push.
4. **Handlers are `async def`**, enforced at registration.
5. **Agent exposure stays opt-in.** Only `tool=True` routes reach `/mcp`.
6. **A resource limit is never released by garbage collection.** Free a
   concurrency slot with an explicit call, never `Drop`, never the collector.
7. **Anything Rust validates, Rust canonicalises**, so Python's constructors
   cannot fail on input Rust already accepted.
8. **Never return exception detail to a client.** Tracebacks go to the log.
   An agent calling a tool over MCP is a client.
9. **A route is guarded the same way however it is reached.** A tool call runs
   its routers' middleware and the exception handlers, with the caller's
   headers. A new way to invoke a handler must not bypass them.
10. **Nothing loop-bound crosses worker loops.** Connection pools and async
   clients belong to one loop; create them in `worker_lifespan`.

## Documentation

The site sources are in [`www/`](www/), and the API reference is generated from
the docstrings, so a public docstring is documentation.

```bash
make docs          # builds with --strict; a broken link or anchor fails it
make docs-serve    # live reload
```

Run it after changing a public docstring or a page. Write pages impersonally
and address the reader as "you": state what the framework does and why, not the
history of arriving at it.

## Commit messages

One short line, imperative, summarising what changed. The diff holds the
detail.

```
add queue-based dispatch
fix startup deadlock on free-threaded build
```

## License

Contributions are accepted under the [MIT license](LICENSE) that covers the
project.
