"""Code that runs *inside* the Python worker threads.

Rust calls `make_worker_loop` once per worker thread and registers a native
drain callback with `loop.add_reader`. When requests are queued, that callback
runs on this thread and schedules `run_handler` for each one, with path
parameters already coerced to Python objects.
"""

import asyncio
import datetime
import sys
import uuid

from ._errors import HTTPError, http_error_body
from ._logging import logger
from ._middleware import Reply, merge
from ._response import Response
from ._schema import RequestValidationError, is_model_instance, to_json
from ._sse import CLOSED, FULL, SSE, SSE_HEADERS, format_event
from ._websocket import WebSocket


async def run_websocket(handler, request, responder, core, params):
    """Run one WebSocket handler.

    `responder` is never used to send a reply here; the 101 already went out
    from the accept path. It is held only so the worker's in-flight count is
    released when this task ends, exactly as it is for an ordinary request.
    """
    loop = asyncio.get_running_loop()
    socket = WebSocket(core)
    gone = loop.create_future()

    def _peer_left():
        if not gone.done():
            gone.set_result(None)

    core.on_close(loop, _peer_left)

    if params is None:
        task = asyncio.ensure_future(handler(request, socket))
    else:
        task = asyncio.ensure_future(handler(request, socket, **params))

    try:
        # Racing the handler against the close is what lets the common pattern
        # work: a handler blocked on `async for item in topic.subscribe()` has
        # no reason to notice its peer left, and would otherwise hold that
        # subscription and its in-flight slot forever.
        done, _ = await asyncio.wait({task, gone}, return_when=asyncio.FIRST_COMPLETED)
        if task not in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            exc = task.exception()
            if exc is not None:
                logger.exception("websocket handler raised", exc_info=exc)
    finally:
        core.close()
        responder.finish()


#: How long to wait before retrying a chunk the connection had no room for.
#: Only ever slept when the buffer is already full, which is to say when the
#: client is slower than the source and the connection is degraded anyway.
FULL_RETRY = 0.005


async def _wait_for_room(responder, chunk, gone):
    """Retry a chunk the connection had no room for.

    Only awaited when the buffer is already full, so the common path costs
    nothing: the caller tries first and comes here only if that fails.
    """
    result = FULL
    while result == FULL and not gone.done():
        await asyncio.sleep(FULL_RETRY)
        result = responder.send_chunk(chunk)
    return result


async def pump_sse(sse, responder):
    """Stream one SSE response until the source ends or the client leaves.

    This holds its worker's in-flight slot for the life of the connection,
    which is correct: a live stream is a request still being served, and it
    should count against `max_concurrency` like any other.

    The disconnect future matters more than it looks. Without it, a stream
    waiting on a quiet topic would sit in `__anext__` indefinitely and never
    discover its client had gone, leaking the subscription and the slot until
    something happened to be published.
    """
    loop = asyncio.get_running_loop()
    gone = loop.create_future()

    def _client_left():
        if not gone.done():
            gone.set_result(None)

    responder.start_stream(sse.status, "text/event-stream; charset=utf-8", SSE_HEADERS)
    responder.notify_disconnect(_client_left)

    iterator = sse.source.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())

            done, _ = await asyncio.wait(
                {pending, gone}, timeout=sse.ping, return_when=asyncio.FIRST_COMPLETED
            )

            if gone in done:
                break

            if pending in done:
                try:
                    item = pending.result()
                except StopAsyncIteration:
                    pending = None
                    break
                except Exception as exc:  # noqa: BLE001
                    # A source that raises mid-stream ends it, and says so.
                    logger.exception("sse source raised", exc_info=exc)
                    pending = None
                    break
                pending = None
                try:
                    chunk = format_event(item)
                except Exception as exc:  # noqa: BLE001
                    # The status is long gone, so the only honest answer is to
                    # end the stream. Logged here because nothing above will:
                    # this runs after the handler returned, so the wrapper's
                    # own except clause has already been left behind.
                    logger.exception("sse source produced an unsendable event", exc_info=exc)
                    break
                # `send_chunk` never blocks, because it runs on a worker
                # loop shared with every other request. When the buffer is
                # full it says so and returns, and this pump used to ignore
                # that: 252 of 400 events vanished from a stream whose client
                # read slowly, with nothing in the stream or the log to say a
                # gap existed. Waiting instead is what makes the documented
                # backpressure apply — the subscription behind this fills and
                # the topic's own policy decides what to drop, which is the
                # decision that belongs to whoever created the topic.
                result = responder.send_chunk(chunk)
                if result == FULL:
                    result = await _wait_for_room(responder, chunk, gone)
                if result == CLOSED:
                    break
            elif sse.ping is not None:
                # Idle. A comment line keeps proxies from closing the stream,
                # and doubles as a liveness check. A full buffer means the
                # connection is anything but idle, so a dropped ping costs
                # nothing and waiting for room would be pointless.
                if responder.send_chunk(b": ping\n\n") == CLOSED:
                    break
    finally:
        if pending is not None:
            pending.cancel()
        responder.end_stream()
        # Releases the topic subscription, if that is what was being iterated.
        close = getattr(sse.source, "close", None)
        if close is not None:
            close()


