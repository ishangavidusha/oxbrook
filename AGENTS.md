# Agents working on Oxbrook

This file is for coding agents changing the Oxbrook repository itself. To
build an application *with* Oxbrook, read
<https://ishangavidusha.github.io/oxbrook/llms.txt> instead.

[`CONTRIBUTING.md`](CONTRIBUTING.md) is the whole of the rules: the build, the
test suites, what a change needs, the invariants, documentation and commit
style. Read it before changing anything. The parts most often missed:

- **Rebuild after changing Rust** (`make build`) before running a test; the
  suites import the installed extension, not the source.
- **The invariants are load-bearing.** Tokio threads never touch the
  interpreter; nothing blocks in native code while attached; a resource limit
  is released explicitly, never by garbage collection; a route is guarded the
  same way however it is reached. Breaking one passes most tests and fails in
  production.
- **Both Python builds must pass:** `make verify` (free-threaded 3.14t) and
  `make verify-gil`. Services for the suites run in containers: `make up`.
- **A correctness change needs a test that fails without it.**
- **`www/` is the public site**, written impersonally, and its API reference is
  the docstrings. Run `make docs` (strict) after touching either.
- **Commit messages are one short imperative line**, with no trailers.
