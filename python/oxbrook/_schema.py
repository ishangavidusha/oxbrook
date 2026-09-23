"""Optional pydantic integration.

Pydantic is the declaration language for request and response bodies. Its core
is Rust, `model_validate_json` parses bytes without building an intermediate
Python dict, and `model_json_schema()` gives OpenAPI and MCP tool schemas for
free later.

The trade-off, taken deliberately: unlike path parameters, body validation runs
on the worker thread rather than the tokio thread, so a bad body does wake a
Python worker before it is rejected.

Oxbrook still imports and runs without pydantic. Only body models need it.
"""

from typing import Any

try:
    from pydantic import BaseModel, ValidationError

    HAVE_PYDANTIC = True
    _MODEL_TYPES: tuple[type, ...] = (BaseModel,)
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    BaseModel = None  # type: ignore[assignment]
    ValidationError = None  # type: ignore[assignment]
    HAVE_PYDANTIC = False
    _MODEL_TYPES = ()


class RequestValidationError(Exception):
    """A request body failed validation. Carries a ready-to-send JSON body."""

    __slots__ = ("body",)

    def __init__(self, body: bytes) -> None:
        super().__init__("request body failed validation")
        self.body = body

    @property
    def errors(self) -> list:
        """The individual failures, for an exception handler that reshapes them."""
        import json

        return json.loads(self.body)["errors"]


def is_model(annotation: Any) -> bool:
    return HAVE_PYDANTIC and isinstance(annotation, type) and issubclass(annotation, BaseModel)


def is_model_instance(value: Any) -> bool:
    # Empty tuple makes this a cheap constant False when pydantic is absent.
    return isinstance(value, _MODEL_TYPES)


def to_json(instance: Any) -> bytes:
    """Serialize a model straight to bytes, skipping the str round trip."""
    return instance.__pydantic_serializer__.to_json(instance)


# One encoding for every value, on every surface: a handler's return, a Reply,
# an HTTPError's detail, an SSE event, a WebSocket message, a topic, an MCP
# tool result. Each used to choose for itself, so a datetime was a 500 returned
# plainly, `"2026-09-23 10:00:00"` inside a Reply, and ISO 8601 inside a model.
# Pydantic's rules are the ones taken because models already follow them: a
# value encodes the same whether or not it sits in a model.
#
# NaN and infinity become null. Pydantic's default writes the bare constants,
# which no JSON parser accepts, and the Rust encoder already answers null.


def _mapping_or_refuse(value: Any) -> Any:
    # A database row is usually mapping-like without being a registered
    # Mapping: asyncpg's Record has keys() and lookup by name. Anything else is
    # refused rather than str()'d, because a repr in a response is a bug that
    # reaches a client looking like data.
    keys = getattr(value, "keys", None)
    if callable(keys):
        return {key: value[key] for key in keys()}
    raise TypeError(
        f"{type(value).__qualname__} cannot be encoded as JSON; return a dict, "
        f"a list, a pydantic model, or a value converted to one of those"
    )


if HAVE_PYDANTIC:
    from pydantic_core import to_json as _pydantic_to_json
    from pydantic_core import to_jsonable_python as _pydantic_jsonable

    def encode(value: Any) -> bytes:
        """JSON bytes for any value Oxbrook sends."""
        return _pydantic_to_json(value, inf_nan_mode="null", fallback=_mapping_or_refuse)

    def jsonable(value: Any) -> Any:
        """The same value as plain dicts, lists and scalars, encoded the same way."""
        return _pydantic_jsonable(value, inf_nan_mode="null", fallback=_mapping_or_refuse)

else:  # pragma: no cover - depends on the environment
    import json as _json

    def encode(value: Any) -> bytes:
        return _json.dumps(
            value, separators=(",", ":"), default=_mapping_or_refuse
        ).encode()

    def jsonable(value: Any) -> Any:
        return _json.loads(encode(value))


def validation_body(exc: Any) -> bytes:
    """Problem details for a 422, with pydantic's errors as the `errors` member.

    Written by hand rather than through the encoder because `exc.json()` is
    already JSON, straight from pydantic's core. The prefix is the one Rust
    writes for a parameter that will not coerce, so a client reads one shape
    for every 422 whichever side refused it.
    """
    return _VALIDATION_PREFIX + exc.json().encode() + b"}"


_VALIDATION_PREFIX = (
    b'{"type":"about:blank","title":"Unprocessable Content","status":422,"errors":'
)
