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

`HTTPError` needs no registration: raising one answers with its status and an
RFC 9457 problem-details body. Register a handler for `HTTPError` to change
that shape, or for `RequestValidationError` to change the shape of a `422`.

Handlers are mapped *inside* middleware, so middleware — the access log
included — sees the status the client will get rather than an exception. An
exception raised by middleware itself is mapped too, on its way out.

A class with no handler is still a `500` with the traceback in the log. A
handler registered for `Exception` replaces that, and takes on the job of
logging.
"""

import builtins
import http
from typing import Any

from ._middleware import Reply
from ._response import Response
from ._schema import RequestValidationError, encode

#: The media type of every error body Oxbrook writes (RFC 9457).
PROBLEM = "application/problem+json"

#: Members RFC 9457 defines. Extension members may not reuse them.
_STANDARD = frozenset({"type", "title", "status", "detail", "instance"})


def status_title(status: int) -> str:
    """The status's name, as RFC 9110 gives it: the title of an `about:blank` problem."""
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def problem(
    status: int,
    detail: str | None = None,
    *,
    type: str = "about:blank",
    title: str | None = None,
    instance: str | None = None,
    extensions: dict[str, Any] | None = None,
) -> bytes:
    """An RFC 9457 problem-details body.

    With `type` left as `about:blank` the problem means no more than its
    status, and the title is the status's name, as the RFC asks.
    """
    body: dict[str, Any] = {
        "type": type,
        "title": title if title is not None else status_title(status),
        "status": status,
    }
    if detail is not None:
        body["detail"] = detail
    if instance is not None:
        body["instance"] = instance
    if extensions:
        body.update(extensions)
    return encode(body)


class HTTPError(Exception):
    """Raise to answer with an error status.

        raise HTTPError(404, "no such user")
        raise HTTPError(401, headers={"www-authenticate": "Bearer"})
        raise HTTPError(
            409,
            "a note with that title already exists",
            type="https://api.example.com/problems/duplicate",
            extensions={"existing_id": 7},
        )

    The response is RFC 9457 problem details, `application/problem+json`:
    `type`, `title`, `status`, and `detail` and `instance` when given, then
    any `extensions`.

    `detail` is sent to the client, so it is for messages written for the
    client, and it is a string: data for a program to read goes in
    `extensions`. This is the one exception whose text is returned; an
    unhandled exception's never is.

    `type` is a URI naming the kind of problem, for clients that branch on
    it. Left as `about:blank`, the problem means no more than its status, and
    `title` is the status's name.
    """

    def __init__(
        self,
        status: int,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
        *,
        type: str = "about:blank",
        title: str | None = None,
        instance: str | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(status, int) or not 400 <= status <= 599:
            raise ValueError(
                f"HTTPError needs a 4xx or 5xx status, got {status!r}; "
                f"return a Reply or Response for anything else"
            )
        if detail is not None and not isinstance(detail, str):
            raise TypeError(
                f"HTTPError detail is a message for a person and must be a str, "
                f"got {builtins.type(detail).__name__}; put data for a program in "
                f"extensions={{...}}"
            )
        clash = _STANDARD.intersection(extensions or ())
        if clash:
            raise ValueError(
                f"extensions cannot redefine the standard members {sorted(clash)}; "
                f"pass them as HTTPError arguments"
            )
        self.status = status
        self.detail = detail
        self.type = type
        self.title = title if title is not None else status_title(status)
        self.instance = instance
        self.extensions = dict(extensions or {})
        self.headers = dict(headers or {})
        super().__init__(detail if detail is not None else self.title)

    def __repr__(self) -> str:
        return f"HTTPError({self.status}, {self.detail!r})"


def http_error_body(exc: HTTPError) -> bytes:
    return problem(
        exc.status,
        exc.detail,
        type=exc.type,
        title=exc.title,
        instance=exc.instance,
        extensions=exc.extensions,
    )


async def _default_http_error(_request: Any, exc: HTTPError) -> Response:
    return Response(
        http_error_body(exc),
        status=exc.status,
        content_type=PROBLEM,
        headers=exc.headers,
    )


async def _default_validation_error(_request: Any, exc: RequestValidationError) -> Response:
    return Response(exc.body, status=422, content_type=PROBLEM)


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


def _drop_after(request: Any) -> None:
    take = getattr(request, "_take_after", None)
    if take is not None:
        take()


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
            # The handler raised: what it set aside for after the response
            # belonged to a request that failed, mapped to a reply or not.
            _drop_after(request)
            try:
                return await handler(request, exc)
            except HTTPError as answer:
                # An exception handler may answer by raising HTTPError, which
                # is the short way to turn a driver's error into a problem
                # response. Mapped here, once, so the middleware around this
                # layer sees a reply like any other rather than an exception.
                fallback = find(handlers, answer)
                if fallback is None:
                    raise
                return await fallback(request, answer)

    guarded.__name__ = getattr(target, "__name__", "handler")
    guarded.__qualname__ = getattr(target, "__qualname__", "handler")
    return guarded


__all__ = [
    "DEFAULT_HANDLERS", "PROBLEM", "HTTPError", "Reply", "find", "guard", "http_error_body",
    "problem", "status_title",
]
