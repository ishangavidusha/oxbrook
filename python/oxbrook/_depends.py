"""Dependency injection.

    async def transaction(request):
        async with request.state.pool.acquire() as db:
            async with db.transaction():   # commits if the handler returned,
                yield db                   # rolls back if it raised

    @app.post("/users")
    async def create_user(_: Request, db = Depends(transaction)):
        return await db.fetchrow("insert ...")

A dependency is any callable. It may take the request or take nothing, be sync
or async, and may itself declare dependencies. A generator dependency is
finished after the handler the way a `with` block is: resumed normally if the
handler returned, and with the handler's exception raised at its `yield` if it
did not. That is what makes a transaction written as above commit or roll back
correctly, and it is the main reason to have this rather than calling a
function at the top of every handler.

Results are cached per request, so a dependency shared by three others runs
once. Teardown runs in reverse order, like a stack of context managers, and an
exception raised during teardown is the request's outcome: a commit that fails
is an error response, never the handler's success.

Sync dependencies are called on the worker loop and must not block: there is no
thread offload, by the same rule that makes handlers `async def` only.
"""

import inspect
from contextlib import AsyncExitStack
from typing import Any


class Depends:
    """Marks a handler argument as supplied by a dependency."""

    __slots__ = ("dependency", "use_cache")

    def __init__(self, dependency: Any, *, use_cache: bool = True) -> None:
        if not callable(dependency):
            raise TypeError(
                f"Depends() needs a callable, got {type(dependency).__name__}"
            )
        self.dependency = dependency
        self.use_cache = use_cache

    def __repr__(self) -> str:
        return f"Depends({getattr(self.dependency, '__name__', self.dependency)})"


def _wants_request(fn: Any) -> bool:
    """True if the dependency takes a positional argument for the request."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in signature.parameters.values():
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            if isinstance(param.default, Depends):
                continue
            return True
        break
    return False


def declared(fn: Any) -> dict[str, Depends]:
    """The `Depends(...)` arguments a callable declares."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return {}
    return {
        name: param.default
        for name, param in signature.parameters.items()
        if isinstance(param.default, Depends)
    }


def _name(fn: Any) -> str:
    return getattr(fn, "__qualname__", None) or repr(fn)


def _yielded_twice(fn: Any) -> RuntimeError:
    return RuntimeError(
        f"dependency {_name(fn)} yielded more than once; a dependency yields its "
        f"value exactly once, and the code after the yield is its teardown"
    )


def _async_exit(fn: Any, gen: Any) -> Any:
    """Finish an async generator dependency the way a `with` block would.

    Closing it instead, which is the obvious thing, raises `GeneratorExit` at
    the `yield` on every request. `async with db.transaction(): yield db` reads
    that as an error and rolls back, so nothing a handler wrote was ever kept,
    and a `try/except/else` never saw the handler's real exception.
    """

    async def finish(kind: Any, exc: BaseException | None, tb: Any) -> bool:
        if exc is None:
            try:
                await gen.__anext__()
            except StopAsyncIteration:
                return False
            await gen.aclose()
            raise _yielded_twice(fn)
        try:
            await gen.athrow(exc)
        except StopAsyncIteration:
            # The generator swallowed the error. A `with` block would treat
            # that as handled, but there is no response to send in its place,
            # so the original error stands.
            return False
        except BaseException as raised:
            if raised is exc:
                return False
            # A different exception replaces it, as it would leaving a `with`
            # block. That is how a dependency translates a driver error into
            # an HTTPError.
            raise
        await gen.aclose()
        raise _yielded_twice(fn)

    return finish


def _sync_exit(fn: Any, gen: Any) -> Any:
    """`_async_exit` for a plain generator."""

    def finish(kind: Any, exc: BaseException | None, tb: Any) -> bool:
        if exc is None:
            try:
                next(gen)
            except StopIteration:
                return False
            gen.close()
            raise _yielded_twice(fn)
        try:
            gen.throw(exc)
        except StopIteration:
            return False
        except BaseException as raised:
            if raised is exc:
                return False
            raise
        gen.close()
        raise _yielded_twice(fn)

    return finish


async def _resolve(
    marker: Depends, request: Any, cache: dict, stack: AsyncExitStack, depth: int = 0
) -> Any:
    if depth > 20:
        raise RuntimeError(
            f"dependency nesting is too deep at {marker!r}; this is almost "
            f"certainly a cycle"
        )

    fn = marker.dependency
    if marker.use_cache and fn in cache:
        return cache[fn]

    kwargs: dict[str, Any] = {}
    for name, sub in declared(fn).items():
        kwargs[name] = await _resolve(sub, request, cache, stack, depth + 1)

    args = (request,) if _wants_request(fn) else ()
    produced = fn(*args, **kwargs)

    if inspect.isasyncgen(produced):
        try:
            value = await produced.__anext__()
        except StopAsyncIteration:
            raise RuntimeError(f"dependency {_name(fn)} returned without yielding") from None
        stack.push_async_exit(_async_exit(fn, produced))
    elif inspect.isgenerator(produced):
        try:
            value = next(produced)
        except StopIteration:
            raise RuntimeError(f"dependency {_name(fn)} returned without yielding") from None
        stack.push(_sync_exit(fn, produced))
    elif inspect.isawaitable(produced):
        value = await produced
    else:
        value = produced

    if marker.use_cache:
        cache[fn] = value
    return value


def bind(handler: Any, dependencies: dict[str, Depends]) -> Any:
    """Wrap a handler so its dependencies are resolved per request."""

    async def wrapped(request, *socket, **params):
        # `socket` is the WebSocket, for a socket handler, which is called
        # with it after the request. Passed through untouched.
        #
        # An exit stack is exactly the semantics wanted: teardown in reverse
        # order, each one seeing whatever exception is propagating by then,
        # every one run even if an earlier one raised, and the last exception
        # raised being the one that leaves.
        async with AsyncExitStack() as stack:
            cache: dict[Any, Any] = {}
            for name, marker in dependencies.items():
                params[name] = await _resolve(marker, request, cache, stack)
            return await handler(request, *socket, **params)

    wrapped.__name__ = getattr(handler, "__name__", "handler")
    wrapped.__qualname__ = getattr(handler, "__qualname__", "handler")
    return wrapped
