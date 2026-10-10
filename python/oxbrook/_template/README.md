# {{title}}

An HTTP API built with [Oxbrook](https://ishangavidusha.github.io/oxbrook/).

## Run it

```bash
uv sync                                   # Python 3.14t and the dependencies
uv run oxbrook run app.main:app --reload --env-file .env.example
```

Then open <http://127.0.0.1:8000/docs>. Without uv: in a Python 3.14
environment, `pip install oxbrook pytest httpx`, and run the same commands
without `uv run`.

## Test it

```bash
uv run pytest
```

## Layout

```text
app/main.py       the App: settings, lifespan, routers
app/settings.py   configuration, read from APP_* environment variables
app/notes.py      the /notes routes and their models
tests/            pytest, against a real server
AGENTS.md         instructions for coding assistants working on this project
```