#: Constructors the Rust side calls for parameter types it validated but
#: cannot build. Values reaching these have already been canonicalised in Rust,
#: so they cannot fail here.
make_uuid = uuid.UUID
make_date = datetime.date.fromisoformat
make_datetime = datetime.datetime.fromisoformat


def make_worker_loop():
    """Build the asyncio loop for one worker thread.

    On Windows the loop has to be a selector loop. Rust registers the drain
    callback with `loop.add_reader`, and the proactor loop that Python uses by
    default there has no `add_reader` at all. Its `select()` watches sockets
    rather than file descriptors, which is why the wake pair is a loopback
    socket pair, and it watches at most 512 of them per loop — a ceiling on
    what handlers on one worker may hold open, not on the server's own
    connections, which are held by Rust.
    """
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


async def run_handler(handler, request, responder, params, debug):
    """Await one handler and turn whatever it returns into a response.

    `params` is None for routes with no path parameters, which keeps the
    common case free of an extra dict and an unpacking call.

    `debug` is passed per server rather than held as module state, so two apps
    in one process cannot end up sharing one another's setting.
    """
    try:
        await _respond(handler, request, responder, params, debug)
    finally:
        # Frees the worker's concurrency slot now rather than whenever Python
        # happens to collect the responder.
        responder.finish()


async def _respond(handler, request, responder, params, debug):
    try:
        result = await (handler(request) if params is None else handler(request, **params))
    except RequestValidationError as exc:
        responder.send(422, "application/json", exc.body)
        return
    except HTTPError as exc:
        # The default handling, for a route with no middleware and no
        # registered handlers, which is wrapped in nothing. Anything registered
        # was already applied by the time an exception reaches here.
        responder.send(
            exc.status, "application/json", http_error_body(exc), list(exc.headers.items()) or None
        )
        return
    except Exception as exc:  # noqa: BLE001 - a handler crash must still answer
        # The detail goes to the server's log. The client gets a status and
        # nothing else, unless the app was started with debug=True.
        logger.exception(
            "handler raised",
            exc_info=exc,
            extra={"method": request.method, "path": request.path},
        )
        detail = (
            f"{type(exc).__name__}: {exc}".encode() if debug else b"internal server error"
        )
        responder.send(500, "text/plain; charset=utf-8", detail)
        return

    # Everything below is the response, and it can fail on its own: a value
    # pydantic or serde cannot encode, a `Response` whose body is not bytes, a
    # header the http crate refuses. Before this guard those escaped into the
    # asyncio task, the responder was dropped without a reply, and the client
    # got the connection-level fallback while the traceback went to asyncio's
    # default handler rather than the log.
    try:
        status_override = None
        extra_headers = None
        if isinstance(result, Reply):
            result, status_override, extra_headers = merge(result)

        if isinstance(result, SSE):
            await pump_sse(result, responder)
        elif isinstance(result, Response):
            responder.send(
                result.status, result.content_type, result.encoded(), result.header_list()
            )
        elif result is None:
            responder.send(status_override or 204, "text/plain", b"", extra_headers)
        elif is_model_instance(result):
            # pydantic serializes straight to bytes, so this skips both a Python
            # str and our own JSON encoder.
            responder.send(
                status_override or 200, "application/json", to_json(result), extra_headers
            )
        elif isinstance(result, (bytes, bytearray, memoryview)):
            responder.send(
                status_override or 200,
                "application/octet-stream",
                bytes(result),
                extra_headers,
            )
        elif isinstance(result, str):
            responder.send(
                status_override or 200,
                "text/plain; charset=utf-8",
                result.encode(),
                extra_headers,
            )
        elif status_override is not None or extra_headers is not None:
            # send_json cannot carry a status or headers, so encode here instead.
            import json as _json

            responder.send(
                status_override or 200,
                "application/json",
                _json.dumps(result, default=str).encode(),
                extra_headers,
            )
        else:
            responder.send_json(200, result)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "response could not be sent",
            exc_info=exc,
            extra={"method": request.method, "path": request.path},
        )
        detail = (
            f"{type(exc).__name__}: {exc}".encode() if debug else b"internal server error"
        )
        try:
            responder.send(500, "text/plain; charset=utf-8", detail)
        except Exception:  # noqa: BLE001
            # Already answered, or a stream that had started. Nothing to add.
            pass
