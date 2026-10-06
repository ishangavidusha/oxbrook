# OpenAPI

The schema is generated from the same route metadata the router uses, so it
cannot describe an endpoint the server would not accept.

```python
app = App(title="Notes", version="1.0.0", description="A notes service.")
```

Two routes are added for you:

- `GET /openapi.json` — the document
- `GET /docs` — a documentation page rendered from it

Pass `openapi_url=None` or `docs_url=None` to turn either off. Both stay public
in an app declared [`App(auth=...)`](guide/auth.md#declaring-it).

Each route's [`auth=`](guide/auth.md#openapi) is described too: the schemes under
`securitySchemes`, and what each operation accepts under `security`, so a
generated client can authenticate and the docs page's **Authorize** button
works.

## Without a server

```python
document = app.openapi()
```

`app.openapi()` returns the document without starting anything, which makes it
usable for client generation in CI, or for a test that asserts the API did not
change accidentally. From the command line:

```bash
oxbrook openapi main:app -o openapi.json
```

## What ends up in it

- Path and query parameters, with their types, and whether they are required
- Request bodies from pydantic models, with nested models hoisted into
  `components/schemas`
- Form bodies bound with `Form()`, as `multipart/form-data` when the model has
  an `UploadFile` field and `application/x-www-form-urlencoded` otherwise
- Streaming bodies, as `application/octet-stream`
- Routes from every included [router](guide/routers.md), at their full paths
- Response models, taken from the handler's return annotation
- The first line of the handler's docstring as the summary, the rest as the
  description
- The handler's name as the `operationId`. Where two handlers share a name,
  such as `list_items` on two routers, each gets its method and path added
  (`list_items_get_a_items`), because the specification requires the id to be
  unique and a generated client names its methods after it
- The `422` shape, so a client knows what a validation failure looks like

Left out: WebSocket routes, because OpenAPI 3.1 has no vocabulary for them, and
dependency arguments, because a client does not supply those. The `/mcp`
endpoint is registered after the document is built, so the agent transport does
not describe itself as a REST endpoint.

## It is checked, not assumed

The test suite runs the generated document through
[`openapi-spec-validator`](https://pypi.org/project/openapi-spec-validator/), so
"valid OpenAPI 3.1" is a checked result rather than a reading of the
specification. The same distinction applies to the [agent
interface](agents.md), which is verified against the official MCP client.
