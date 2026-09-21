# Oxbrook milestone-1 spike. Two venvs: .venv (free-threaded 3.14t) and .venv-gil (standard 3.14).
FT_PY  := 3.14.7+freethreaded
# Homebrew's 3.14 on this Mac, so its numbers stay comparable with every run
# recorded before benchmarks ever left it. Anywhere else, uv fetches its own.
GIL_PY := $(shell test -x /opt/homebrew/bin/python3.14 && echo /opt/homebrew/bin/python3.14 || echo 3.14)

.PHONY: image-bench bench-imbalance bench-streams bench-protocols bench-container machine bench-collect venvs build build-ft build-gil docs docs-serve coverage coverage-rust lint run bench bench-gil bench-cpu bench-cpu-gil sweep sweep-gil bench-all verify verify-gil up down logs image stack stack-down clean

venvs:
	uv venv --python $(FT_PY) .venv
	uv venv --python $(GIL_PY) .venv-gil
	uv pip install --python .venv/bin/python maturin uvicorn granian fastapi "httpx[http2]" cryptography openapi-spec-validator websockets redis mcp coverage
	uv pip install --python .venv-gil/bin/python maturin uvicorn granian fastapi "httpx[http2]" cryptography openapi-spec-validator websockets redis mcp coverage
	# Docs tooling only in the GIL venv: mkdocs has no reason to run twice.
	uv pip install --python .venv-gil/bin/python mkdocs-material 'mkdocstrings[python]' ruff

build: build-ft build-gil

build-ft:
	VIRTUAL_ENV=$(CURDIR)/.venv .venv/bin/maturin develop --release

build-gil:
	VIRTUAL_ENV=$(CURDIR)/.venv-gil .venv-gil/bin/maturin develop --release

run: build-ft
	.venv/bin/python examples/hello.py

bench: build-ft
	.venv/bin/python bench/run.py --python .venv/bin/python

bench-gil: build-gil
	.venv-gil/bin/python bench/run.py --python .venv-gil/bin/python

bench-cpu: build-ft
	.venv/bin/python bench/cpu.py --python .venv/bin/python

bench-cpu-gil: build-gil
	.venv-gil/bin/python bench/cpu.py --python .venv-gil/bin/python

# One list, used by both builds, by the coverage run, and by the platforms
# with no shell loop to write it in: it lives in tests/run.py, which runs the
# suites on Windows and against a freshly built wheel. Three copies of it is
# how a suite ends up running on one interpreter and not the other.
SUITES := $(shell python3 tests/run.py --list)

# SUITE_TIMEOUT is empty locally and set to `timeout 300` in CI, where a hung
# suite would otherwise burn the whole job. Echo the name first: a suite that
# hangs before its own first print is otherwise invisible in a CI log.
SUITE_TIMEOUT ?=

verify: build-ft
	@for s in $(SUITES); do echo "== $$s"; $(SUITE_TIMEOUT) .venv/bin/python tests/$$s.py || exit 1; done

verify-gil: build-gil
	@for s in $(SUITES); do echo "== $$s"; $(SUITE_TIMEOUT) .venv-gil/bin/python tests/$$s.py || exit 1; done

# Branch coverage of the Python half. COVERAGE_CORE=sysmon matters: handlers run
# on threads Rust created, which the classic trace hook never sees, and the
# report would understate the runtime by a wide margin.
COVERAGE_MIN := 85

# Paths are absolute because the CLI suite's processes run from a temporary
# project directory, and subprocess measurement (pyproject) follows them there.
coverage: build-ft
	@rm -f .coverage .coverage.* 2>/dev/null || true
	@for s in $(SUITES); do echo "== $$s"; \
		COVERAGE_FILE=$(CURDIR)/.coverage COVERAGE_CORE=sysmon $(SUITE_TIMEOUT) \
			.venv/bin/python -m coverage run --branch -p \
			--source=$(CURDIR)/python/oxbrook tests/$$s.py || exit 1; \
	done
	@.venv/bin/python -m coverage combine -q
	@.venv/bin/python -m coverage report --precision=1 --sort=cover \
		--fail-under=$(COVERAGE_MIN)
	@.venv/bin/python -m coverage html -q -d htmlcov
	@echo "html report: htmlcov/index.html"

