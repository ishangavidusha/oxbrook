"""Turning routes into agent-callable capabilities.

This is the point the whole design has been building towards. A handler already
declares typed path parameters, typed query parameters, a request body model, a
response model and a docstring, because the router and OpenAPI both need them.
An MCP tool needs exactly the same material, so it is derived rather than
declared again, and the two cannot drift apart.

Marking a route with `tool=True` is deliberate. Every route being callable by an
agent by default would mean an administrative delete endpoint is callable by an
agent by default.
"""

import json
import math
from typing import Any

from ._core import Request
from ._response import Response
from ._routing import RouteInfo

_SCALAR_SCHEMA: dict[str, dict[str, str]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "uuid": {"type": "string", "format": "uuid"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
}

#: HTTP methods that only read. Used to fill in MCP's tool annotations, which
#: is how a client decides what it may call without asking permission.
_READ_ONLY = {"GET", "HEAD"}
_DESTRUCTIVE = {"DELETE"}
#: A second identical call has the same effect as one.
_IDEMPOTENT = {"GET", "HEAD", "PUT", "DELETE"}


class CapabilityError(Exception):
    """A capability could not be built or invoked."""


def _param_schema(param) -> dict[str, Any]:
    schema: dict[str, Any] = dict(_SCALAR_SCHEMA[param.kind])
    if param.repeated:
        schema = {"type": "array", "items": schema}
    if param.optional:
        schema = {"anyOf": [schema, {"type": "null"}]}
    if param.presence == "omit":
        schema["default"] = param.default
    return schema


class Capability:
    """One route, exposed as an MCP tool."""

    __slots__ = ("_body_fields", "description", "input_schema", "name", "route", "target")

    def __init__(self, route: RouteInfo, target: Any = None) -> None:
        self.route = route
        #: What runs when the tool is called: the handler inside its routers'
        #: middleware and the app's exception handlers. Without it a tool call
        #: skipped a router's auth middleware that the same route over HTTP ran.
        self.target = target
        self.name = getattr(route.fn, "__name__", "handler")
        self.description = self._describe(route)
        self._body_fields: set[str] = set()
        self.input_schema = self._build_schema(route)

    @staticmethod
    def _describe(route: RouteInfo) -> str:
        parts = [p for p in (route.summary, route.description) if p]
        if parts:
            return "\n\n".join(parts)
        # Better than an empty description, which leaves an agent guessing.
        return f"{route.method} {route.path}"

    def _build_schema(self, route: RouteInfo) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        defs: dict[str, Any] = {}

        for param in route.params:
            properties[param.name] = _param_schema(param)
            if param.source == "path" or param.presence == "required":
                required.append(param.name)

        if route.body is not None:
            _, model = route.body
            body_schema = model.model_json_schema(ref_template="#/$defs/{model}")
            defs.update(body_schema.pop("$defs", {}))
            # Body fields are flattened to the top level rather than nested
            # under "body". An agent filling one flat argument list is far more
            # reliable than one nesting an object it has to infer the shape of.
            for field, schema in (body_schema.get("properties") or {}).items():
                if field in properties:
                    raise CapabilityError(
                        f"{route.method} {route.path}: body field {field!r} collides "
                        f"with a path or query parameter of the same name. Rename one, "
                        f"or do not expose this route as a tool"
                    )
                properties[field] = schema
                self._body_fields.add(field)
            required.extend(body_schema.get("required") or [])

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
        }
        if defs:
            schema["$defs"] = defs
        return schema

    def annotations(self) -> dict[str, Any]:
        """MCP hints about what calling this does.

        Inferred from the HTTP method, which already carries the intent: a GET
        reads, a DELETE destroys, a POST is neither safe nor repeatable.
        """
        method = self.route.method.upper()
        return {
            "title": self.route.summary or self.name,
            "readOnlyHint": method in _READ_ONLY,
            "destructiveHint": method in _DESTRUCTIVE,
            "idempotentHint": method in _IDEMPOTENT,
            "openWorldHint": False,
        }

    def describe(self) -> dict[str, Any]:
        """The tool definition an MCP client receives."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations(),
        }

    def _split(self, arguments: dict[str, Any]) -> tuple[dict, dict, dict]:
        """Sort flat arguments back into path, query and body."""
        by_name = {p.name: p for p in self.route.params}
        path_and_query: dict[str, Any] = {}
        body: dict[str, Any] = {}
        unknown: dict[str, Any] = {}

        for key, value in arguments.items():
            if key in by_name:
                path_and_query[key] = value
            elif key in self._body_fields:
                body[key] = value
            else:
                unknown[key] = value
        return path_and_query, body, unknown

    async def invoke(self, arguments: dict[str, Any], parent: Any = None) -> Any:
        """Call the handler directly, without going back out over HTTP.

        The synthesized request is what lets the same handler serve both, and
        means the body still passes through the route's own pydantic
        validation rather than a second copy of it.

        `parent` is the request that carried the call. Its headers and worker
        context are copied onto the synthesized one, so a router's middleware
        checks the credentials the agent actually sent, and the handler sees
        the same `request.state` it would over HTTP.
        """
        params, body, unknown = self._split(arguments or {})
        if unknown:
            raise CapabilityError(
                f"{self.name}: unexpected argument(s) "
                f"{', '.join(sorted(repr(k) for k in unknown))}"
            )

        missing = [
            name
            for name in self.input_schema["required"]
            if name not in params and name not in body
        ]
        if missing:
            raise CapabilityError(
                f"{self.name}: missing required argument(s) "
                f"{', '.join(repr(m) for m in missing)}"
            )

        path = self.route.path
        for param in self.route.params:
            if param.source == "path" and param.name in params:
                path = path.replace(f"{{{param.name}}}", str(params[param.name]))
                path = path.replace(f"{{*{param.name}}}", str(params[param.name]))

        request = Request(
            self.route.method,
            path,
            None,
            json.dumps(body).encode() if body else b"",
            None if parent is None else list(parent.headers.items()),
            None if parent is None else parent._context,
            None if parent is None else parent.locals,
            None if parent is None else parent.client,
        )
        # The route's limit, which over HTTP the server checks before a worker
        # sees the request. A tool call arrives inside `/mcp`, past that
        # check, so it is made here: an agent must not be able to call a
        # limited route without limit by calling it as a tool.
        if self.route.rate_limit is not None:
            wait = self.route.rate_limit._limiter.take(request)
            if wait is not None:
                return _limited(wait)
        target = self.target if self.target is not None else self.route.target
        result = await target(request, **params)
        # Work the tool set aside runs after the `/mcp` response, as it would
        # after its own over HTTP: handed to the request that carried the call.
        # Not reached when the tool raised, so a failed call's work is dropped.
        if parent is not None:
            for fn, args, kwargs in request._take_after():
                parent.after_response(fn, *args, **kwargs)
        return result


def _limited(wait: float) -> Response:
    """The tool-call twin of the server's `429`, rounded up the same way."""
    from ._errors import PROBLEM, problem

    return Response(problem(429, "rate limit exceeded"), status=429, content_type=PROBLEM,
                    headers={"retry-after": str(max(1, math.ceil(wait)))})


def build(routes: list[RouteInfo], compose: Any = None) -> dict[str, Capability]:
    """Capabilities for every route that asked to be one."""
    capabilities: dict[str, Capability] = {}
    for route in routes:
        if not route.tool or route.websocket:
            continue
        capability = Capability(route, None if compose is None else compose(route))
        if capability.name in capabilities:
            raise CapabilityError(
                f"two routes export a tool named {capability.name!r}; "
                f"tool names come from the handler name, so rename one"
            )
        capabilities[capability.name] = capability
    return capabilities
