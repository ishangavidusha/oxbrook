#!/usr/bin/env python3
"""The `oxbrook` command.

Driven as a user drives it: real subprocesses, a real project directory, real
signals and real file edits. Three defects were found by doing exactly that
before this suite existed, and each has a case here:

* **SIGTERM killed the server outright.** Only Ctrl-C drained; the signal a
  container runtime or Kubernetes sends took the default action — exit 143, no
  drain, no lifespan teardown. The server now treats SIGTERM like SIGINT.
* **An edit saved while the server started was lost.** The reload supervisor
  took its first snapshot of the files after starting the server, so the edit
  was already in it and never counted as a change.
* **The supervisor could not be stopped.** Started in the background it
  inherited SIGINT as ignored, and hung. It now installs its own handlers for
  SIGINT and SIGTERM.

And two found while building it:

* **A process that had run a test client ignored SIGTERM.** Signal handlers stay
  installed for the life of a process, so once any server had run, the process
  survived SIGTERM and Ctrl-C. Signals are now handled only when serving from
  the main thread, as Python does; the test client serves from another.

* **A reload served the previous edit** (GIL build, three runs in four).
  Python reuses a `.pyc` whose recorded source time, in whole seconds, and size
  match. `VERSION = 2` then `VERSION = 3`
  saved within one second is the same size, so the restarted server loaded the
  stale bytecode. The supervisor now deletes the cached bytecode of changed files
  before restarting.
"""
import json
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

failures: list[str] = []
BIN = Path(sys.executable).parent
WINDOWS = sys.platform == "win32"
#: `CreateProcess` would supply this itself, but naming it keeps a failure
#: here from looking like a command that was never installed.
EXE = ".exe" if WINDOWS else ""

#: How a server is asked to stop, and how it must be started for that to be
#: possible. Windows has no SIGTERM to send and no way to deliver SIGINT to
#: another process: Ctrl-Break is the signal a supervisor sends there, and it
#: reaches a child only when the child has its own process group — which also
#: keeps it from reaching this test runner.
if WINDOWS:
    NEW_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP
    STOP_SIGNALS = [signal.CTRL_BREAK_EVENT]
else:
    NEW_GROUP = 0
    STOP_SIGNALS = [signal.SIGTERM, signal.SIGINT]


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def oxbrook(*args: str, cwd: Path, command: str = "oxbrook", timeout: float = 60):
    return subprocess.run([str(BIN / (command + EXE)), *args], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


def get(port: int) -> str | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            return response.read().decode()
    except OSError:
        return None


def wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


APP = '''
from contextlib import asynccontextmanager
from oxbrook import App, BodyStream, Request, Router

@asynccontextmanager
async def lifespan(app):
    yield
    print("lifespan teardown ran", flush=True)

app = App(title="CLI Test", lifespan=lifespan)
other = App(title="Other")
admin = Router(prefix="/admin")

@admin.middleware
async def guard(request, call_next):
    return await call_next(request)

@admin.get("/stats", tool=True)
async def stats(_: Request):
    """Admin statistics."""
    return {"ok": True}

@app.get("/")
async def root(_: Request):
    return {"version": VERSION}

@app.put("/upload")
async def upload(_: Request, body: BodyStream):
    return {}

@app.websocket("/live")
async def live(request, ws):
    await ws.close()

app.include(admin)
VERSION = 1

def make_app():
    return other
'''


def project() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="oxbrook-cli-"))
    (directory / "main.py").write_text(APP)
    (directory / "broken.py").write_text("import a_module_that_does_not_exist\n")
    return directory


def set_version(directory: Path, version: int, extra: str = "") -> None:
    """Rewrite main.py from the template, so a fix really removes a break."""
    (directory / "main.py").write_text(APP.replace("VERSION = 1", f"VERSION = {version}") + extra)


