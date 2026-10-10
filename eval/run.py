#!/usr/bin/env python3
"""Does a coding agent build a correct Oxbrook app? Measure it.

Each run gives a coding agent one task in a fresh directory, under one
condition, and grades what it leaves behind:

  released  an empty directory with the oxbrook on PyPI (0.3.0: no llms.txt,
            no `oxbrook new`), no web tools: what a user got before this work
  bare      the same with this tree's oxbrook, whose package carries the
            `oxbrook new` template and its AGENTS.md
  llms      the same, with one line in the prompt pointing at llms.txt
  scaffold  a project made by `oxbrook new`, whose AGENTS.md points at the docs

The grade is behavioural: the agent's app is imported and served by
`check_runner.py` with several worker loops, and the task's hidden checks run
against it — the same mistakes that pass a single-loop smoke test (a pool made
for one loop, a blocking call on the loop, a queue that cannot cross loops)
fail there. A static scan names the likely cause, and the transcript says what
the agent read and what it cost.

    python eval/run.py                                # every task and condition, once
    python eval/run.py --tasks forecast --conditions bare,scaffold --repeat 3

Needs: the `claude` CLI logged in, uv, Docker's PostgreSQL (`make up`), and a
built tree (the wheel is built from it). Results go to eval/results/<time>/.

The agent works in a directory under the system's temporary directory, never
inside this repository: Claude Code reads CLAUDE.md files from every parent
directory, and the repository holds both the maintainers' notes and the
reference solutions. Each finished project is copied back, without its venv.

**The agent runs in Claude Code's sandbox.** Unsandboxed, agents cleaning up
their test servers ran `pkill -f oxbrook` and `pkill -f python3`, which
reached every matching process on the machine — other runs, and anything of
the user's. In the sandbox a command cannot see or signal other processes,
cannot write outside its directory, cannot read this repository, and reaches
the network only on localhost (the docs, PostgreSQL, its own server). Package
indexes are not reachable through it, so what the tasks need is installed
first. Runs go one at a time by default, because agents all pick port 8000.
"""

import argparse
import ast
import concurrent.futures
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
TASKS = EVAL / "tasks"
PUBLIC_DOCS = "https://ishangavidusha.github.io/oxbrook/"
POSTGRES = "postgresql://oxbrook:oxbrook@127.0.0.1:5499/{db}"
#: Installed into every run's environment: the network is closed to package
#: indexes inside the sandbox.
PACKAGES = ["pytest", "pytest-asyncio", "httpx", "asyncpg", "requests", "websockets"]
#: Short names to exact models: the CLI's own aliases can lag the newest model.
MODELS = {"sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5",
          "opus": "claude-opus-5-5"}


def sandbox_settings() -> str:
    return json.dumps({
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "network": {"allowLocalBinding": True,
                        "allowedDomains": ["localhost", "127.0.0.1"]},
            "filesystem": {"denyRead": [str(ROOT)]},
        },
        # The Read tool is not the shell; it is denied the repository separately.
        "permissions": {"deny": [f"Read(/{ROOT}/**)", f"Grep(/{ROOT}/**)",
                                 f"Glob(/{ROOT}/**)"]},
    })
CONDITIONS = ("released", "bare", "llms", "scaffold")
#: What `released` installs: the last release before the agent work.
RELEASED = "oxbrook==0.3.0"

#: How a careful developer asks. `--terse` drops it, closer to someone who
#: would not think to ask for tests.
COMMON = """
The Python environment is already active: `python`, `pytest` and `oxbrook` run
from `.venv`, where oxbrook is installed with pytest, pytest-asyncio, httpx,
asyncpg, requests and websockets. There is no access to package indexes, so
use what is installed. The app must be importable as `app`
from `main.py`, or from `app/main.py` in a project made by `oxbrook new`.
Write tests for it and make sure they pass before you finish. Work on your
own until it is done: nobody will answer questions.
"""

