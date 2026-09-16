# Install

```bash
pip install oxbrook
```

## Requirements

- **CPython 3.14.** The free-threaded build (`python3.14t`) is the primary
  target and runs several worker loops; the standard build runs one.
- **Linux or macOS.** Wheels are published for both interpreter builds on
  x86-64 and ARM (manylinux 2.28 and macOS 11 or newer). Elsewhere pip builds
  from the source distribution, which needs a Rust toolchain. Windows is not
  supported: the core relies on Unix sockets and signals.
- **Redis**, only for durable topics: `pip install oxbrook redis`.

With [uv](https://docs.astral.sh/uv/), a free-threaded environment is one
command:

```bash
uv venv --python 3.14t
uv pip install oxbrook
```

Check which build is running with `oxbrook --version`.

## Versions

Oxbrook is alpha. While the version is `0.x`:

- a **patch** release (`0.1.1`) fixes bugs and never changes the API;
- a **minor** release (`0.2.0`) may change the API, and lists every change
  that breaks existing code under **Breaking** in the
  [changelog](changelog.md).

Pin a minor version, `oxbrook~=0.1.0`, to take fixes without changes. `1.0`
will mean the API is committed to.

## From source

Building from the repository needs a Rust toolchain, uv, and Docker if the
suites should cover durable topics:

```bash
git clone https://github.com/ishangavidusha/oxbrook
cd oxbrook
make venvs     # .venv (free-threaded 3.14t) and .venv-gil (standard 3.14)
make build     # maturin develop --release into both
make run       # examples/hello.py
```

Rebuild after any change to `src/`; the `make` targets that need it already
do. The contributing guide covers the test suites and conventions.

## First app

```python
from oxbrook import App, Request

app = App(title="Notes", version="1.0.0")

@app.get("/notes/{note_id}")
async def read_note(_: Request, note_id: int):
    """Read one note by its id."""
    return {"id": note_id, "title": "hello"}

if __name__ == "__main__":
    app.run(port=8000)
```

```bash
oxbrook run app:app --reload     # or: python app.py
```

Installing Oxbrook puts two commands on the environment's path, `oxbrook` and
its short form `oxb`. See [Running a server](running.md) for what they do.

That gives you, without further configuration:

- `GET /notes/1` returning JSON, and `HEAD` on the same path
- `422` for `GET /notes/abc`, produced in Rust before Python is woken
- `405` with an `Allow` header for `DELETE /notes/1`
- `GET /openapi.json` and a documentation page at `GET /docs`
- `POST /mcp` speaking the Model Context Protocol, exposing nothing until a
  route asks to be exposed

## Services

Services run in containers rather than on the host.

```bash
make up      # start redis
make down    # stop it and remove its volume
make stack   # build the app image and run two nodes against one redis
```

## Handler rules

Handlers are `async def`. A synchronous handler is a `TypeError` at
registration rather than a surprise at runtime, because a blocking call on a
worker loop stalls every request that loop is carrying.

```python
@app.get("/bad")
def wrong(_: Request):      # TypeError: handlers must be async def
    return {}
```