# Coverage of the Rust half, via LLVM source-based instrumentation.
#
# The Rust runs as a Python extension driven by the Python suites, so this is
# not `cargo test`: build the extension instrumented, run the suites against it,
# then merge the .profraw each process leaves behind. Needs the llvm-tools
# component (`rustup component add llvm-tools-preview`).
#
# Built into target-cov/ and rebuilt release at the end, so this never leaves an
# unoptimised extension installed for the next benchmark to measure.
LLVM_BIN := $(shell rustc --print sysroot)/lib/rustlib/$(shell rustc -vV | sed -n 's/^host: //p')/bin

coverage-rust:
	@rm -rf target-cov/prof target-cov/html && mkdir -p target-cov/prof
	CARGO_TARGET_DIR=target-cov RUSTFLAGS="-Cinstrument-coverage" \
		VIRTUAL_ENV=$(CURDIR)/.venv .venv/bin/maturin develop
	@for s in $(SUITES); do \
		LLVM_PROFILE_FILE="$(CURDIR)/target-cov/prof/%p-%m.profraw" \
			.venv/bin/python tests/$$s.py >/dev/null 2>&1 || echo "suite failed: $$s"; \
	done
	@$(LLVM_BIN)/llvm-profdata merge -sparse target-cov/prof/*.profraw \
		-o target-cov/oxbrook.profdata
	@$(LLVM_BIN)/llvm-cov report --instr-profile=target-cov/oxbrook.profdata \
		--object python/oxbrook/_core.cpython-314t-darwin.so \
		--ignore-filename-regex='(/.cargo/|/rustc/|library/std)'
	@$(LLVM_BIN)/llvm-cov show --instr-profile=target-cov/oxbrook.profdata \
		--object python/oxbrook/_core.cpython-314t-darwin.so \
		--format=html --output-dir=target-cov/html \
		--ignore-filename-regex='(/.cargo/|/rustc/|library/std)'
	@echo "html report: target-cov/html/index.html"
	@echo "restoring the release build"
	@$(MAKE) --no-print-directory build-ft

# What CI enforces, runnable before pushing. Rust formatting and lints, then
# the Python linter. `ruff format` is deliberately not run: see pyproject.
lint:
	cargo fmt --check
	cargo clippy --all-targets -- -D warnings
	.venv-gil/bin/python -m ruff check python/oxbrook tests bench examples

# --- public documentation ---------------------------------------------------
# Built from the GIL venv, which is where the docs tooling lives. mkdocstrings
# imports the package for the API reference, so the extension has to be built
# first: an unbuilt tree documents nothing.
docs: build-gil
	.venv-gil/bin/python -m mkdocs build --strict

docs-serve: build-gil
	.venv-gil/bin/python -m mkdocs serve

# --- a measurement session --------------------------------------------------
# One command per host. Everything below records the machine it ran on, so
# results from a cloud instance and results from this Mac can sit in the same
# directory without pretending to be comparable.
#
# STRICT is on by default: a host that fails preflight — powersave governor,
# existing load, too few descriptors — refuses to produce numbers rather than
# producing misleading ones. `make bench-all STRICT=` overrides that when a
# rough number on a busy machine is genuinely what is wanted.
#
# PROFILE=quick is a two-minute sanity pass, for checking the harness works on
# a new host before committing an hour to it.
PROFILE ?= full
STRICT  ?= --strict

ifeq ($(PROFILE),quick)
RUN_ARGS       := --duration 3 --warmup 1
CPU_ARGS       := --duration 3
SWEEP_ARGS     := --duration 2 --costs 0 50 500
IMBALANCE_ARGS := --duration 3 --conns 512
STREAMS_ARGS   := --subscribers 10 100 --deliveries 20000 --connections 10 --seconds 2
else
RUN_ARGS       := --duration 10
CPU_ARGS       := --duration 6
SWEEP_ARGS     := --duration 5
# 512 connections, so the load generator never runs out of them and reports
# its own backlog as server latency.
IMBALANCE_ARGS := --conns 512
STREAMS_ARGS   :=
endif

bench-all: build
	@echo "== hello world, free-threaded"
	.venv/bin/python bench/run.py --python .venv/bin/python $(RUN_ARGS) $(STRICT)
	@echo "== hello world, gil"
	.venv-gil/bin/python bench/run.py --python .venv-gil/bin/python $(RUN_ARGS) $(STRICT)
	@echo "== handler parallelism, free-threaded"
	.venv/bin/python bench/cpu.py --python .venv/bin/python $(CPU_ARGS) $(STRICT)
	@echo "== handler parallelism, gil"
	.venv-gil/bin/python bench/cpu.py --python .venv-gil/bin/python $(CPU_ARGS) $(STRICT)
	@echo "== handler cost against loop count, free-threaded"
	.venv/bin/python bench/sweep.py --python .venv/bin/python $(SWEEP_ARGS) $(STRICT)
	@echo "== handler cost against loop count, gil"
	.venv-gil/bin/python bench/sweep.py --python .venv-gil/bin/python $(SWEEP_ARGS) $(STRICT)
	@echo "== slow handlers against worker assignment, free-threaded"
	.venv/bin/python bench/imbalance.py --python .venv/bin/python $(IMBALANCE_ARGS) $(STRICT)
	@echo "== sse fan-out and websocket echo, free-threaded"
	.venv/bin/python bench/streams.py --python .venv/bin/python $(STREAMS_ARGS) $(STRICT)
	@$(MAKE) --no-print-directory bench-collect

# One tarball to copy off a host that is about to be destroyed. Named for the
# machine, so two of them never overwrite each other.
# Either venv can answer; a host that only built one still collects.
BENCH_PY := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo .venv-gil/bin/python)

bench-collect:
	@id=$$($(BENCH_PY) -c "import sys;sys.path.insert(0,'bench');\
import machine;print(machine.machine_id(machine.fingerprint()))"); \
	out=bench/results-$$id-$$(date +%Y%m%d-%H%M%S).tar.gz; \
	tar czf $$out -C bench results && echo "collected $$out"

bench-imbalance: build-ft
	.venv/bin/python bench/imbalance.py --python .venv/bin/python --conns 512

bench-streams: build-ft
	.venv/bin/python bench/streams.py --python .venv/bin/python

bench-protocols: build-ft
	.venv/bin/python bench/protocols.py --python .venv/bin/python

# Native against containerised, in one session. Needs Docker and image-bench.
bench-container: build-ft image-bench
	.venv/bin/python bench/container.py --python .venv/bin/python

machine:
	@$(BENCH_PY) bench/machine.py


sweep: build-ft
	.venv/bin/python bench/sweep.py --python .venv/bin/python

sweep-gil: build-gil
	.venv-gil/bin/python bench/sweep.py --python .venv-gil/bin/python

# --- containers -------------------------------------------------------------
# Services run in containers so nothing has to be installed on the host.
# Docker Desktop does not always put its CLI on a non-interactive PATH.
DOCKER := $(shell command -v docker 2>/dev/null || echo $(HOME)/.docker/bin/docker)
COMPOSE := $(DOCKER) compose

# Durable-topic tests need Redis. Without it they print SKIP and still pass, so
# run this before trusting `make verify` to have covered milestone 4.
up:
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f

# Build the app image, and run a two-node stack against one Redis. This is the
# only way to exercise cross-process fan-out the way it actually ships.
image:
	$(DOCKER) build --target runtime -t oxbrook:dev .

# The runtime image plus oha and bench/, for measuring inside Docker's network.
image-bench:
	$(DOCKER) build --target bench -t oxbrook:bench .

stack: image
	$(COMPOSE) -f docker-compose.yml -f docker-compose.stack.yml up -d --wait

stack-down:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.stack.yml down -v

clean:
	rm -rf target target-cov .venv .venv-gil bench/results site htmlcov
