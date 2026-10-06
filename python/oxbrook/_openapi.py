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

_NOT_IDENT = re.compile(r"[^A-Za-z0-9]+")

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


def _problem(description: str, components: dict[str, Any]) -> dict[str, Any]:
    components.setdefault("Problem", _PROBLEM_SCHEMA)
    return {
        "description": description,
        "content": {
            "application/problem+json": {"schema": {"$ref": _REF_TEMPLATE.format(model="Problem")}}
        },
    }


def _secure(
    op: dict[str, Any],
    route: RouteInfo,
    default_auth: Any,
    schemes: dict[str, Any],
    components: dict[str, Any],
) -> None:
    """An operation's `security`, its 401 and 403, and the schemes it names."""
    from . import _auth

    if route.auth is None:
        # Public on purpose, in an app that is not: say so, or a reader of
        # the document would take the route to need the app's credentials.
        if default_auth is not None:
            op["security"] = []
        return
    listed, used = _auth.security(route.auth)
    for name, (scheme, entry) in used.items():
        known = schemes.get(name)
        if known is not None and known[0] is not scheme and known[1] != entry:
            raise ValueError(
                f"two different schemes are both named {name!r} in the OpenAPI "
                f"document; give one a distinct name= "
            )
        schemes.setdefault(name, (scheme, entry))
    if listed:
        op["security"] = listed
    op["responses"]["401"] = _problem("Unauthenticated", components)
    if any(scopes for alternative in listed for scopes in alternative.values()) or any(
        requirements for _, options in _auth.plan(route.auth)[0] for requirements in options
    ):
        op["responses"]["403"] = _problem("Forbidden", components)


def _operation(route: RouteInfo, components: dict[str, Any], operation_id: str) -> dict[str, Any]:
    op: dict[str, Any] = {
        "operationId": operation_id,
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


def _operation_ids(routes: list[RouteInfo]) -> dict[int, str]:
    """An `operationId` per route, by `id(route)`, unique across the document.

    A handler's name is its id wherever no other handler shares it. Two
    `list_items` on two routers are a normal layout, and the specification
    requires the id to be unique, so every route in a group that shares a name
    gets the method and path added: both of them, not only the second, so that
    which id a route has does not depend on the order things were included in.
    """
    named = [(route, getattr(route.fn, "__name__", "handler")) for route in routes]
    counts: dict[str, int] = {}
    for _, name in named:
        counts[name] = counts.get(name, 0) + 1
    ids: dict[int, str] = {}
    taken = {name for name, count in counts.items() if count == 1}
    for route, name in named:
        if counts[name] == 1:
            ids[id(route)] = name
            continue
        slug = _NOT_IDENT.sub("_", route.path).strip("_") or "root"
        candidate = base = f"{name}_{route.method.lower()}_{slug}"
        # `/a-b` and `/a_b` slug alike; the suffix is a last resort.
        n = 2
        while candidate in taken:
            candidate, n = f"{base}_{n}", n + 1
        taken.add(candidate)
        ids[id(route)] = candidate
    return ids


def build(
    routes: list[RouteInfo],
    title: str,
    version: str,
    description: str = "",
    default_auth: Any = None,
) -> dict[str, Any]:
    components: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    schemes: dict[str, Any] = {}
    # OpenAPI 3.1 has no vocabulary for WebSocket endpoints, so they are left
    # out rather than described as ordinary GETs.
    routes = [route for route in routes if not route.websocket]
    ids = _operation_ids(routes)

    for route in routes:
        entry = paths.setdefault(_openapi_path(route.path), {})
        operation = _operation(route, components, ids[id(route)])
        _secure(operation, route, default_auth, schemes, components)
        entry[route.method.lower()] = operation

    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": paths,
    }
    if description:
        document["info"]["description"] = description
    if components or schemes:
        document["components"] = {}
    if components:
        document["components"]["schemas"] = components
    if schemes:
        document["components"]["securitySchemes"] = {
            name: entry for name, (_, entry) in schemes.items()
        }
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
