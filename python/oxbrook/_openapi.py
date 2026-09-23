"""OpenAPI 3.1 generation.

Built from the same `RouteInfo` objects the router uses, so the document cannot
drift from what the server actually accepts. Pydantic supplies body and response
schemas through `model_json_schema()`; scalar path and query parameters are
described from their annotations.

This is the first half of the M5 story. The registry that emits MCP tools will
read the same route metadata.
"""

import re
from typing import Any

from ._routing import RouteInfo

_SCALAR_SCHEMA: dict[Any, dict[str, str]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "uuid": {"type": "string", "format": "uuid"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
}

_REF_TEMPLATE = "#/components/schemas/{model}"

# `/files/{*rest}` in Oxbrook is `/files/{rest}` in OpenAPI, which has no
# wildcard syntax of its own.
_WILDCARD = re.compile(r"\{\*([A-Za-z_][A-Za-z0-9_]*)\}")

# RFC 9457 problem details: what every error response is.
_PROBLEM_SCHEMA = {
    "type": "object",
    "title": "Problem",
    "properties": {
        "type": {"type": "string", "format": "uri-reference", "default": "about:blank"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "instance": {"type": "string", "format": "uri-reference"},
    },
    "required": ["type", "title", "status"],
}

_VALIDATION_PROBLEM_SCHEMA = {
    "title": "ValidationProblem",
    "allOf": [
        {"$ref": "#/components/schemas/Problem"},
        {
            "type": "object",
            "properties": {
                "errors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "loc": {"type": "array", "items": {"type": "string"}},
                            "msg": {"type": "string"},
                        },
                    },
                }
            },
            "required": ["errors"],
        },
    ],
}


def _openapi_path(path: str) -> str:
    return _WILDCARD.sub(r"{\1}", path)


def _register_model(model: Any, components: dict[str, Any]) -> dict[str, str]:
    """Add a model and everything it references to components, return a $ref."""
    schema = model.model_json_schema(ref_template=_REF_TEMPLATE)
    for name, sub in schema.pop("$defs", {}).items():
        components.setdefault(name, sub)
    name = schema.get("title") or model.__name__
    components[name] = schema
    return {"$ref": _REF_TEMPLATE.format(model=name)}


def _parameter(param, components: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = dict(_SCALAR_SCHEMA[param.kind])
    if param.repeated:
        schema = {"type": "array", "items": schema}
    if param.optional:
        schema = {"anyOf": [schema, {"type": "null"}]}
    if param.presence == "omit":
        schema["default"] = param.default

    return {
        "name": param.name,
        "in": param.source,
        "required": param.source == "path" or param.presence == "required",
        "schema": schema,
    }


def _operation(route: RouteInfo, components: dict[str, Any]) -> dict[str, Any]:
    op: dict[str, Any] = {
        "operationId": getattr(route.fn, "__name__", "handler"),
        "responses": {},
    }
    if route.summary:
        op["summary"] = route.summary
    if route.description:
        op["description"] = route.description

    if route.params:
        op["parameters"] = [_parameter(p, components) for p in route.params]

    if route.body is not None:
        op["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {"schema": _register_model(route.body[1], components)}
            },
        }

    if route.form is not None:
        from ._forms import has_files

        model = route.form[1]
        media = "multipart/form-data" if has_files(model) else "application/x-www-form-urlencoded"
        op["requestBody"] = {
            "required": True,
            "content": {media: {"schema": _register_model(model, components)}},
        }

    if route.stream is not None:
        op["requestBody"] = {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }

    ok: dict[str, Any] = {"description": "Successful Response"}
    if route.response_model is not None:
        ok["content"] = {
            "application/json": {"schema": _register_model(route.response_model, components)}
        }
    op["responses"]["200"] = ok

    # Anything with a parameter or a body can fail validation, and the shape is
    # the same in both cases.
    if route.params or route.body is not None or route.form is not None:
        op["responses"]["422"] = {
            "description": "Validation Error",
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": _REF_TEMPLATE.format(model="ValidationProblem")}
                }
            },
        }
        components.setdefault("Problem", _PROBLEM_SCHEMA)
        components.setdefault("ValidationProblem", _VALIDATION_PROBLEM_SCHEMA)

    return op


def build(
    routes: list[RouteInfo],
    title: str,
    version: str,
    description: str = "",
) -> dict[str, Any]:
    components: dict[str, Any] = {}
    paths: dict[str, Any] = {}

    for route in routes:
        # OpenAPI 3.1 has no vocabulary for WebSocket endpoints, so they are
        # left out rather than described as ordinary GETs.
        if route.websocket:
            continue
        entry = paths.setdefault(_openapi_path(route.path), {})
        entry[route.method.lower()] = _operation(route, components)

    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": paths,
    }
    if description:
        document["info"]["description"] = description
    if components:
        document["components"] = {"schemas": components}
    return document


DOCS_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
<div id="ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
window.onload = () => SwaggerUIBundle({{ url: "{openapi_url}", dom_id: "#ui" }});
</script>
</body>
</html>
"""
