"""Oxbrook: a fast Python web framework with a Rust core.

Async handlers, a radix-tree router with typed path and query parameters
coerced in Rust, pydantic request and response bodies, bounded concurrency,
and OpenAPI 3.1 generated from the same route metadata.
"""

from importlib.metadata import PackageNotFoundError as _NotInstalled
from importlib.metadata import version as _version

from ._app import App
from ._bodies import BodyStream
from ._capabilities import Capability, CapabilityError
from ._core import Request
from ._cors import CORS
from ._depends import Depends
from ._errors import HTTPError
from ._forms import Form, FormData, UploadFile
from ._logging import JsonFormatter, json_logging
from ._middleware import Reply
from ._redis import Consumer, Message, RedisBackend
from ._response import Response
from ._routers import Router
from ._schema import RequestValidationError
from ._sessions import Session, Sessions
from ._sse import SSE, Event
from ._streams import BLOCK, DROP_NEWEST, DROP_OLDEST, ERROR, Subscription, Topic, TopicFull
from ._websocket import WebSocket, WebSocketClosed

try:
    __version__ = _version("oxbrook")
except _NotInstalled:  # imported from a source tree that was never installed
    __version__ = "0+unknown"

__all__ = [
    "BLOCK",
    "CORS",
    "DROP_NEWEST",
    "DROP_OLDEST",
    "ERROR",
    "SSE",
    "App",
    "BodyStream",
    "Capability",
    "CapabilityError",
    "Consumer",
    "Depends",
    "Event",
    "Form",
    "FormData",
    "HTTPError",
    "JsonFormatter",
    "Message",
    "RedisBackend",
    "Reply",
    "Request",
    "RequestValidationError",
    "Response",
    "Router",
    "Session",
    "Sessions",
    "Subscription",
    "Topic",
    "TopicFull",
    "UploadFile",
    "WebSocket",
    "WebSocketClosed",
    "json_logging",
]
