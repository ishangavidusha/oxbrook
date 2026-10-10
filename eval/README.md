# Agent eval

Does a coding agent build a correct Oxbrook app, and what does it need to get
there? Oxbrook is newer than most models' training data, and it resembles
FastAPI closely enough that an agent's guesses compile and then fail in ways a
single request does not show. This measures that, task by task.

```bash
make up                                   # PostgreSQL, for the bookmarks task
python eval/run.py                        # every task, every condition, once
python eval/run.py --tasks forecast --conditions bare,scaffold --repeat 3
python eval/run.py --model haiku --terse  # a smaller model, asked as a non-expert would
```

Needs the `claude` CLI, logged in, and uv. The wheel and the docs site are
built from this tree at the start of a session. Results land in
`eval/results/<time>/`: `report.md`, `results.json`, and per run the prompt,
the agent's transcript, the grade and the project it left.

## Conditions

| condition | the agent starts with |
|---|---|
| `released` | an empty directory with oxbrook 0.3.0 from PyPI, before any of this; no web tools |
| `bare` | an empty directory with this tree's oxbrook, which carries the `oxbrook new` template; no web tools |
| `llms` | the same, and one line in the prompt naming `llms.txt` |
| `scaffold` | a project made by `oxbrook new`, whose `AGENTS.md` points at the docs |

The documentation is this tree's, served locally, since the published site
follows releases.

## Tasks

Each task is an ordinary request with one trap in it, the kind a single-loop
smoke test does not catch.

| task | asks for | the trap |
|---|---|---|
| `bookmarks` | a CRUD API on PostgreSQL | one connection pool for the process, when each worker loop needs its own |
| `forecast` | an API over a synchronous vendor SDK | a blocking call inside `async def`, which stalls every request on the loop |
| `scores` | a live scoreboard over Server-Sent Events | a per-watcher `asyncio.Queue`, which cannot be woken from another loop |

## Grading

The agent's app is imported and served with several worker loops by
`check_runner.py`, and `tasks/<task>/check.py` runs against it: the behaviour
the prompt specified, then the trap — concurrent requests across loops, a
`/ping` timed while slow work runs, watchers on different loops. A static scan
of the code names the likely cause of a failure (a pool made at import, a
blocking call in `async def`, a changed vendor file); it explains a grade and
never sets one. The agent's own tests are run too, and reported.

Each task's `reference/good` passes every check and `reference/bad` makes the
task's mistake and fails; run the grader against both after changing a check.

The agent runs outside this repository, in the system's temporary directory,
so no instruction file above it is read, with none of the host's settings,
plugins or MCP servers.

**It runs in Claude Code's sandbox,** and must. Unsandboxed, agents tidying up
their test servers ran `pkill -f oxbrook` and `pkill -f python3`, which reach
every matching process on the machine: other runs, and anything else the user
had open. Sandboxed, a command cannot list or signal other processes, write
outside its directory or read this repository, and its network is localhost
only — the docs, PostgreSQL, its own server. Package indexes are therefore out
of reach, and what the tasks need is installed beforehand. Runs go one at a
time by default, since agents all reach for port 8000.