# ---------------------------------------------------------------------------
def both_commands_exist(directory: Path) -> None:
    for command in ("oxbrook", "oxb"):
        result = oxbrook("--version", cwd=directory, command=command)
        check(result.returncode == 0 and result.stdout.startswith("oxbrook "),
              f"`{command} --version` gave {result.returncode} {result.stdout!r} {result.stderr!r}")
    result = subprocess.run([sys.executable, "-m", "oxbrook", "--version"], cwd=directory,
                            capture_output=True, text=True, timeout=30)
    check(result.returncode == 0, f"`python -m oxbrook --version` failed: {result.stderr}")
    build = "GIL" if sys._is_gil_enabled() else "free-threaded"
    check(build in result.stdout, f"--version did not report the {build} build: {result.stdout!r}")


def routes_and_openapi(directory: Path) -> None:
    result = oxbrook("routes", "main:app", cwd=directory)
    check(result.returncode == 0, f"routes failed: {result.stderr}")
    table = result.stdout
    for expected in ("/admin/stats", "tool", "router middleware", "streams body",
                     "WS", "/live", "/openapi.json", "built in", "main.root"):
        check(expected in table, f"routes table is missing {expected!r}:\n{table}")

    listed = json.loads(oxbrook("routes", "main:app", "--json", cwd=directory).stdout)
    check({"method": "GET", "path": "/", "handler": "main.root", "notes": []} in listed,
          f"routes --json did not describe GET /: {listed[:2]}")

    result = oxbrook("openapi", "main", "-o", "spec.json", cwd=directory)
    check(result.returncode == 0, f"openapi -o failed: {result.stderr}")
    document = json.loads((directory / "spec.json").read_text())
    check(document["info"]["title"] == "CLI Test", "openapi used the wrong app")
    check("/admin/stats" in document["paths"], "openapi is missing a router's route")

    result = oxbrook("routes", "main:make_app", "--factory", cwd=directory)
    check(result.returncode == 0 and "/openapi.json" in result.stdout,
          f"--factory did not load the returned app: {result.stderr}")


def targets_fail_with_a_reason(directory: Path) -> None:
    for label, target, needle in [
        ("a missing module", "nosuch:app", "no module named 'nosuch'"),
        ("a missing attribute", "main:nope", "it defines app, other"),
        ("something that is not an App", "main:admin", "not an oxbrook.App"),
        ("a malformed target", "a:b:c", "module:attribute"),
    ]:
        result = oxbrook("routes", target, cwd=directory)
        check(result.returncode == 2, f"{label} exited {result.returncode}")
        check(needle in result.stderr, f"{label} said {result.stderr.strip()!r}")
        check("Traceback" not in result.stderr, f"{label} printed a traceback for a usage error")

    result = oxbrook("routes", "broken:app", cwd=directory)
    check(result.returncode != 0 and "a_module_that_does_not_exist" in result.stderr
          and "Traceback" in result.stderr,
          "an import error inside the app was reported as a usage error, hiding its traceback")


def run_stops_gracefully(directory: Path) -> None:
    """SIGTERM used to kill the server with no drain and no teardown."""
    for sig in STOP_SIGNALS:
        port = free_port()
        server = subprocess.Popen(
            [str(BIN / f"oxb{EXE}"), "run", "main:app", "--port", str(port), "--workers", "1",
             "--access-log"],
            cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            creationflags=NEW_GROUP,
        )
        try:
            started = wait_for(lambda port=port: get(port) is not None, 20)
            check(started, f"`oxb run` did not serve on port {port}")
            server.send_signal(sig)
            output, _ = server.communicate(timeout=20)
        finally:
            if server.poll() is None:
                server.kill()
                server.communicate()
        name = signal.Signals(sig).name
        check(server.returncode == 0, f"{name} made the server exit {server.returncode}")
        check("lifespan teardown ran" in output, f"{name} skipped lifespan teardown:\n{output}")
        check("GET / 200" in output, f"--access-log did not log the request:\n{output}")