TERSE = """
The Python environment is active, with oxbrook and common packages installed,
and no access to package indexes. Put the app in
`main.py` (or `app/main.py` in a project made by `oxbrook new`) as `app`.
"""

PREAMBLE = {
    "released": "Use Oxbrook, a Python web framework (the `oxbrook` package).",
    "bare": "Use Oxbrook, a Python web framework (the `oxbrook` package).",
    "llms": "Use Oxbrook, a Python web framework (the `oxbrook` package). Its "
            "documentation for coding assistants is at {docs}llms.txt.",
    "scaffold": "This directory is a project started with `oxbrook new`. Build the "
                "following in it, with Oxbrook.",
}


# ---------------------------------------------------------------------------
# the session: a wheel, the docs, a database
# ---------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_wheel(out: Path) -> Path:
    out.mkdir(parents=True)
    subprocess.run([str(ROOT / ".venv/bin/maturin"), "build", "--release", "-q",
                    "-i", str(ROOT / ".venv/bin/python"), "-o", str(out)],
                   cwd=ROOT, check=True)
    return next(out.glob("*.whl"))


def build_docs(out: Path, base: str) -> None:
    """The site as it will be published, with its own address swapped for a
    local one: the published site follows releases, so it does not have what
    this tree documents yet."""
    subprocess.run([str(ROOT / ".venv-gil/bin/python"), "-m", "mkdocs", "build", "-q",
                    "-d", str(out)], cwd=ROOT, check=True)
    for path in list(out.rglob("*.md")) + list(out.rglob("*.txt")):
        text = path.read_text(encoding="utf-8")
        if PUBLIC_DOCS in text:
            path.write_text(text.replace(PUBLIC_DOCS, base), encoding="utf-8")


def create_database(name: str) -> None:
    subprocess.run(["docker", "exec", "oxbrook-postgres", "sh", "-c",
                    f"dropdb -U oxbrook --if-exists {name} && createdb -U oxbrook {name}"],
                   check=True, capture_output=True)


