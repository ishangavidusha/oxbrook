"""The `oxbrook` command, also installed as `oxb` and runnable as `python -m oxbrook`.

    oxbrook run main:app                  serve
    oxbrook run main:app --reload         restart when a file changes
    oxbrook routes main:app               list every route
    oxbrook openapi main:app              print the OpenAPI document

A target is `module:attribute`, imported with the working directory (or
`--app-dir`) on the path. With no attribute, `app` is used. `--factory` calls
the attribute and serves what it returns.

**Reload restarts the process** rather than re-importing modules inside it. The
Rust extension cannot be reloaded in a running interpreter, and a fresh process
also re-runs the lifespans, so what a handler sees after a reload is what it
would see after a deploy. A supervisor process watches the files and replaces
the server; a syntax error in an edit stops the server, not the supervisor, and
the next save starts it again.
"""

import argparse
import fnmatch
import importlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

#: Set on the server process a reload supervisor starts, so it serves instead of
#: supervising in turn.
RELOAD_CHILD = "OXBROOK_RELOAD_CHILD"

#: Grace for a server being replaced by a reload. A browser holding an SSE
#: stream open would otherwise hold every restart for the full default grace.
RELOAD_GRACE = 1.0

POLL_INTERVAL = 0.4

#: Never worth watching, and some are very large.
IGNORED_DIRS = frozenset({
    "__pycache__", "node_modules", "target", "target-cov", "site", "dist", "build",
    "htmlcov", "venv", "env",
})


class TargetError(Exception):
    """The target could not be turned into an App. The message is the whole story."""


def fail(message: str) -> int:
    print(f"oxbrook: error: {message}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# loading the app
# ---------------------------------------------------------------------------
def load_app(target: str, app_dir: str | None = None, factory: bool = False) -> Any:
    from ._app import App

    module_name, _, attribute = target.partition(":")
    if not module_name or target.count(":") > 1:
        raise TargetError(f"target {target!r} should look like 'module:attribute', e.g. main:app")
    directory = Path(app_dir or os.getcwd()).resolve()
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        # Only the target itself not existing is a usage error. A module the
        # app imports being missing is the app's own bug, and its traceback is
        # the useful thing to show.
        if missing and (module_name == missing or module_name.startswith(missing + ".")):
            raise TargetError(
                f"could not import {module_name!r}: no module named {missing!r} in {directory}"
            ) from None
        raise

    attribute = attribute or "app"
    found: Any = module
    for part in attribute.split("."):
        try:
            found = getattr(found, part)
        except AttributeError:
            apps = sorted(name for name, value in vars(module).items() if isinstance(value, App))
            hint = f"; it defines {', '.join(apps)}" if apps else "; it defines no App"
            raise TargetError(f"{module_name!r} has no attribute {attribute!r}{hint}") from None

    if factory:
        if not callable(found):
            raise TargetError(f"--factory needs a callable, but {target!r} is a "
                              f"{type(found).__name__}")
        found = found()
    if not isinstance(found, App):
        import inspect

        hint = (" (pass --factory if it builds and returns an App)"
                if inspect.isfunction(found) and not factory else "")
        raise TargetError(f"{target!r} is a {type(found).__name__}, not an oxbrook.App{hint}")
    return found


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def configure_logging(level: str) -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("oxbrook").setLevel(level.upper())


def serve(args: argparse.Namespace) -> int:
    from . import _app

    configure_logging(args.log_level)
    app = load_app(args.target, args.app_dir, args.factory)
    if args.access_log:
        from ._logging import access_middleware

        if access_middleware not in app._middleware:
            # First, so it wraps everything and records the final status.
            app._middleware.insert(0, access_middleware)

    grace = args.shutdown_grace
    if grace is None:
        grace = RELOAD_GRACE if os.environ.get(RELOAD_CHILD) else _app.DEFAULT_SHUTDOWN_GRACE
    app.run(
        host=args.host,
        port=args.port,
        workers=args.workers,
        max_concurrency=args.max_concurrency,
        max_body=args.max_body,
        max_message=args.max_message,
        request_timeout=args.request_timeout,
        shutdown_grace=grace,
        max_connections=args.max_connections,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
        http2=args.http2,
    )
    return 0


def run(args: argparse.Namespace) -> int:
    if args.reload and not os.environ.get(RELOAD_CHILD):
        return supervise(args)
    return serve(args)


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------
def watched_files(directories: list[Path], patterns: list[str]) -> dict[Path, int]:
    """Every matching file under the directories, with its modification time."""
    seen: dict[Path, int] = {}
    for directory in directories:
        for root, dirs, files in os.walk(directory):
            # Pruned in place, so os.walk never descends into them.
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in IGNORED_DIRS]
            for name in files:
                if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
                    path = Path(root, name)
                    try:
                        seen[path] = path.stat().st_mtime_ns
                    except OSError:
                        continue
    return seen


