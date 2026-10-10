"""The `oxbrook` command, also installed as `oxb` and runnable as `python -m oxbrook`.

    oxbrook run main:app                  serve
    oxbrook run main:app --reload         restart when a file changes
    oxbrook routes main:app               list every route
    oxbrook openapi main:app              print the OpenAPI document
    oxbrook settings main:app             list the settings it reads, and what is missing
    oxbrook new notes-api                 start a project: an app, its tests, AGENTS.md

A target is `module:attribute`, imported with the working directory (or
`--app-dir`) on the path. With no attribute, `app` is used. `--factory` calls
the attribute and serves what it returns.

Every `run` option can also be set in the environment, as `OXBROOK_` and the
option's name: `OXBROOK_PORT=8080`, `OXBROOK_ACCESS_LOG=true`. A flag wins over
the variable. `PORT` is read too, after `OXBROOK_PORT`, since that is what
hosting platforms set. `--env-file` reads variables from a file first, without
overriding any already set.

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
import re
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


class UsageError(Exception):
    """An option or its environment variable is wrong. The message says which."""


# ---------------------------------------------------------------------------
# the environment
# ---------------------------------------------------------------------------
def _flag(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off", ""):
        return False
    raise ValueError


#: `run` options that can come from the environment: the argument's dest, how
#: to read the text, and the default when neither a flag nor a variable says.
#: The variable is `OXBROOK_` and the dest in capitals.
def _run_options() -> dict[str, tuple[Any, Any, str]]:
    from . import _app

    return {
        "host": (str, "127.0.0.1", "text"),
        "port": (int, 8000, "a whole number"),
        "workers": (int, None, "a whole number"),
        "reload": (_flag, False, "true or false"),
        "tls_cert": (str, None, "text"),
        "tls_key": (str, None, "text"),
        "http2": (_flag, True, "true or false"),
        "access_log": (_flag, False, "true or false"),
        "log_level": (str.lower, "info", "text"),
        "max_concurrency": (int, _app.DEFAULT_MAX_CONCURRENCY, "a whole number"),
        "max_connections": (int, _app.DEFAULT_MAX_CONNECTIONS, "a whole number"),
        "max_body": (int, _app.DEFAULT_MAX_BODY, "a whole number"),
        "max_message": (int, _app.DEFAULT_MAX_MESSAGE, "a whole number"),
        "request_timeout": (float, _app.DEFAULT_REQUEST_TIMEOUT, "a number"),
        "shutdown_grace": (float, None, "a number"),
    }


LOG_LEVELS = ("debug", "info", "warning", "error")


def resolve_options(args: argparse.Namespace) -> None:
    """Fill each `run` option the command line left unset: from its variable,
    then, for the port, from `PORT`, then from the default."""
    for dest, (read, default, kind) in _run_options().items():
        if getattr(args, dest) is not None:
            continue
        variable = f"OXBROOK_{dest.upper()}"
        names = [variable, "PORT"] if dest == "port" else [variable]
        value = default
        for name in names:
            text = os.environ.get(name)
            # Set but empty is unset: `PORT=` in a compose file means nothing.
            if text is None or not text.strip():
                continue
            try:
                value = read(text.strip())
            except ValueError:
                # The variable's name, never its value: it may be a secret
                # that was set in the wrong place.
                raise UsageError(f"{name} must be {kind}") from None
            break
        setattr(args, dest, value)
    if args.log_level not in LOG_LEVELS:
        raise UsageError(f"the log level must be one of {', '.join(LOG_LEVELS)}, "
                         f"not {args.log_level!r}")


def read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """`NAME=value` lines, as `.env` files write them.

    Blank lines and `#` comments are skipped, and `export ` before a name is
    allowed. A value in single quotes is taken as written; in double quotes,
    `\\n`, `\\t`, `\\"` and `\\\\` are escapes; unquoted, it ends at ` #` and
    surrounding space is dropped. Nothing is interpolated.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read the env file {os.fspath(path)!r}: "
                         f"{exc.strerror or exc}") from None
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, equals, value = line.partition("=")
        name = name.strip()
        if not equals or not name.isidentifier():
            raise UsageError(f"{os.fspath(path)}, line {number}: expected NAME=value")
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            while quote == '"' and end > 0 and value[end - 1] == "\\":
                end = value.find(quote, end + 1)
            if end < 0:
                raise UsageError(f"{os.fspath(path)}, line {number}: "
                                 f"the value of {name} has no closing {quote}")
            inner = value[1:end]
            if quote == '"':
                inner = (inner.replace("\\\\", "\0").replace("\\n", "\n")
                         .replace("\\t", "\t").replace('\\"', '"').replace("\0", "\\"))
            value = inner
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value
    return values


