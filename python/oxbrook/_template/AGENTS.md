# Instructions for coding assistants

This is an HTTP API built with **Oxbrook**, a Python web framework with a Rust
core. Oxbrook is probably not in your training data. It resembles FastAPI and
differs from it in ways that break code written from FastAPI habits, so do not
guess its API: **read the documentation first.**

- Index of every page: <https://ishangavidusha.github.io/oxbrook/llms.txt>
- The whole documentation and API reference in one file:
  <https://ishangavidusha.github.io/oxbrook/llms-full.txt>
- Any page as Markdown: its URL with `.md` for the trailing slash, such as
  <https://ishangavidusha.github.io/oxbrook/guide/database.md>

## Commands

```bash
uv sync                                    # install
uv run pytest                              # test: run after every change
uv run oxbrook run app.main:app --reload --env-file .env.example   # serve
uv run oxbrook routes app.main:app         # list the routes
uv run oxbrook settings app.main:app       # list the settings and what is missing
```

## Rules that differ from FastAPI

1. **Every handler takes the request as its first argument**, before path,
   query and body parameters:

   ```python
   @router.get("/{note_id}")
   async def get_note(request: Request, note_id: int): ...
   ```

   Name it `_` when unused; it is still passed.

2. **Handlers are `async def`.** A plain `def` handler is refused at startup.
   Never call blocking I/O (`requests`, `time.sleep`, a synchronous database
   driver, a large file read) inside an `async def` handler: it stalls every
   request on that event loop. For unavoidably blocking code, declare the
   route `blocking=True` and write the handler as a plain `def`.

3. **Async clients and connection pools are made in `worker_lifespan`, never
   at module level or in `lifespan`.** The server runs several event loops on
   several threads, and an async pool belongs to the loop that made it.
   `worker_lifespan` runs once per loop; what it yields is `request.state`:

   ```python
   @asynccontextmanager
   async def worker_lifespan(app):
       pool = await asyncpg.create_pool(DSN, max_size=app.per_worker(20))
       try:
           yield {"db": pool}
       finally:
           await pool.close()

   app = App(..., worker_lifespan=worker_lifespan)
   # in a handler: request.state.db
   ```

   `lifespan` runs once per process, for things that are not tied to a loop
   (like `NoteStore` here). Plain Python state shared across loops is touched
   from several threads at once: guard it with a lock, as `NoteStore` does.

4. **Responses:** return a dict, a list, a pydantic model or `None` (204). For
   another status or headers, return `Reply(value, status=201, headers={...})`.
   For an error, `raise HTTPError(404, "message for the client")`. Never put
   exception text or internal detail in a response.

5. **Parameters:** a pydantic model argument is the JSON body; scalar
   arguments are path parameters if named in the path, query parameters
   otherwise. Use `Depends(...)` for shared setup and teardown; see the
   dependencies guide, as its rules are not FastAPI's.

6. **Configuration** goes in `app/settings.py` as fields on `AppSettings`,
   read from `APP_*` environment variables. Do not read `os.environ` in the
   code. Type secrets as `pydantic.SecretStr`. Add new variables to
   `.env.example`.

7. **`tool=True` exposes a route to AI agents** over MCP at `/mcp`. Only add it
   to routes that are safe for an agent to call; never to one that deletes or
   pays.

8. **Tests use `TestClient`**, which runs the real server (see
   `tests/conftest.py`). Add a test for every route you add or change, and
   run `uv run pytest` before finishing.

## Where to look in the documentation

| Task | Page |
|---|---|
| Routes, path and query parameters | `guide/routing.md`, `guide/parameters.md` |
| Request bodies, forms, uploads | `guide/bodies.md`, `guide/forms.md` |
| Organizing routes across modules | `guide/routers.md` |
| A database | `guide/database.md`, `guide/lifespan.md` |
| Login, API keys, tokens | `guide/auth.md` |
| Blocking libraries | `guide/blocking.md` |
| Sending email or other work after replying | `guide/after-response.md` |
| Errors and limits | `guide/errors.md` |
| Live updates (SSE, WebSockets) | `streams/topics.md`, `streams/sse.md`, `streams/websockets.md` |
| Deploying | `running.md` |

Each is under <https://ishangavidusha.github.io/oxbrook/>.