def agent_environment(venv: Path, database: str) -> dict[str, str]:
    """The environment of a developer's terminal with the venv activated.

    Everything the host session set for its own agent is removed, so the run
    gets the CLI's own login and none of this session's settings or tools.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "MCP_", "VIRTUAL_ENV", "PYTHON"))
           and k not in ("ANTHROPIC_BASE_URL",)}
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["DATABASE_URL"] = database
    return env


# ---------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------
def prepare(run_dir: Path, task: str, condition: str, wheel: Path, docs: str) -> Path:
    project = run_dir / "project"
    project.mkdir(parents=True)
    if condition == "scaffold":
        # Before the venv: `oxbrook new` wants an empty directory. The tree's
        # own oxbrook is the wheel's version.
        subprocess.run([str(ROOT / ".venv/bin/python"), "-m", "oxbrook", "new", ".",
                        "--name", task], cwd=project, check=True, capture_output=True)
    venv = project / ".venv"
    subprocess.run(["uv", "venv", "-q", "--python", str(ROOT / ".venv/bin/python"), str(venv)],
                   check=True)
    oxbrook = RELEASED if condition == "released" else str(wheel)
    subprocess.run(["uv", "pip", "install", "-q", "--python", str(venv / "bin/python"),
                    oxbrook, *PACKAGES], check=True)
    if condition == "scaffold":
        agents = project / "AGENTS.md"
        agents.write_text(agents.read_text().replace(PUBLIC_DOCS, docs))
        # Stands in for the release: `uv sync` would otherwise fetch the
        # published oxbrook, which predates what the template uses.
        pyproject = project / "pyproject.toml"
        pyproject.write_text(pyproject.read_text()
                             + f'\n[tool.uv.sources]\noxbrook = {{ path = "{wheel}" }}\n')
    for fixture in TASKS.glob(f"{task}/*.py"):
        if fixture.name != "check.py":
            shutil.copy(fixture, project / fixture.name)
    return project


def prompt_for(task: str, condition: str, docs: str, terse: bool) -> str:
    preamble = PREAMBLE[condition].format(docs=docs)
    body = (TASKS / task / "prompt.md").read_text()
    return f"{preamble}\n\n{body}\n{TERSE if terse else COMMON}"


def run_agent(project: Path, prompt: str, condition: str, env: dict, args, log: Path) -> dict:
    disallowed = ["WebSearch"] + (["WebFetch"] if condition in ("bare", "released") else [])
    command = [
        "claude", "-p", prompt, "--model", MODELS.get(args.model, args.model),
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions",
        "--setting-sources", "project",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--no-session-persistence",
        "--settings", sandbox_settings(),
        "--max-budget-usd", str(args.budget),
        "--disallowedTools", *disallowed,
    ]
    started = time.monotonic()
    with log.open("w") as out:
        try:
            subprocess.run(command, cwd=project, env=env, stdout=out, stderr=subprocess.STDOUT,
                           timeout=args.minutes * 60)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
    return {"seconds": round(time.monotonic() - started), "timed_out": timed_out}


def read_transcript(log: Path) -> dict:
    """What the agent did, from its stream: cost, turns, docs it read."""
    info: dict = {"cost_usd": None, "turns": None, "fetched": [], "read_docs": [],
                  "tool_calls": 0, "bash": 0, "is_error": None, "left_its_directory": []}
    for line in log.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            info["cost_usd"] = event.get("total_cost_usd")
            info["turns"] = event.get("num_turns")
            info["is_error"] = event.get("is_error")
            info["subtype"] = event.get("subtype")
            info["models"] = sorted(event.get("modelUsage") or {})
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            info["tool_calls"] += 1
            name, data = block.get("name"), block.get("input", {})
            # Nothing stops a `find /` reaching this repository, with its
            # reference solutions; a run that touched it is marked, not trusted.
            raw = json.dumps(data)
            if str(ROOT) in raw or "reference/good" in raw or "reference/bad" in raw:
                info["left_its_directory"].append(raw[:200])
            if name == "Bash" and "_template" in data.get("command", ""):
                info["read_docs"].append("the oxbrook new template (bash)")
            if name == "WebFetch":
                info["fetched"].append(data.get("url"))
            elif name == "Bash":
                info["bash"] += 1
                command = data.get("command", "")
                if "curl" in command or "wget" in command:
                    info["fetched"].append(command[:200])
                if "site-packages/oxbrook" in command:
                    info["read_docs"].append("package source (bash)")
            elif name == "Read":
                path = data.get("file_path", "")
                if path.endswith("AGENTS.md") or "site-packages/oxbrook" in path:
                    info["read_docs"].append(path.split("site-packages/")[-1])
    return info


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------
def sources(project: Path) -> list[Path]:
    skip = {".venv", "tests", "__pycache__"}
    return [p for p in project.rglob("*.py")
            if not skip & set(p.relative_to(project).parts)
            and not p.name.startswith("test_") and p.name != "conftest.py"
            and p.name != "weatherlib.py"]


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return ""


def static_flags(project: Path) -> list[str]:
    """Known mistakes, by reading the code. A hint at the cause of a failed
    check, never a grade on its own: the checks are."""
    flags: set[str] = set()
    for path in sources(project):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            flags.add(f"syntax error in {path.name}")
            continue
        lifespan_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [
                    node.module or ""]
                if any(n.split(".")[0] in ("fastapi", "starlette", "flask") for n in names):
                    flags.add("imports another framework")
            if isinstance(node, ast.Call) and dotted(node.func).endswith("App"):
                for kw in node.keywords:
                    if kw.arg == "lifespan" and isinstance(kw.value, ast.Name):
                        lifespan_names.add(kw.value.id)
            if isinstance(node, ast.Attribute) and dotted(node) in ("os.environ", "os.getenv"):
                flags.add("reads os.environ")
        for node in tree.body:
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and not isinstance(node, (
                        ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and dotted(call.func).split(".")[-1] in (
                            "create_pool", "create_async_engine", "AsyncClient")):
                    flags.add("async pool or client made at import")
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call):
                    continue
                name = dotted(call.func)
                if fn.name in lifespan_names and name.split(".")[-1] == "create_pool":
                    flags.add("pool made in the process-wide lifespan")
                if isinstance(fn, ast.AsyncFunctionDef) and name in (
                        "weatherlib.get_forecast", "get_forecast", "time.sleep",
                        "requests.get", "requests.post"):
                    flags.add(f"blocking call in async def: {name}")
    return sorted(flags)


def grade(project: Path, task: str, env: dict, fixtures: dict[str, str]) -> dict:
    python = str(project / ".venv/bin/python")
    out: dict = {"static": static_flags(project)}
    tampered = [name for name, digest in fixtures.items()
                if hashlib.sha256((project / name).read_bytes()).hexdigest() != digest]
    if tampered:
        out["static"].append(f"changed the vendor's {', '.join(tampered)}")
    try:
        checked = subprocess.run([python, str(EVAL / "check_runner.py"),
                                  str(TASKS / task / "check.py")],
                                 cwd=project, env=env, capture_output=True, text=True,
                                 timeout=300)
        out.update(json.loads(checked.stdout.strip().splitlines()[-1]))
    except Exception as exc:
        out.update({"imported": False, "import_errors": [f"grader: {exc}"]})
    try:
        own = subprocess.run([python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
                             cwd=project, env=env, capture_output=True, text=True, timeout=300)
        tail = (own.stdout.strip().splitlines() or [""])[-1]
        out["own_tests"] = {"passed": own.returncode == 0, "summary": tail[:200]}
    except subprocess.TimeoutExpired:
        out["own_tests"] = {"passed": False, "summary": "timed out"}
    checks = out.get("checks", [])
    out["passed"] = sum(c["passed"] for c in checks)
    out["total"] = len(checks)
    return out


def one_run(spec: tuple, session: Path, out: Path, wheel: Path, docs: str, args) -> dict:
    task, condition, n = spec
    run_id = f"{task}-{condition}-{n}"
    run_dir = session / run_id
    database = f"eval_{session.name.replace('-', '_')}_{task}_{condition}_{n}"
    create_database(database)
    project = prepare(run_dir, task, condition, wheel, docs)
    fixtures = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in TASKS.glob(f"{task}/*.py") if p.name != "check.py"}
    env = agent_environment(project / ".venv", POSTGRES.format(db=database))
    prompt = prompt_for(task, condition, docs, args.terse)
    (run_dir / "prompt.md").write_text(prompt)
    print(f"  start {run_id}", flush=True)
    agent = run_agent(project, prompt, condition, env, args, run_dir / "transcript.jsonl")
    agent.update(read_transcript(run_dir / "transcript.jsonl"))
    result = {"run": run_id, "task": task, "condition": condition, "model": args.model,
              "terse": args.terse,
              "agent": agent, "grade": grade(project, task, env, fixtures)}
    (run_dir / "result.json").write_text(json.dumps(result, indent=2))
    shutil.copytree(run_dir, out / run_id, ignore=shutil.ignore_patterns(
        ".venv", "__pycache__", ".pytest_cache"))
    g = result["grade"]
    print(f"  done  {run_id}: {g['passed']}/{g['total']} checks, "
          f"{agent['seconds']}s, ${agent['cost_usd'] or 0:.2f}", flush=True)
    return result


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------
def report(results: list[dict], session: Path, args) -> str:
    lines = [f"# Agent eval {session.name}", "",
             f"Model `{MODELS.get(args.model, args.model)}`, "
             f"{'terse' if args.terse else 'full'} prompt, "
             f"{args.repeat} run(s) per cell, "
             f"oxbrook from this tree. Checks pass / total; `!` marks a run that "
             f"touched the repository and is not comparable.", ""]
    tasks = sorted({r["task"] for r in results})
    conditions = [c for c in CONDITIONS if any(r["condition"] == c for r in results)]
    lines.append("| task | " + " | ".join(conditions) + " |")
    lines.append("|---|" + "---|" * len(conditions))
    for task in tasks:
        row = [task]
        for condition in conditions:
            cell = [r for r in results if r["task"] == task and r["condition"] == condition]
            row.append(", ".join(
                (f"{r['grade']['passed']}/{r['grade']['total']}" if r["grade"].get("imported")
                 else "no app") + ("!" if r["agent"].get("left_its_directory") else "")
                for r in cell))
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "## Runs", ""]
    for r in sorted(results, key=lambda r: r["run"]):
        g, a = r["grade"], r["agent"]
        failed = [c["check"] for c in g.get("checks", []) if not c["passed"]]
        lines.append(f"### {r['run']}")
        lines.append(f"- checks {g['passed']}/{g['total']}"
                     + (f"; failed: {', '.join(failed)}" if failed else ""))
        if not g.get("imported"):
            lines.append(f"- app did not import: {(g.get('import_errors') or ['?'])[0][-300:]}")
        if g.get("static"):
            lines.append(f"- flags: {'; '.join(g['static'])}")
        own = g.get("own_tests", {})
        lines.append(f"- own tests: {'pass' if own.get('passed') else 'fail'} "
                     f"({own.get('summary', '')})")
        lines.append(f"- {a['seconds']}s, {a.get('turns')} turns, "
                     f"${a.get('cost_usd') or 0:.2f}"
                     + (", timed out" if a.get("timed_out") else ""))
        if a.get("fetched"):
            lines.append(f"- fetched: {', '.join(str(u) for u in a['fetched'][:8])}")
        if a.get("left_its_directory"):
            lines.append(f"- **touched the repository, not comparable**: "
                         f"{a['left_its_directory'][0]}")
        if a.get("read_docs"):
            lines.append(f"- read: {', '.join(sorted(set(a['read_docs']))[:8])}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks", default=",".join(sorted(p.name for p in TASKS.iterdir()
                                                           if p.is_dir())))
    parser.add_argument("--conditions", default=",".join(CONDITIONS))
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--terse", action="store_true",
                        help="the request alone, without asking for tests")
    parser.add_argument("--parallel", type=int, default=1,
                        help="runs at once; above 1, agents' servers can collide on a port")
    parser.add_argument("--budget", type=float, default=5.0, help="USD per run, at most")
    parser.add_argument("--minutes", type=float, default=25, help="per run, at most")
    args = parser.parse_args()

    tasks = args.tasks.split(",")
    conditions = args.conditions.split(",")
    for name in tasks:
        if not (TASKS / name / "prompt.md").exists():
            parser.error(f"no task {name!r}")
    for name in conditions:
        if name not in CONDITIONS:
            parser.error(f"no condition {name!r}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = EVAL / "results" / stamp
    out.mkdir(parents=True)
    session = Path(tempfile.gettempdir()).resolve() / "oxbrook-eval" / stamp
    session.mkdir(parents=True)
    print(f"session {session}\nresults {out}", flush=True)
    wheel = build_wheel(session / "wheel")
    port = free_port()
    docs = f"http://127.0.0.1:{port}/"
    build_docs(session / "site", docs)
    server = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind",
                               "127.0.0.1", "--directory", str(session / "site")],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    specs = [(t, c, n) for n in range(1, args.repeat + 1) for t in tasks for c in conditions]
    results = []
    try:
        with concurrent.futures.ThreadPoolExecutor(args.parallel) as pool:
            futures = [pool.submit(one_run, s, session, out, wheel, docs, args) for s in specs]
            for future in concurrent.futures.as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    print(f"  run failed: {type(exc).__name__}: {exc}", flush=True)
    finally:
        server.terminate()
    text = report(results, out, args)
    (out / "report.md").write_text(text)
    (out / "results.json").write_text(json.dumps(results, indent=2))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
