"""Middleware: code that runs around every handler.

    @app.middleware
    async def timing(request, call_next):
        started = time.perf_counter()
        reply = await call_next(request)
        reply.headers["x-elapsed-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return reply

Middleware runs in the order it was registered, outermost first, and unwinds in
reverse. It can observe a request, add response headers, change the status, or
refuse to call the handler at all.

**What `call_next` returns is a `Reply`, not a finished response.** It holds
whatever the handler returned, still unserialized. That is deliberate: a
handler returning a dict is encoded to JSON in Rust, and materializing a body
just so middleware could look at it would throw that away on every request.
Middleware that genuinely needs the bytes can set `reply.value` to a
`Response`.

Routes with no middleware registered are untouched and pay nothing.
"""

from typing import Any

from ._response import Response, header_pairs


class Reply:
    """A handler's result on its way back out.

    `value` is whatever the handler returned: a dict, a model, a `Response`, an
    `SSE`, or None. `status` overrides what that value would otherwise imply.
    `headers` are added to the response.
    """

    __slots__ = ("headers", "status", "value")

    def __init__(
        self,
        value: Any = None,
        status: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.value = value
        self.status = status
        self.headers = headers if headers is not None else {}

    def __repr__(self) -> str:
        return f"<Reply status={self.status} value={type(self.value).__name__}>"


def as_reply(result: Any) -> Reply:
    return result if isinstance(result, Reply) else Reply(result)


def _link(middleware: Any, nxt: Any):
    async def call(request):
        return as_reply(await middleware(request, nxt))

    return call


def wrap(handler: Any, middlewares: list[Any], around: Any = None) -> Any:
    """Return a handler that runs `middlewares` around `handler`.

    `around`, when given, wraps every link of the chain. Exception mapping uses
    it, so an exception raised by one middleware reaches the next one out as a
    reply rather than as an exception.
    """

    async def dispatch(request, **params):
        async def endpoint(req):
            return as_reply(await handler(req, **params))

        call = endpoint
        # Reversed so the first-registered middleware ends up outermost.
        for middleware in reversed(middlewares):
            call = _link(middleware, call)
            if around is not None:
                call = around(call)
        return await call(request)

    dispatch.__name__ = getattr(handler, "__name__", "handler")
    dispatch.__qualname__ = getattr(handler, "__qualname__", "handler")
    return dispatch


#: What an authorizer returns to mean "accept". Never reaches a client: the
#: server builds the real handshake response itself.
ACCEPT_STATUS = 101


def make_gate(authorize: Any) -> Any:
    """Turn an authorizer into something the normal reply path can carry.

    Accepting is signalled as a 101 because the upgrade decision has to travel
    back through the same channel an ordinary response uses.
    """
    from ._response import Response

    async def gate(request, **params):
        verdict = authorize(request)
        if hasattr(verdict, "__await__"):
            verdict = await verdict
        if verdict is None or verdict is True:
            return Response(b"", status=ACCEPT_STATUS, content_type="text/plain")
        if isinstance(verdict, Reply):
            return verdict
        if isinstance(verdict, Response):
            return verdict
        # A falsy verdict with no detail still has to mean "no".
        from ._errors import PROBLEM, problem

        return Response(problem(403), status=403, content_type=PROBLEM)

    gate.__name__ = getattr(authorize, "__name__", "authorize")
    return gate


def merge(reply: Reply) -> tuple[Any, int | None, list[tuple[str, str]] | None]:
    """Fold a Reply into (value, status override, extra headers).

    A `Response` inside a Reply keeps its own status and headers; the Reply's
    are added on top, so middleware can annotate a response it did not build.
    """
    value = reply.value
    headers = dict(reply.headers)
    status = reply.status

    if isinstance(value, Response):
        combined = {**value.headers, **headers}
        value = Response(
            value.body,
            status=status if status is not None else value.status,
            content_type=value.content_type,
            headers=combined,
        )
        return value, None, None

    return value, status, header_pairs(headers)
