#!/usr/bin/env python3
"""`oxbrook new`: the project it writes works, and tells an agent the truth.

The promise is a project that passes its own tests the moment it exists, so
that is checked by running them: the generated pytest suite, against the
generated app, on a real server, from a subprocess in the new directory. Then
`oxbrook routes` and `oxbrook settings` against it, as its README says to.

Held to account too: the names it refuses and the directories it will not
touch, the dependency it pins, and AGENTS.md — every documentation page it
sends an agent to exists, and its links point at the site this repository
builds. An instruction file that sends an agent to a page that is not there
is worse than none.
"""
import json
import re
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

from oxbrook import _cli

failures: list[str] = []
ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {
    ".env.example", ".gitignore", ".python-version", "AGENTS.md", "CLAUDE.md", "README.md",
    "pyproject.toml", "app/__init__.py", "app/main.py", "app/notes.py", "app/settings.py",
    "tests/conftest.py", "tests/test_notes.py",
}


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def oxbrook(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "oxbrook", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=120)


def files(directory: Path) -> set[str]:
    return {p.relative_to(directory).as_posix() for p in directory.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts}


def makes_a_project_that_passes_its_own_tests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        made = oxbrook("new", "notes-api", cwd=Path(tmp))
        project = Path(tmp) / "notes-api"
        check(made.returncode == 0, f"oxbrook new failed: {made.stderr}")
        check("uv run pytest" in made.stdout, f"no next steps printed: {made.stdout!r}")
        check(files(project) == EXPECTED,
              f"unexpected files: extra {files(project) - EXPECTED}, "
              f"missing {EXPECTED - files(project)}")
        for path in project.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                check("{{" not in text, f"{path.name} kept a placeholder")
        pyproject = (project / "pyproject.toml").read_text()
        check('name = "notes-api"' in pyproject, "the project name was not filled in")
        check(f'"{_cli.requirement()}"' in pyproject, "the oxbrook requirement was not pinned")
        check('title="Notes Api"' in (project / "app/main.py").read_text(),
              "the title was not derived from the name")

        tests = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                               cwd=project, capture_output=True, text=True, timeout=180)
        check(tests.returncode == 0 and "4 passed" in tests.stdout,
              f"the new project's own tests failed:\n{tests.stdout[-2000:]}{tests.stderr[-2000:]}")

        listed = oxbrook("routes", "app.main:app", "--json", cwd=project)
        check(listed.returncode == 0, f"oxbrook routes failed: {listed.stderr}")
        if listed.returncode == 0:
            routes = {(r["method"], r["path"]): r for r in json.loads(listed.stdout)}
            for key in [("GET", "/notes"), ("POST", "/notes"), ("GET", "/notes/{note_id}"),
                        ("PUT", "/notes/{note_id}"), ("DELETE", "/notes/{note_id}")]:
                check(key in routes, f"{key} is not routed")
            tools = sorted(k for k, r in routes.items() if "tool" in r.get("notes", []))
            check(tools == [("GET", "/notes"), ("GET", "/notes/{note_id}")],
                  f"the tools offered to agents are {tools}; only the reads should be")

        settings = oxbrook("settings", "app.main:app", cwd=project)
        check(settings.returncode == 0 and "APP_DEBUG" in settings.stdout,
              f"oxbrook settings did not list APP_DEBUG: {settings.stdout}{settings.stderr}")


def refuses_what_it_should() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        occupied = base / "occupied"
        occupied.mkdir()
        (occupied / "mine.txt").write_text("keep me")
        result = oxbrook("new", "occupied", cwd=base)
        check(result.returncode != 0 and "not empty" in result.stderr,
              f"a non-empty directory was not refused: {result.returncode} {result.stderr}")
        check(files(occupied) == {"mine.txt"}, "a refused directory was written to")

        (base / "a-file").write_text("")
        result = oxbrook("new", "a-file", cwd=base)
        check(result.returncode != 0, "a path that is a file was not refused")

        for bad in ("-flag-like", "has space", "trailing-", "ünïcode"):
            result = oxbrook("new", "somewhere", "--name", bad, cwd=base)
            check(result.returncode != 0 and "--name" in result.stderr,
                  f"the name {bad!r} was accepted: {result.stdout}{result.stderr}")
        check(not (base / "somewhere").exists(), "a refused name still created the directory")

        # An empty directory is fine, including the one you are in: `oxb new .`
        here = base / "My_Service.v2"
        here.mkdir()
        result = oxbrook("new", ".", cwd=here)
        check(result.returncode == 0, f"oxbrook new . in an empty directory failed: "
                                      f"{result.stderr}")
        check('name = "my_service.v2"' in (here / "pyproject.toml").read_text(),
              "the name was not taken from the current directory")
        check('title="My Service V2"' in (here / "app/main.py").read_text(),
              "the title was not derived from the directory name")

        named = base / "dir"
        result = oxbrook("new", str(named), "--name", "billing", cwd=base)
        check(result.returncode == 0
              and 'name = "billing"' in (named / "pyproject.toml").read_text(),
              "--name did not name the project")


def pins_this_minor_version() -> None:
    real = metadata.version
    cases = {
        "0.4.2": "oxbrook>=0.4,<0.5",
        "0.4.0.dev3": "oxbrook>=0.4,<0.5",
        "1.2.0": "oxbrook>=1.2,<2",
        "unparseable": "oxbrook",
    }
    try:
        for installed, expected in cases.items():
            metadata.version = lambda _name, v=installed: v
            check(_cli.requirement() == expected,
                  f"version {installed} pinned as {_cli.requirement()!r}, not {expected!r}")
    finally:
        metadata.version = real


def agents_md_tells_the_truth() -> None:
    template = ROOT / "python" / "oxbrook" / "_template"
    agents = (template / "AGENTS.md").read_text()
    site = re.search(r"^site_url:\s*(\S+)", (ROOT / "mkdocs.yml").read_text(), re.M)
    if not site:
        check(False, "mkdocs.yml has no site_url")
        return
    base = site.group(1)
    for url in re.findall(r"https?://[^\s>)`]+", agents):
        check(url.startswith(base), f"AGENTS.md links {url}, outside the docs site {base}")
    check(f"{base}llms.txt" in agents and f"{base}llms-full.txt" in agents,
          "AGENTS.md does not point at llms.txt and llms-full.txt")
    pages = set(re.findall(r"`((?:guide|streams)/[\w-]+\.md|[\w-]+\.md)`", agents))
    pages |= {u.removeprefix(base) for u in re.findall(r"https?://\S+?\.md", agents)}
    pages -= {"AGENTS.md"}
    check(len(pages) >= 10, f"only {len(pages)} documentation pages found in AGENTS.md")
    for page in sorted(pages):
        check((ROOT / "www" / page).is_file(), f"AGENTS.md sends an agent to {page}, "
                                                "which is not a page of the site")
    check((template / "CLAUDE.md").read_text().strip() == "@AGENTS.md",
          "CLAUDE.md does not import AGENTS.md")


def main() -> None:
    for step in (makes_a_project_that_passes_its_own_tests, refuses_what_it_should,
                 pins_this_minor_version, agents_md_tells_the_truth):
        try:
            step()
            print(f"  {step.__name__}: ok", flush=True)
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR", flush=True)

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
