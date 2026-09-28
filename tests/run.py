#!/usr/bin/env python3
"""Run the test suites, one process each, on any platform.

    python tests/run.py                 every suite, in order
    python tests/run.py routing files   only these
    python tests/run.py --wheel         what a freshly built wheel is tested with
    python tests/run.py --list          the names, for the Makefile

The Makefile drives the suites through a shell loop, which is fine on Unix and
does not exist on Windows, where there is no `make` either. This runner is what
Windows CI and the wheel checks use, and it holds the one list both of them
read: a second copy is how a suite ends up running on one interpreter and not
the other.

Each suite is a standalone script that exits non-zero on failure, so the first
failure ends the run with that suite's own exit code.
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Every suite, in the order they run. `verify` is last: it is the end-to-end
#: pass over a running server, and a failure there is most readable once the
#: narrower suites have had their say.
SUITES = [
    "workers", "routing", "query", "bodies", "encoding", "blocking", "openapi", "capabilities",
    "agents", "streams", "sse",
    "websocket", "hardening", "escaping", "wire", "failures", "plumbing", "injection",
    "durable", "database", "backpressure", "assignment", "composition", "lifespan", "cors",
    "uploads", "origins", "auth", "oidc", "login", "cli", "files", "protocols", "cancellation",
    "verify",
]

#: What a built wheel is checked with, in `[tool.cibuildwheel]`. A subset,
#: because this runs once per wheel on a CI runner: the dispatch path, the
#: request path, the protections, the static mounts, cancellation, TLS and
#: HTTP/2, and the end-to-end pass. Redis, PostgreSQL and Keycloak are not
#: there, so `durable`, `database`, `oidc` and `login` are out.
WHEEL = ["routing", "bodies", "hardening", "files", "cancellation", "protocols", "verify"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suites", nargs="*", help="suites to run; default is all of them")
    parser.add_argument("--wheel", action="store_true", help="the subset a wheel is tested with")
    parser.add_argument("--list", action="store_true", help="print the names and exit")
    parser.add_argument("--timeout", type=float, default=None,
                        help="seconds one suite may take before it counts as hung")
    args = parser.parse_args()

    chosen = args.suites or (WHEEL if args.wheel else SUITES)
    unknown = [name for name in chosen if not (HERE / f"{name}.py").exists()]
    if unknown:
        print(f"no such suite: {', '.join(unknown)}", file=sys.stderr)
        return 2

    if args.list:
        print(" ".join(chosen))
        return 0

    # `durable` is the only suite covering milestone 4 and it prints SKIP
    # without a Redis, so a run that forgot `make up` comes back green having
    # tested none of it (I-019). Ask first: when Redis is required, this is
    # the difference between finding out now and finding out fifteen suites
    # in, and when it is not, the warning still reaches the top of the log.
    # `database` is the same shape for PostgreSQL, `oidc` and `login` for Keycloak.
    for gated in ("durable", "database", "oidc", "login"):
        if gated in chosen:
            probe = subprocess.run([sys.executable, str(HERE / f"{gated}.py"), "--check"],
                                   cwd=HERE.parent)
            if probe.returncode != 0:
                return probe.returncode

    for name in chosen:
        # The name first: a suite that hangs before its own first print is
        # otherwise invisible in a CI log.
        print(f"== {name}", flush=True)
        try:
            result = subprocess.run([sys.executable, str(HERE / f"{name}.py")],
                                    cwd=HERE.parent, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(f"\n{name} did not finish in {args.timeout}s", file=sys.stderr, flush=True)
            return 1
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