def changes(
    directories: list[Path], patterns: list[str], before: dict[Path, int]
) -> Iterator[list[Path]]:
    """Yield the files that changed, were added or were removed, as they happen.

    Polls modification times. That needs no dependency and behaves the same on
    every platform and both interpreter builds; for a source tree with the
    virtual environment and build output pruned, a scan is a few milliseconds.

    `before` is taken by the caller *before* it starts the server. Taken here,
    after the server had started, an edit saved during startup was already in
    the first snapshot and never counted as a change.
    """
    while True:
        time.sleep(POLL_INTERVAL)
        after = watched_files(directories, patterns)
        changed = [p for p in after.keys() | before.keys() if after.get(p) != before.get(p)]
        if changed:
            # Editors often write a file in two steps; let the second land
            # before restarting on the first.
            time.sleep(0.1)
            after = watched_files(directories, patterns)
            before = after
            yield sorted(changed)


class _Stop(Exception):
    """Raised by the supervisor's signal handlers to end the watch loop."""


def _raise_stop(_signum: int, _frame: Any) -> None:
    raise _Stop


def forget_bytecode(paths: list[Path]) -> None:
    """Delete the cached bytecode of changed source files.

    Python trusts a `.pyc` whose recorded source modification time — in whole
    seconds — and size match the source. Two saves in the same second that keep
    the size the same, such as changing one digit, left the restarted server
    loading the first save's bytecode: an edit that reloaded and did nothing.
    Deleting the cache for exactly the changed files costs one recompile of each.
    """
    from importlib.util import cache_from_source

    for path in paths:
        if path.suffix != ".py":
            continue
        try:
            Path(cache_from_source(str(path))).unlink(missing_ok=True)
        except (OSError, ValueError, NotImplementedError):
            continue


def stop(child: subprocess.Popen, grace: float) -> None:
    if child.poll() is not None:
        return
    # SIGTERM rather than SIGINT: a process started in the background inherits
    # SIGINT as ignored, and the server drains on either.
    child.send_signal(signal.SIGTERM)
    try:
        child.wait(timeout=grace + 5.0)
    except subprocess.TimeoutExpired:
        print("oxbrook: the server did not stop in time; killing it", file=sys.stderr)
        child.kill()
        child.wait()