def load_env_file(path: str | os.PathLike[str]) -> None:
    """Put a file's variables into the environment, under any already set:
    the real environment is where a deployment overrides development."""
    for name, value in read_env_file(path).items():
        os.environ.setdefault(name, value)


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
    # The environment as it was before the env file, for a reload's server:
    # it reads the file again itself each time it starts, so an edit to the
    # file takes effect on the next restart.
    original = dict(os.environ)
    if args.env_file:
        load_env_file(args.env_file)
    resolve_options(args)
    if args.reload and not os.environ.get(RELOAD_CHILD):
        return supervise(args, original)
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

    Yields on every poll, an empty list when nothing changed, so the caller
    gets a turn on each tick: that is how the supervisor notices a server that
    died on its own rather than only when the next edit arrives.

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
        if not changed:
            yield []
            continue
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


#: How a child server is started and stopped, which is the one part of the
#: supervisor that is not the same on both platforms.
#:
#: Windows has no SIGTERM to send: terminating a process there is
#: `TerminateProcess`, which gives it no chance to drain. Ctrl-Break does
#: arrive as an event the server can handle, but only for a process in its own
#: group, so the child is started in one. The console's own Ctrl-C then stops
#: reaching the child, which is what should happen anyway — the supervisor
#: takes it and stops the child in an orderly way.
if sys.platform == "win32":
    CHILD_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP
    STOP_SIGNAL = signal.CTRL_BREAK_EVENT
else:
    CHILD_FLAGS = 0
    # SIGTERM rather than SIGINT: a process started in the background inherits
    # SIGINT as ignored, and the server drains on either.
    STOP_SIGNAL = signal.SIGTERM