SIGNALS_AFTER_CLIENT = '''
import os, signal, sys, time
from oxbrook import App, Request
from oxbrook.testing import TestClient
app = App(openapi_url=None, docs_url=None, mcp_url=None)
@app.get("/")
async def root(_: Request):
    return 1
with TestClient(app, workers=1) as client:
    client.get("/")
open("client-ran", "w").close()
if sys.platform != "win32":
    os.kill(os.getpid(), signal.SIGTERM)
time.sleep(5)
print("survived")
'''


def a_test_client_leaves_signals_alone(directory: Path) -> None:
    """A server that has run must not leave the process deaf to a stop signal.

    The process kills itself on Unix. On Windows nothing can send itself
    Ctrl-Break, and `os.kill` there is `TerminateProcess`, which no handler can
    refuse and which would prove nothing; so the signal comes from here, to a
    child in its own process group.
    """
    script = directory / "signals_after_client.py"
    script.write_text(SIGNALS_AFTER_CLIENT)
    child = subprocess.Popen([sys.executable, str(script)], cwd=directory,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             creationflags=NEW_GROUP)
    try:
        if WINDOWS:
            ran = directory / "client-ran"
            check(wait_for(lambda: ran.exists() or child.poll() is not None, 60),
                  "the script never reported that its client had run")
            child.send_signal(signal.CTRL_BREAK_EVENT)
        output, _ = child.communicate(timeout=60)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    expected = "not 0" if WINDOWS else f"-{signal.SIGTERM}"
    ended = child.returncode != 0 if WINDOWS else child.returncode == -signal.SIGTERM
    check(ended and "survived" not in output,
          f"after a test client ran, the stop signal no longer ended the process: exit "
          f"{child.returncode} (expected {expected}), {output.strip()!r}")


def reload_follows_edits(directory: Path) -> None:
    port = free_port()
    # Started with SIGINT ignored, as a background job in a shell is: the
    # supervisor must stop on SIGINT anyway.
    log_path = directory / "reload.log"
    log = log_path.open("w")
    started = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
        else {"preexec_fn": lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)}
    )
    supervisor = subprocess.Popen(
        [str(BIN / f"oxbrook{EXE}"), "run", "main:app", "--port", str(port), "--workers", "1",
         "--reload"],
        cwd=directory, stdout=log, stderr=subprocess.STDOUT, text=True, **started,
    )
    try:
        # Saved while the server is still starting: the edit that was lost.
        time.sleep(0.2)
        set_version(directory, 2)
        check(wait_for(lambda: get(port) == '{"version":2}', 20),
              f"an edit saved during startup never took effect; server says {get(port)!r}")

        set_version(directory, 3)
        check(wait_for(lambda: get(port) == '{"version":3}', 20),
              f"an ordinary edit did not reload; server says {get(port)!r}")

        set_version(directory, 4, extra="\nasync def broken(:\n")
        check(wait_for(lambda: get(port) is None, 20), "a syntax error left the old server up")
        time.sleep(1.0)
        check(supervisor.poll() is None, "a syntax error in the app stopped the supervisor")

        set_version(directory, 5)
        check(wait_for(lambda: get(port) == '{"version":5}', 20),
              f"fixing the syntax error did not bring the server back; says {get(port)!r}")

        supervisor.send_signal(STOP_SIGNALS[0])
        supervisor.wait(timeout=30)
        output = log_path.read_text()
        check(supervisor.returncode == 0, f"the supervisor exited {supervisor.returncode}")
        check(get(port) is None, "the server outlived its supervisor")
        check("changed; restarting" in output, f"reloads were not announced:\n{output}")
        check("exited with code" in output, f"the failed start was not reported:\n{output}")
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait()
            failures.append("the supervisor had to be killed")
        log.close()
        if failures:
            print("--- reload log ---\n" + log_path.read_text(), flush=True)


def main() -> None:
    directory = project()
    for step in (both_commands_exist, routes_and_openapi, targets_fail_with_a_reason,
                 run_stops_gracefully, a_test_client_leaves_signals_alone,
                 reload_follows_edits):
        try:
            step(directory)
            print(f"  {step.__name__}: ok", flush=True)
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR", flush=True)

    shutil.rmtree(directory, ignore_errors=True)

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