def supervise(args: argparse.Namespace) -> int:
    """Run the server in a child process and replace it whenever a file changes."""
    base = Path(args.app_dir or os.getcwd()).resolve()
    directories = [Path(d).resolve() for d in args.reload_dir] or [base]
    patterns = ["*.py", *args.reload_include]
    grace = args.shutdown_grace if args.shutdown_grace is not None else RELOAD_GRACE

    environment = {**os.environ, RELOAD_CHILD: "1"}
    command = [sys.executable, "-m", "oxbrook", *sys.argv[1:]]
    shown = ", ".join(str(d) for d in directories)
    print(f"oxbrook: watching {shown} for changes to {', '.join(patterns)}", flush=True)

    # Explicit handlers, because an inherited disposition cannot be trusted: a
    # background process in a non-interactive shell starts with SIGINT ignored,
    # and a supervisor that ignored it never stopped. SIGTERM is what a process
    # manager sends.
    previous = {sig: signal.signal(sig, _raise_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    child: subprocess.Popen | None = None
    try:
        snapshot = watched_files(directories, patterns)
        child = subprocess.Popen(command, env=environment)
        # The supervisor never imports the app itself: importing it here would
        # run module-level code twice and keep the first import's state for the
        # life of the session. A target that cannot start shows up as the first
        # server exiting, reported here rather than left to look like silence.
        time.sleep(POLL_INTERVAL * 2)
        reported_exit = child.poll() is not None
        if reported_exit:
            print(f"oxbrook: the server exited with code {child.returncode}; "
                  f"waiting for a change", file=sys.stderr, flush=True)
        for changed in changes(directories, patterns, snapshot):
            names = ", ".join(os.path.relpath(p, base) for p in changed[:3])
            more = f" and {len(changed) - 3} more" if len(changed) > 3 else ""
            print(f"oxbrook: {names}{more} changed; restarting", flush=True)
            stop(child, grace)
            forget_bytecode(changed)
            child = subprocess.Popen(command, env=environment)
            reported_exit = False
            # A server that dies on start — a syntax error mid-edit — is
            # reported once, and the supervisor waits for the next save.
            time.sleep(POLL_INTERVAL)
            if child.poll() is not None and not reported_exit:
                print(f"oxbrook: the server exited with code {child.returncode}; "
                      f"waiting for a change", file=sys.stderr, flush=True)
                reported_exit = True
    except (_Stop, KeyboardInterrupt):
        pass
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if child is not None:
            stop(child, grace)
    return 0


# ---------------------------------------------------------------------------
# routes and openapi
# ---------------------------------------------------------------------------
def routes(args: argparse.Namespace) -> int:
    app = load_app(args.target, args.app_dir, args.factory)
    declared = list(app.routes)
    # The built-in routes are added when a server is built; add them here too,
    # so the list is what a running server would serve.
    app._register_docs()
    app._register_mcp()
    builtin = [r for r in app.routes if r not in declared]

    rows = []
    for route in app.routes:
        notes = []
        if route.websocket:
            notes.append("websocket")
        if route.tool:
            notes.append("tool")
        if route.stream is not None:
            notes.append("streams body")
        if route.form is not None:
            notes.append("form")
        if route.middleware:
            notes.append(f"{len(route.middleware)} router middleware")
        if route in builtin:
            notes.append("built in")
        fn = route.fn
        handler = ("oxbrook" if route in builtin
                   else f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', '?')}")
        rows.append(("WS" if route.websocket else route.method, route.path, handler,
                     ", ".join(notes)))

    for mount in app.mounts:
        path = mount.prefix if mount.prefix == "/" else f"{mount.prefix}/"
        notes = ["static files"]
        if mount.fallback:
            notes.append(f"fallback {mount.fallback}")
        rows.append(("GET", f"{path}*", mount.directory, ", ".join(notes)))

    if args.json:
        print(json.dumps([
            {"method": m, "path": p, "handler": h, "notes": n.split(", ") if n else []}
            for m, p, h, n in rows
        ], indent=2))
        return 0

    widths = [max(len(row[i]) for row in [("METHOD", "PATH", "HANDLER", ""), *rows])
              for i in range(3)]
    header = ("METHOD", "PATH", "HANDLER", "NOTES")
    for row in (header, *rows):
        print(f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  {row[3]}"
              .rstrip())
    return 0


def openapi(args: argparse.Namespace) -> int:
    app = load_app(args.target, args.app_dir, args.factory)
    document = json.dumps(app.openapi(), indent=2)
    if args.output:
        Path(args.output).write_text(document + "\n")
    else:
        print(document)
    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed

    from ._workers import gil_enabled

    try:
        number = installed("oxbrook")
    except PackageNotFoundError:
        number = "unknown"
    build = "GIL" if gil_enabled() else "free-threaded"
    return f"oxbrook {number} (Python {sys.version.split()[0]}, {build})"


def parser() -> argparse.ArgumentParser:
    from . import _app

    main = argparse.ArgumentParser(
        prog="oxbrook",
        description="Serve and inspect Oxbrook apps.",
    )
    main.add_argument("--version", action="version", version=version())
    commands = main.add_subparsers(dest="command", metavar="command")

    def target(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("target", help="the app, as module:attribute (default attribute: app)")
        sub.add_argument("--app-dir", help="directory to import the target from "
                                           "(default: the working directory)")
        sub.add_argument("--factory", action="store_true",
                         help="call the target and use the App it returns")

    serve_cmd = commands.add_parser("run", help="serve an app")
    target(serve_cmd)
    serve_cmd.add_argument("--host", default="127.0.0.1",
                           help="interface to bind (default: 127.0.0.1; 0.0.0.0 in a container)")
    serve_cmd.add_argument("--port", type=int, default=8000, help="port to bind (default: 8000)")
    serve_cmd.add_argument("--workers", type=int, default=None,
                           help="worker loops (default: detected; 1 on the GIL build)")
    serve_cmd.add_argument("--reload", action="store_true",
                           help="restart when a watched file changes; for development")
    serve_cmd.add_argument("--reload-dir", action="append", default=[], metavar="DIR",
                           help="directory to watch, repeatable (default: the app directory)")
    serve_cmd.add_argument("--reload-include", action="append", default=[], metavar="GLOB",
                           help="also restart for files matching this, e.g. '*.html'")
    serve_cmd.add_argument("--tls-cert", metavar="FILE",
                           help="PEM certificate chain; serve HTTPS (needs --tls-key)")
    serve_cmd.add_argument("--tls-key", metavar="FILE", help="PEM private key for --tls-cert")
    serve_cmd.add_argument("--http2", action=argparse.BooleanOptionalAction, default=True,
                           help="serve HTTP/2 as well as HTTP/1.1 (default: on)")
    serve_cmd.add_argument("--access-log", action="store_true", help="log every request")
    serve_cmd.add_argument("--log-level", default="info",
                           choices=["debug", "info", "warning", "error"])
    serve_cmd.add_argument("--max-concurrency", type=int, default=_app.DEFAULT_MAX_CONCURRENCY)
    serve_cmd.add_argument("--max-connections", type=int, default=_app.DEFAULT_MAX_CONNECTIONS)
    serve_cmd.add_argument("--max-body", type=int, default=_app.DEFAULT_MAX_BODY)
    serve_cmd.add_argument("--max-message", type=int, default=_app.DEFAULT_MAX_MESSAGE)
    serve_cmd.add_argument("--request-timeout", type=float,
                           default=_app.DEFAULT_REQUEST_TIMEOUT)
    serve_cmd.add_argument("--shutdown-grace", type=float, default=None,
                           help=f"seconds to let requests finish on stop "
                                f"(default: {_app.DEFAULT_SHUTDOWN_GRACE:g}, "
                                f"or {RELOAD_GRACE:g} when reloading)")
    serve_cmd.set_defaults(handler=run)

    routes_cmd = commands.add_parser("routes", help="list an app's routes")
    target(routes_cmd)
    routes_cmd.add_argument("--json", action="store_true", help="print JSON instead of a table")
    routes_cmd.set_defaults(handler=routes)

    openapi_cmd = commands.add_parser("openapi", help="print an app's OpenAPI document")
    target(openapi_cmd)
    openapi_cmd.add_argument("--output", "-o", help="write to this file instead of stdout")
    openapi_cmd.set_defaults(handler=openapi)

    return main


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if args.command is None:
        cli.print_help()
        return 2
    try:
        return args.handler(args)
    except TargetError as exc:
        return fail(str(exc))
    except KeyboardInterrupt:
        return 0


def entry() -> None:
    """Console-script entry point for `oxbrook` and `oxb`."""
    sys.exit(main())