def start_server(command: list[str], environment: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(command, env=environment, creationflags=CHILD_FLAGS)


def stop(child: subprocess.Popen, grace: float) -> None:
    if child.poll() is not None:
        return
    child.send_signal(STOP_SIGNAL)
    try:
        child.wait(timeout=grace + 5.0)
    except subprocess.TimeoutExpired:
        print("oxbrook: the server did not stop in time; killing it", file=sys.stderr)
        child.kill()
        child.wait()


def supervise(args: argparse.Namespace, original: dict[str, str] | None = None) -> int:
    """Run the server in a child process and replace it whenever a file changes."""
    base = Path(args.app_dir or os.getcwd()).resolve()
    directories = [Path(d).resolve() for d in args.reload_dir] or [base]
    patterns = ["*.py", *args.reload_include]
    if args.env_file:
        # A dotfile, which the default patterns never match.
        patterns.append(Path(args.env_file).name)
    grace = args.shutdown_grace if args.shutdown_grace is not None else RELOAD_GRACE

    environment = {**(os.environ if original is None else original), RELOAD_CHILD: "1"}
    command = [sys.executable, "-m", "oxbrook", *sys.argv[1:]]
    shown = ", ".join(str(d) for d in directories)
    print(f"oxbrook: watching {shown} for changes to {', '.join(patterns)}", flush=True)

    # Explicit handlers, because an inherited disposition cannot be trusted: a
    # background process in a non-interactive shell starts with SIGINT ignored,
    # and a supervisor that ignored it never stopped. SIGTERM is what a process
    # manager sends.
    # SIGBREAK is Windows' Ctrl-Break, and the signal a supervisor of this
    # supervisor would send; it does not exist elsewhere.
    stops = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        stops.append(signal.SIGBREAK)
    previous = {sig: signal.signal(sig, _raise_stop) for sig in stops}
    child: subprocess.Popen | None = None
    try:
        snapshot = watched_files(directories, patterns)
        child = start_server(command, environment)
        # The supervisor never imports the app itself: importing it here would
        # run module-level code twice and keep the first import's state for the
        # life of the session. A target that cannot start shows up as the
        # server exiting, reported below rather than left to look like silence.
        reported_exit = False
        for changed in changes(directories, patterns, snapshot):
            # A server that died on its own — a syntax error mid-edit, a port
            # already taken — is reported once, whenever it is noticed. This
            # used to be one look a fixed moment after the start, which on a
            # platform where starting a process takes longer saw a server that
            # was still alive and then never looked again.
            if child.poll() is not None and not reported_exit:
                print(f"oxbrook: the server exited with code {child.returncode}; "
                      f"waiting for a change", file=sys.stderr, flush=True)
                reported_exit = True
            if not changed:
                continue
            names = ", ".join(os.path.relpath(p, base) for p in changed[:3])
            more = f" and {len(changed) - 3} more" if len(changed) > 3 else ""
            print(f"oxbrook: {names}{more} changed; restarting", flush=True)
            stop(child, grace)
            forget_bytecode(changed)
            child = start_server(command, environment)
            reported_exit = False
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
    from ._auth import describe as describe_auth

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
        if route.blocking:
            # Worth seeing in a listing: these are the routes that hold a
            # thread rather than a slot on a loop.
            notes.append("blocking")
        if route.stream is not None:
            notes.append("streams body")
        if route.form is not None:
            notes.append("form")
        if route.middleware:
            notes.append(f"{len(route.middleware)} router middleware")
        # Every route's answer to "who may call this", so a route left public
        # in an app that is not stands out.
        auth = describe_auth(route.auth, app.auth)
        if auth:
            notes.append(auth if auth == "public" else f"auth {auth}")
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


def settings(args: argparse.Namespace) -> int:
    """Every Settings class the target defines, each variable it reads, and
    whether the environment would satisfy it now. Exits 1 if it would not."""
    from ._settings import _DEFINED, SettingsError

    if args.env_file:
        load_env_file(args.env_file)
    known = len(_DEFINED)
    failed: SettingsError | None = None
    try:
        load_app(args.target, args.app_dir, args.factory)
    except SettingsError as exc:
        # Creating one at import is the usual pattern, and the reason this
        # command exists: what it lists is most wanted when that fails.
        failed = exc
    # A class is recorded when it is defined, before anything creates one, so
    # one whose creation failed during the import is listed like the rest.
    classes = [c for c in _DEFINED[known:] if not c.__module__.startswith("oxbrook.")]

    report = []
    for cls in classes:
        try:
            cls()
            problems: dict[str, str] = {}
        except SettingsError as exc:
            problems = exc.problems
        rows = cls.variables()
        for row in rows:
            row["problem"] = problems.get(row["variable"])
        report.append({"settings": f"{cls.__module__}.{cls.__qualname__}", "variables": rows})
    bad = any(row["problem"] for entry in report for row in entry["variables"])

    if args.json:
        print(json.dumps(report, indent=2))
        return 1 if bad else 0
    if not report:
        print("no Settings classes found")
        if failed is None:
            return 0
    for entry in report:
        print(entry["settings"])
        table = []
        for row in entry["variables"]:
            if row["problem"]:
                status = row["problem"]
            elif row["source"] == "default":
                status = "default" if row["secret"] else f"default {json.dumps(row['default'])}"
            else:
                status = f"set ({row['source']})" if row["source"] != "environment" else "set"
            kind = row["type"] + (", secret" if row["secret"] else "")
            table.append((row["variable"], kind, status))
        widths = [max(len(r[i]) for r in [("VARIABLE", "TYPE", ""), *table]) for i in range(2)]
        for name, kind, status in [("VARIABLE", "TYPE", "STATUS"), *table]:
            print(f"  {name:<{widths[0]}}  {kind:<{widths[1]}}  {status}".rstrip())
    return 1 if bad else 0


# ---------------------------------------------------------------------------
# a new project
# ---------------------------------------------------------------------------
TEMPLATE = Path(__file__).parent / "_template"

#: A project name as pip and uv accept one (PEP 508), kept to what is also a
#: sensible directory name.
PROJECT_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


def requirement() -> str:
    """The dependency a new project declares: this minor version and its patches.

    Nothing is API-stable while the version is 0.x, and a minor release may
    break code, so a project made today should not pick up the next one
    unasked.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed

    try:
        number = installed("oxbrook")
    except PackageNotFoundError:
        return "oxbrook"
    parts = number.split(".")
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return "oxbrook"
    major, minor = int(parts[0]), int(parts[1])
    upper = f"{major}.{minor + 1}" if major == 0 else f"{major + 1}"
    return f"oxbrook>={major}.{minor},<{upper}"


def new(args: argparse.Namespace) -> int:
    """Write a working project into a new directory: an app with one router,
    settings, tests, and an AGENTS.md telling a coding assistant how Oxbrook
    differs from what it will guess."""
    target = Path(args.directory)
    name = args.name or target.resolve().name
    if not PROJECT_NAME.fullmatch(name):
        raise UsageError(f"{name!r} is not a usable project name: use letters, digits, "
                         "'-', '_' and '.', starting and ending with a letter or digit "
                         "(or pass --name)")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise UsageError(f"{target} already exists and is not empty")

    values = {
        "{{name}}": name.lower(),
        "{{title}}": re.sub(r"[-_.]+", " ", name).strip().title(),
        "{{oxbrook}}": requirement(),
    }
    written = []
    for source in sorted(TEMPLATE.rglob("*")):
        if source.is_dir() or "__pycache__" in source.parts:
            continue
        relative = source.relative_to(TEMPLATE)
        # Stored under other names so that tools working on this repository
        # leave them alone: git and packaging would obey a .gitignore, and
        # linters and uv would read a pyproject.toml as this repository's.
        if relative.name.startswith("dot-"):
            relative = relative.with_name("." + relative.name.removeprefix("dot-"))
        relative = relative.with_name(relative.name.removesuffix(".tmpl"))
        text = source.read_text(encoding="utf-8")
        for placeholder, value in values.items():
            text = text.replace(placeholder, value)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        written.append(relative)

    shown = os.path.relpath(target) if not target.is_absolute() else str(target)
    print(f"Created {name} in {shown} ({len(written)} files).")
    print()
    print("Next:")
    print(f"  cd {shown}")
    print("  uv sync")
    print("  uv run pytest")
    print("  uv run oxbrook run app.main:app --reload --env-file .env.example")
    print()
    print("AGENTS.md tells a coding assistant where Oxbrook's documentation is "
          "and what it must not guess.")
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

    serve_cmd = commands.add_parser(
        "run", help="serve an app",
        epilog="Each option can also be set as OXBROOK_<OPTION> in the environment, "
               "e.g. OXBROOK_PORT=8080; PORT is read after OXBROOK_PORT. A flag wins.")
    target(serve_cmd)
    env_file = {"metavar": "FILE",
                "help": "read NAME=value lines into the environment first, without "
                        "overriding variables already set; for development"}
    serve_cmd.add_argument("--env-file", **env_file)
    # Defaults are None here and filled by `resolve_options`, after the
    # environment has had its say: argparse cannot tell a default it filled in
    # from the same value typed on the command line.
    serve_cmd.add_argument("--host", default=None,
                           help="interface to bind (default: 127.0.0.1; 0.0.0.0 in a container)")
    serve_cmd.add_argument("--port", type=int, default=None, help="port to bind (default: 8000)")
    serve_cmd.add_argument("--workers", type=int, default=None,
                           help="worker loops (default: detected; 1 on the GIL build)")
    serve_cmd.add_argument("--reload", action="store_true", default=None,
                           help="restart when a watched file changes; for development")
    serve_cmd.add_argument("--reload-dir", action="append", default=[], metavar="DIR",
                           help="directory to watch, repeatable (default: the app directory)")
    serve_cmd.add_argument("--reload-include", action="append", default=[], metavar="GLOB",
                           help="also restart for files matching this, e.g. '*.html'")
    serve_cmd.add_argument("--tls-cert", metavar="FILE",
                           help="PEM certificate chain; serve HTTPS (needs --tls-key)")
    serve_cmd.add_argument("--tls-key", metavar="FILE", help="PEM private key for --tls-cert")
    serve_cmd.add_argument("--http2", action=argparse.BooleanOptionalAction, default=None,
                           help="serve HTTP/2 as well as HTTP/1.1 (default: on)")
    serve_cmd.add_argument("--access-log", action="store_true", default=None,
                           help="log every request")
    serve_cmd.add_argument("--log-level", default=None, type=str.lower,
                           help=f"one of {', '.join(LOG_LEVELS)} (default: info)")
    serve_cmd.add_argument("--max-concurrency", type=int, default=None,
                           help=f"default: {_app.DEFAULT_MAX_CONCURRENCY}")
    serve_cmd.add_argument("--max-connections", type=int, default=None,
                           help=f"default: {_app.DEFAULT_MAX_CONNECTIONS}")
    serve_cmd.add_argument("--max-body", type=int, default=None,
                           help=f"bytes (default: {_app.DEFAULT_MAX_BODY})")
    serve_cmd.add_argument("--max-message", type=int, default=None,
                           help=f"bytes (default: {_app.DEFAULT_MAX_MESSAGE})")
    serve_cmd.add_argument("--request-timeout", type=float, default=None,
                           help=f"seconds, 0 for none (default: {_app.DEFAULT_REQUEST_TIMEOUT:g})")
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

    settings_cmd = commands.add_parser(
        "settings", help="list the settings an app reads, and which are missing or invalid")
    target(settings_cmd)
    settings_cmd.add_argument("--env-file", **env_file)
    settings_cmd.add_argument("--json", action="store_true", help="print JSON instead of a table")
    settings_cmd.set_defaults(handler=settings)

    new_cmd = commands.add_parser(
        "new", help="start a project: an app, its tests, and AGENTS.md for coding assistants")
    new_cmd.add_argument("directory", help="where to create it; must not exist or be empty")
    new_cmd.add_argument("--name", help="the project's name (default: the directory's name)")
    new_cmd.set_defaults(handler=new)

    return main


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if args.command is None:
        cli.print_help()
        return 2
    from ._settings import SettingsError

    try:
        return args.handler(args)
    except (TargetError, UsageError, SettingsError) as exc:
        return fail(str(exc))
    except KeyboardInterrupt:
        return 0


def entry() -> None:
    """Console-script entry point for `oxbrook` and `oxb`."""
    sys.exit(main())
