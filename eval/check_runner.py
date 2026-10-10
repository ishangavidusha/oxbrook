"""Grade one finished project: import its app, serve it, run the task's checks.

Run by `eval/run.py` with the project's own interpreter, from inside the
project, so the app imports exactly as it would for its author:

    python check_runner.py TASK_CHECK_FILE

Prints one JSON object: whether the app imported (and the error if not), and
each check's name, outcome and detail.
"""

import importlib
import importlib.util
import json
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

TARGETS = ["main:app", "app.main:app", "app:app"]


def find_app():
    """The app, from the places the prompt allows, else any App in a top-level module."""
    sys.path.insert(0, str(Path.cwd()))
    from oxbrook import App

    errors = []
    for target in TARGETS:
        module_name, attribute = target.split(":")
        if importlib.util.find_spec(module_name.split(".")[0]) is None:
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception:
            errors.append(f"{target}: {traceback.format_exc(limit=3)}")
            continue
        app = getattr(module, attribute, None)
        if isinstance(app, App):
            return target, app, errors
    for path in sorted(Path.cwd().glob("*.py")):
        if path.stem in ("conftest", "check_runner") or path.stem.startswith("test"):
            continue
        try:
            module = importlib.import_module(path.stem)
        except Exception:
            errors.append(f"{path.stem}: {traceback.format_exc(limit=3)}")
            continue
        for name, value in vars(module).items():
            if isinstance(value, App):
                return f"{path.stem}:{name}", value, errors
    return None, None, errors


class Checks:
    def __init__(self, app) -> None:
        self.app = app
        self.results: list[dict] = []

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append({"check": name, "passed": bool(passed), "detail": detail[:500]})
        return passed

    @contextmanager
    def serve(self, workers: int):
        from oxbrook.testing import TestClient

        client = TestClient(self.app, workers=workers, request_timeout=10, shutdown_grace=2)
        client.start()
        try:
            yield client
        finally:
            client.stop()

    def run(self, name: str, fn) -> None:
        """One check; an exception is a failure with its message, not a crash."""
        started = time.monotonic()
        try:
            fn()
        except Exception as exc:
            self.record(name, False, f"{type(exc).__name__}: {exc}")
        else:
            if not any(r["check"] == name for r in self.results):
                self.record(name, True, f"{time.monotonic() - started:.2f}s")


def main() -> None:
    check_file = Path(sys.argv[1])
    out: dict = {"imported": False}
    try:
        target, app, errors = find_app()
    except Exception:
        target, app, errors = None, None, [traceback.format_exc(limit=5)]
    out["target"] = target
    out["import_errors"] = [e[-1500:] for e in errors]
    if app is None:
        print(json.dumps(out))
        return
    out["imported"] = True
    spec = importlib.util.spec_from_file_location("task_check", check_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checks = Checks(app)
    try:
        module.check(checks)
    except Exception:
        checks.record("server", False, traceback.format_exc(limit=5))
    out["checks"] = checks.results
    print(json.dumps(out))


if __name__ == "__main__":
    main()
