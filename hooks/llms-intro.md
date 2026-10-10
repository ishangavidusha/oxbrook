# Oxbrook

> A Python REST framework with a Rust core, built-in reactive streams (topics, SSE, WebSockets) and agent-native interfaces (OpenAPI and an MCP endpoint generated from the same routes). Install with `pip install oxbrook`; import `oxbrook`; run with `oxbrook run main:app` (or `oxb`).

Oxbrook is its own server, not an ASGI framework: it does not run under uvicorn, and ASGI middleware does not apply. It looks like FastAPI in places and differs in ways that break code written from FastAPI habits. Before writing an Oxbrook app:

- **Every handler takes the request as its first argument**, before path, query and body parameters: `async def get_user(request: Request, user_id: int)`. Name it `_` when unused; it is still passed.
- **Handlers are `async def`.** A plain `def` handler is refused at registration unless the route says `blocking=True`, which runs it on a bounded threadpool. Never call blocking I/O from an `async def` handler.
- **Async clients and pools come from `worker_lifespan`, never a module-level variable.** The server runs several asyncio event loops, and a pool made on one loop cannot be used from another. `worker_lifespan` runs once per loop; what it yields is read through `request.state`.
- **Return a dict, a list, a pydantic model or a `Reply`.** `Reply(body, status=201, headers=...)` sets the status and headers. Raise `HTTPError(status, detail)` for an error response.
- **A pydantic model parameter is the JSON body.** Scalars are path or query parameters.
- **Configuration is `oxbrook.Settings`**, a pydantic model read from environment variables.
- **Test with `oxbrook.testing.TestClient(app)`**, which runs the real server: `with TestClient(app) as client: client.get("/")`.
- **Agents reach a route only if it says `tool=True`.** The MCP endpoint is `/mcp`.

Every page below is available as Markdown at the linked URL. The whole documentation, API reference included, is one file: [llms-full.txt]({base}llms-full.txt).
