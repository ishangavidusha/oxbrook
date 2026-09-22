"""Turning exceptions into responses.

    class NotFound(Exception):
        pass

    @app.exception_handler(NotFound)
    async def not_found(request, exc):
        return Reply({"error": str(exc)}, status=404)

An exception handler runs when a handler, a dependency, body validation or an
authorizer raises an exception of that class or a subclass; the most specific
registered class wins. What it returns goes through the same response path as a
handler's return value, so a `Reply` or `Response` sets the status.

`HTTPError` needs no registration: raising one answers with its status and a
`{"detail": ...}` body. Register a handler for `HTTPError` to change that shape,
or for `RequestValidationError` to change the shape of a `422`.

Handlers are mapped *inside* middleware, so middleware — the access log
included — sees the status the client will get rather than an exception. An
exception raised by middleware itself is mapped too, on its way out.

A class with no handler is still a `500` with the traceback in the log. A
handler registered for `Exception` replaces that, and takes on the job of
logging.
"""

import http
from typing import Any

from ._middleware import Reply
from ._response import Response
from ._schema import RequestValidationError, encode


class HTTPError(Exception):
    """Raise to answer with an error status.

        raise HTTPError(404, "no such user")
        raise HTTPError(401, headers={"www-authenticate": "Bearer"})

    `detail` is sent to the client, so it is for messages written for the
    client. It defaults to the status's standard phrase. This is the one
    exception whose text is returned: an unhandled exception's never is.
    """

    def __init__(
        self,
        status: int,
        detail: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not isinstance(status, int) or not 400 <= status <= 599:
            raise ValueError(
                f"HTTPError needs a 4xx or 5xx status, got {status!r}; "
                f"return a Reply or Response for anything else"
            )
        if detail is None:
            try:
                detail = http.HTTPStatus(status).phrase
            except ValueError:
                detail = "error"
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = dict(headers or {})

    def __repr__(self) -> str:
        return f"HTTPError({self.status}, {self.detail!r})"


def http_error_body(exc: HTTPError) -> bytes:
    return encode({"detail": exc.detail})


async def _default_http_error(_request: Any, exc: HTTPError) -> Response:
    return Response(
        http_error_body(exc),
        status=exc.status,
        content_type="application/json",
        headers=exc.headers,
    )


async def _default_validation_error(_request: Any, exc: RequestValidationError) -> Response:
    return Response(exc.body, status=422, content_type="application/json")


#: What happens with no registration. A user handler for the same class
#: replaces the default.
DEFAULT_HANDLERS: dict[type, Any] = {
    HTTPError: _default_http_error,
    RequestValidationError: _default_validation_error,
}


def find(handlers: dict[type, Any], exc: BaseException) -> Any:
    """The handler for the most specific class in the exception's MRO."""
    for cls in type(exc).__mro__:
        handler = handlers.get(cls)
        if handler is not None:
            return handler
    return None


def guard(target: Any, handlers: dict[type, Any]) -> Any:
    """Wrap a handler so exceptions with a registered handler become replies.

    Only `Exception` is caught. Cancellation is a `BaseException` and must keep
    propagating, or a disconnected client's task would be answered instead of
    stopped.
    """

    async def guarded(request, **params):
        try:
            return await target(request, **params)
        except Exception as exc:
            handler = find(handlers, exc)
            if handler is None:
                raise
            return await handler(request, exc)

    guarded.__name__ = getattr(target, "__name__", "handler")
    guarded.__qualname__ = getattr(target, "__qualname__", "handler")
    return guarded


__all__ = ["DEFAULT_HANDLERS", "HTTPError", "Reply", "find", "guard", "http_error_body"]
