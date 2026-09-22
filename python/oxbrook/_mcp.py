"""The Model Context Protocol endpoint.

One HTTP endpoint an agent talks to, over MCP's Streamable HTTP transport. It
lists the capabilities a service exposes, invokes them, and offers topics as
readable resources.

Nothing here is declared twice. Tools are derived from routes, which already
carry typed parameters, a body model, a response model and a docstring because
the router and OpenAPI need them. The same service answers curl, an OpenAPI
client and an agent from one set of declarations.

**The transport.** MCP's Streamable HTTP: one endpoint answering POST, GET and
DELETE. A POST carries one JSON-RPC message and is answered with JSON. A GET
opens an SSE stream the server sends on, which is what makes an agent able to
*follow* something rather than poll it. A DELETE ends the session.

**Why sessions exist here.** A stateless server can list and call tools, which
is all the endpoint did before. It cannot tell a client that something
happened, because it has nowhere to send it. The session is what a
server-initiated message is addressed to.

A session's channel is a `Topic` — the same in-memory fan-out that serves SSE
and WebSocket subscribers. That is not reuse for its own sake. The GET stream
lives on whichever worker loop accepted it, and a POST for the same session may
land on any other; a `Subscription` binds to the loop that created it and is
woken from elsewhere through `call_soon_threadsafe`, which is exactly invariant
11's requirement and already load-bearing for topics.

**No resumability.** The spec lets a server attach an `id` to its SSE events so
a disconnected client can resume with `Last-Event-ID`. Doing that honestly
means a per-stream replay buffer. Until there is one, this server attaches no
event ids at all, so no client asks to resume — an unsupported feature nobody
is invited to use, rather than one that silently loses messages.
"""

import asyncio
import json
import secrets
import threading
import time
from collections import deque
from typing import Any

from ._capabilities import Capability, CapabilityError
from ._logging import logger
from ._middleware import Reply, merge
from ._response import Response
from ._schema import encode, jsonable
from ._streams import Topic

#: Protocol revisions this server has actually been exercised against, newest
#: first. A client asking for one of these gets it back; anything else gets
#: PREFERRED_VERSION, which is what the spec asks of a server that cannot
#: honour the request.
#:
#: These are verified, not aspirational. An earlier version of this list
#: claimed a revision newer than any client would accept, and the official SDK
#: refused to connect: it negotiates 2025-11-25 and rejected the newer number
#: offered back. Add a version here only after a real client has used it.
PREFERRED_VERSION = "2025-11-25"
SUPPORTED_VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26"}

#: The 2026-07-28 wire, which is a different protocol on the same endpoint.
#:
#: There is no handshake and no session: a request is self-contained, carrying
#: its protocol version and the client's identity in `_meta`. A client finds
#: out a server speaks it by asking — `server/discover` — and falls back to the
#: handshake on anything that is not a positive answer.
#:
#: Server-to-client messages do not ride a standing GET stream here. A client
#: sends `subscriptions/listen` and the response to *that request* is the
#: stream. Nothing is resumable: a dropped stream is re-listened, not replayed.
MODERN_VERSIONS = ("2026-07-28",)

DISCOVER = "server/discover"
LISTEN = "subscriptions/listen"
ACKNOWLEDGED = "notifications/subscriptions/acknowledged"
RESOURCE_UPDATED = "notifications/resources/updated"

#: Every 2026-07-28 result carries caching metadata, and a client that parses
#: strictly refuses one without it. `ttlMs: 0` says "do not cache this": a tool
#: list changes when the process does, and a call's result is about the moment
#: it ran. A server that wanted to be cached would have to mean it, per result.
RESULT_STAMP = {"ttlMs": 0, "cacheScope": "private", "resultType": "complete"}

META = "_meta"
PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
SUBSCRIPTION_META = "io.modelcontextprotocol/subscriptionId"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: 2026-07-28: "I do not speak any version you offered", and the answer names
#: the ones this server does, so the client can retry at a mutual one.
UNSUPPORTED_VERSION = -32022

TOPIC_SCHEME = "topic://"

SESSION_HEADER = "mcp-session-id"
VERSION_HEADER = "mcp-protocol-version"

#: How long a session survives with nothing touching it. A client that goes
#: away without a DELETE would otherwise hold its channel for ever. A session
#: with an open stream is never idle, however quiet it is: listening without
#: speaking is the whole point of subscribing.
SESSION_IDLE = 300.0

#: Messages a followed topic keeps for the agent between reads. Bounded, and
#: it drops the oldest: an agent that stops reading must not be able to grow a
#: server's memory by staying subscribed.
FEED_BUFFER = 256

#: A cap, for the reason every other queue here has one. An `initialize` costs
#: a session, and nothing authenticates it, so without a limit this is a memory
#: leak with a network interface. Refused rather than evicting a live session:
#: throwing out a working client to make room for an arriving one trades a
#: known good connection for an unknown one.
MAX_SESSIONS = 512


class Feed:
    """One session following one topic.

    The buffer is what the agent reads. An MCP `resources/updated` notification
    carries no payload — it says a resource changed and the client re-reads it
    — so something has to hold what changed in between. For a durable topic
    that could be the stream, but an in-memory topic keeps no history at all,
    and "go and read it" would hand the agent nothing.
    """

    __slots__ = ("buffer", "notified", "subscription", "task", "uri")

    def __init__(self, uri: str, subscription: Any, size: int) -> None:
        self.uri = uri
        self.subscription = subscription
        #: Held because a task nobody references can be collected mid-flight.
        self.task: Any = None
        self.buffer: deque = deque(maxlen=size)
        #: Whether the client has been told there is something to read and has
        #: not read since.
        self.notified = False

    def take(self) -> list:
        """Everything since the last read. Clears the notification latch."""
        items = list(self.buffer)
        self.buffer.clear()
        self.notified = False
        return items

    def close(self) -> None:
        # Thread-safe from any loop: it wakes its waiter through
        # call_soon_threadsafe. Closing it is what ends the pump task, so
        # there is never a task to cancel across a loop boundary.
        self.subscription.close()


class Session:
    """One client's session: a protocol version and a way to reach the client."""

    __slots__ = ("_lock", "channel", "feeds", "id", "touched", "version")

    def __init__(self, sid: str, version: str) -> None:
        self.id = sid
        self.version = version
        self.touched = time.monotonic()
        self.channel: Topic | None = None
        self.feeds: dict[str, Feed] = {}
        self._lock = threading.Lock()

    def outbound(self) -> Topic:
        """The channel a GET streams from, made on first use.

        Most sessions never open one — a client that only calls tools has
        nothing to listen for — and an unused Topic is a subscriber list nobody
        will ever read.
        """
        with self._lock:
            if self.channel is None:
                self.channel = Topic(f"mcp:{self.id}")
            return self.channel

    @property
    def listening(self) -> bool:
        channel = self.channel
        return channel is not None and channel.subscribers > 0

    def post(self, message: dict) -> int:
        """Send one message to the client. Callable from any worker loop.

        `emit_nowait`, not `emit`: the caller is a handler on some other loop,
        and a server notification must never hold up the request that produced
        it. A client whose buffer has filled loses notifications, not its
        session.
        """
        channel = self.channel
        return 0 if channel is None else channel.emit_nowait(message)

    def close(self) -> None:
        """Explicit, never left to garbage collection — invariant 7. Closing
        the channel closes its subscriptions, which is what ends the GET, and
        closing each feed is what ends its pump."""
        with self._lock:
            channel, self.channel = self.channel, None
            feeds, self.feeds = list(self.feeds.values()), {}
        for feed in feeds:
            feed.close()
        if channel is not None:
            channel.close()


class Sessions:
    """Every live session in the process.

    Deliberately not loop-bound. A POST for a session can land on any worker
    loop, so the registry has to be reachable from all of them; what must not
    cross a loop is the *channel*, and a Topic is built for exactly that.
    """

    __slots__ = ("_by_id", "_lock", "idle", "limit")

    def __init__(self, idle: float = SESSION_IDLE, limit: int = MAX_SESSIONS) -> None:
        self._by_id: dict[str, Session] = {}
        self._lock = threading.Lock()
        self.idle = idle
        self.limit = limit

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_id)

    def create(self, version: str) -> "Session | None":
        """A new session, or None when the process already holds `limit`."""
        self.sweep()
        # token_urlsafe is visible ASCII throughout, which the spec requires of
        # a session id, and cryptographically secure, which it also requires.
        session = Session(secrets.token_urlsafe(32), version)
        with self._lock:
            if len(self._by_id) >= self.limit:
                return None
            self._by_id[session.id] = session
        return session

    def get(self, sid: str | None) -> "Session | None":
        """The live session with that id, touched. None if it never existed or
        has expired, which the caller answers with 404 either way."""
        if not sid:
            return None
        with self._lock:
            session = self._by_id.get(sid)
            if session is None:
                return None
            if self._expired(session):
                del self._by_id[sid]
            else:
                session.touched = time.monotonic()
                return session
        # Outside the lock: close() takes the session's own.
        session.close()
        return None

    def drop(self, sid: str | None) -> bool:
        if not sid:
            return False
        with self._lock:
            session = self._by_id.pop(sid, None)
        if session is None:
            return False
        session.close()
        return True

    def sweep(self) -> int:
        """Drop expired sessions. Called on create, which is often enough: a
        process that has stopped taking clients has stopped growing too."""
        with self._lock:
            dead = [s for s in self._by_id.values() if self._expired(s)]
            for session in dead:
                del self._by_id[session.id]
        for session in dead:
            session.close()
        return len(dead)

    def close(self) -> None:
        with self._lock:
            sessions, self._by_id = list(self._by_id.values()), {}
        for session in sessions:
            session.close()

    def _expired(self, session: Session) -> bool:
        if session.listening:
            return False
        return time.monotonic() - session.touched > self.idle


def _ok(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


class MCP:
    """Dispatch for one app's MCP endpoint."""

    __slots__ = ("app", "capabilities", "sessions")

    def __init__(
        self,
        app: Any,
        capabilities: dict[str, Capability],
        sessions: "Sessions | None" = None,
    ) -> None:
        self.app = app
        self.capabilities = capabilities
        self.sessions = sessions if sessions is not None else Sessions()

    # ---- descriptions -----------------------------------------------------

    def tool_list(self) -> list[dict]:
        tools = []
        for capability in self.capabilities.values():
            described = capability.describe()
            model = capability.route.response_model
            if model is not None:
                # Declaring an output schema is a promise: a client may reject
                # a result that does not match it, so only routes that declare
                # a response model get one.
                described["outputSchema"] = model.model_json_schema(
                    ref_template="#/$defs/{model}"
                )
            tools.append(described)
        return tools

    def resource_list(self) -> list[dict]:
        resources = []
        for name, topic in self.app.topics.items():
            resources.append(
                {
                    "uri": f"{TOPIC_SCHEME}{name}",
                    "name": name,
                    "description": (
                        "Durable topic. Reading returns recent messages."
                        if topic.durable
                        else "In-memory topic. Live only; it keeps no history to read."
                    ),
                    "mimeType": "application/json",
                }
            )
        return resources

    def _topic(self, uri: str) -> Any:
        if not uri.startswith(TOPIC_SCHEME):
            raise CapabilityError(f"unknown resource {uri!r}")
        name = uri[len(TOPIC_SCHEME):]
        topic = self.app.topics.get(name)
        if topic is None:
            raise CapabilityError(f"no topic named {name!r}")
        return topic

    async def subscribe(self, uri: str, session: "Session | None") -> dict:
        """Follow a topic, so the agent is told when it has something new.

        This is the point of the whole transport. The same topic that feeds a
        browser over SSE and a client over a WebSocket now feeds an agent, from
        the one declaration that made it — no polling loop, and no second way
        of saying what the topic is.
        """
        if session is None:
            raise CapabilityError("subscribing needs a session")
        topic = self._topic(uri)
        if uri in session.feeds:
            return {}

        feed = Feed(uri, topic.subscribe(), FEED_BUFFER)
        session.feeds[uri] = feed
        # The pump runs on whichever worker loop took this request; the GET it
        # feeds may be on another. Both hops go through a Subscription, which
        # is what makes that safe.
        feed.task = asyncio.create_task(self._pump(session, feed))
        return {}

    async def unsubscribe(self, uri: str, session: "Session | None") -> dict:
        if session is None:
            raise CapabilityError("unsubscribing needs a session")
        feed = session.feeds.pop(uri, None)
        if feed is not None:
            feed.close()
        return {}

    async def _pump(self, session: "Session", feed: Feed) -> None:
        """Forward a topic's messages to one session, one notification at a time.

        Not one notification per message. The notification says "there is
        something to read"; saying it again before the client has read changes
        nothing and costs a frame on a busy topic. The same coalescing rule as
        invariant 3, for the same reason.
        """
        try:
            async for item in feed.subscription:
                feed.buffer.append(item)
                if not feed.notified:
                    feed.notified = True
                    session.post(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/resources/updated",
                            "params": {"uri": feed.uri},
                        }
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a pump that dies silently is a feed
            # that never delivers again and never says why. Logged, not raised:
            # there is no caller to raise to, only the task's own death.
            logger.exception("mcp feed stopped", extra={"uri": feed.uri})

    async def read_resource(self, uri: str, session: "Session | None" = None) -> list[dict]:
        topic = self._topic(uri)
        name = uri[len(TOPIC_SCHEME):]

        feed = session.feeds.get(uri) if session is not None else None
        if feed is not None:
            # Subscribed: what has arrived since the last read, which is what
            # the notification was about. Reading is what re-arms it.
            body = {"topic": name, "messages": feed.take()}
            return [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": encode(body).decode(),
                }
            ]

        if topic.durable:
            history = await topic.history(count=50)
            body = {"topic": name, "messages": [value for _id, value in history]}
        else:
            body = {
                "topic": name,
                "messages": [],
                "note": (
                    "in-memory topic: live only, no history is retained. "
                    "Subscribe to it to receive what is emitted from now on"
                ),
                "subscribers": topic.subscribers,
            }
        return [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": encode(body).decode(),
            }
        ]

    # ---- invocation -------------------------------------------------------

    async def call_tool(self, params: dict, parent: Any = None) -> dict:
        name = params.get("name")
        capability = self.capabilities.get(name)
        if capability is None:
            # A missing tool is a protocol-level error; a tool that fails while
            # running is not, and comes back as isError below.
            raise CapabilityError(f"no tool named {name!r}")

        try:
            result = await capability.invoke(params.get("arguments") or {}, parent)
        except CapabilityError as exc:
            # About the arguments the agent sent, written for it.
            return _tool_error(str(exc))
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
            # Exception text never goes to a client, and an agent is a client.
            # It used to: any raising tool returned `TypeName: message`, the
            # same leak an HTTP 500 is built to prevent.
            logger.exception("tool raised", exc_info=exc, extra={"tool": name})
            detail = f"{type(exc).__name__}: {exc}" if self.app.debug else "internal error"
            return _tool_error(detail)

        status = None
        if isinstance(result, Reply):
            result, status, _headers = merge(result)

        if isinstance(result, Response):
            body = result.encoded().decode("utf-8", "replace")
            # An HTTPError, a validation failure, or middleware refusing the
            # call all arrive as a response with an error status.
            return {"content": [{"type": "text", "text": body}], "isError": result.status >= 400}

        if status is not None and status >= 400:
            return _tool_error(encode(result).decode())

        payload = jsonable(result)
        content = {
            "content": [{"type": "text", "text": encode(payload).decode()}],
            "isError": False,
        }
        if capability.route.response_model is not None:
            content["structuredContent"] = payload
        return content

    # ---- protocol ---------------------------------------------------------

    async def dispatch(
        self, message: dict, parent: Any = None, session: "Session | None" = None
    ) -> dict | None:
        """Handle one JSON-RPC message. None means it was a notification.

        `session` is None only for `initialize`, which is the message that
        creates one.
        """
        if message.get("jsonrpc") != "2.0" or "method" not in message:
            return _err(message.get("id"), INVALID_REQUEST, "not a JSON-RPC 2.0 request")

        method = message["method"]
        request_id = message.get("id")
        params = message.get("params") or {}
        notification = request_id is None

        if method.startswith("notifications/"):
            return None

        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                result = {
                    "protocolVersion": asked if asked in SUPPORTED_VERSIONS else PREFERRED_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        # Subscribing is real: a topic followed here pushes
                        # notifications/resources/updated down the GET stream.
                        "resources": {"subscribe": True, "listChanged": False},
                    },
                    "serverInfo": {"name": self.app.title, "version": self.app.version},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tool_list()}
            elif method == "tools/call":
                result = await self.call_tool(params, parent)
            elif method == "resources/list":
                result = {"resources": self.resource_list()}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "resources/read":
                result = {"contents": await self.read_resource(params.get("uri", ""), session)}
            elif method == "resources/subscribe":
                result = await self.subscribe(params.get("uri", ""), session)
            elif method == "resources/unsubscribe":
                result = await self.unsubscribe(params.get("uri", ""), session)
            else:
                return None if notification else _err(
                    request_id, METHOD_NOT_FOUND, f"unknown method {method!r}"
                )
        except CapabilityError as exc:
            return None if notification else _err(request_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 - must answer, not hang the client
            return None if notification else _err(request_id, INTERNAL_ERROR, str(exc))

        return None if notification else _ok(request_id, result)

    # ---- the 2026-07-28 wire ----------------------------------------------

    def _era(self, message: dict) -> str:
        """Which protocol this message belongs to: "modern", "handshake", or
        "mismatch" when it names a version nothing here speaks.

        `server/discover` is always modern: it is the question "are you a
        modern server?", and only a modern server answers it.
        """
        if message.get("method") == DISCOVER:
            return "modern"
        asked = ((message.get("params") or {}).get(META) or {}).get(PROTOCOL_META)
        if asked is None:
            return "handshake"
        if asked in MODERN_VERSIONS:
            return "modern"
        if asked in SUPPORTED_VERSIONS:
            return "handshake"
        return "mismatch"

    def _stamp(self, result: Any) -> Any:
        if isinstance(result, dict):
            for key, value in RESULT_STAMP.items():
                result.setdefault(key, value)
        return result

    def discover(self) -> dict:
        """What a modern client probes with, and the whole of the negotiation.

        Answering it at all is what tells a client to stop and use this wire
        rather than falling back to `initialize`.
        """
        return {
            "supportedVersions": list(MODERN_VERSIONS),
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": True, "listChanged": False},
            },
            "instructions": self.app.description or None,
        }

    def _honored(self, asked: dict) -> dict:
        """The subset of a listen filter this server will actually deliver.

        Echoed back in the acknowledgement, because a client is entitled to
        know what it is not going to be told about. Only resource
        subscriptions are honoured: nothing here changes its tool or prompt
        list while running, so promising those notifications would be
        promising silence.
        """
        uris = [u for u in (asked.get("resourceSubscriptions") or []) if isinstance(u, str)]
        return {"resourceSubscriptions": uris}

    async def listen(self, message: dict) -> Any:
        """`subscriptions/listen`: the response to this request *is* the stream.

        No session, no GET, nothing to clean up afterwards — the stream's life
        is the request's life, which is what an Oxbrook handler already is.
        """
        from ._sse import SSE

        request_id = message.get("id")
        if request_id is None:
            raise CapabilityError("subscriptions/listen needs a request id")

        honored = self._honored((message.get("params") or {}).get("notifications") or {})
        # Resolved before the stream opens, so an unknown topic is an error
        # answered with JSON rather than a stream that turns out to be empty.
        topics = [(uri, self._topic(uri)) for uri in honored["resourceSubscriptions"]]
        return SSE(self._listen_stream(request_id, honored, topics))

    async def _listen_stream(self, request_id: Any, honored: dict, topics: list) -> Any:
        from ._sse import Event

        meta = {SUBSCRIPTION_META: request_id}
        # Subscribed before the acknowledgement goes out, so a message emitted
        # while that frame is in flight is buffered rather than missed.
        subscriptions = [(uri, topic.subscribe()) for uri, topic in topics]
        queue: asyncio.Queue = asyncio.Queue(maxsize=FEED_BUFFER)
        pumps = [
            asyncio.create_task(self._drain_into(queue, uri, subscription))
            for uri, subscription in subscriptions
        ]
        try:
            yield Event(
                data={
                    "jsonrpc": "2.0",
                    "method": ACKNOWLEDGED,
                    "params": {"notifications": honored, META: meta},
                },
                event="message",
            )
            while True:
                uri = await queue.get()
                yield Event(
                    data={
                        "jsonrpc": "2.0",
                        "method": RESOURCE_UPDATED,
                        "params": {"uri": uri, META: meta},
                    },
                    event="message",
                )
        finally:
            for _uri, subscription in subscriptions:
                subscription.close()
            for pump in pumps:
                pump.cancel()

    async def _drain_into(self, queue: "asyncio.Queue", uri: str, subscription: Any) -> None:
        """One topic's emits, as "there is something to read at `uri`".

        The message itself is dropped, because the wire has nowhere to put it:
        an updated notification carries a uri and no payload. On this wire
        there is no session either, so no per-client buffer for the client to
        come back and read — see I-095.
        """
        async for _item in subscription:
            try:
                queue.put_nowait(uri)
            except asyncio.QueueFull:
                # The client is not reading. Dropping a duplicate "something
                # changed" costs nothing: the next one says the same thing.
                pass

    # ---- transport --------------------------------------------------------

    def origin_allowed(self, request: Any) -> bool:
        """Guard against DNS rebinding, which the spec requires of every
        Streamable HTTP server.

        A page on `http://evil.test` can make a browser resolve that name to
        127.0.0.1 and then talk to a local MCP server as if it were its own.
        The request looks ordinary; the `Origin` header is what gives it away.

        No Origin means no browser sent it, so there is nothing to rebind. An
        Origin that matches the Host the client asked for is same-origin. Any
        other Origin is allowed only if CORS already says that origin may call
        this app, because refusing there would be refusing something the
        application has explicitly permitted.
        """
        origin = request.header("origin") if request is not None else None
        if origin is None:
            return True

        cors = getattr(self.app, "cors", None)
        if cors is not None and (
            "*" in cors.allow_origins or origin in cors.allow_origins
        ):
            return True

        host = request.header("host")
        return host is not None and origin.partition("://")[2] == host

    def _refuse(self, code: int, message: str, status: int) -> Response:
        # A JSON-RPC error with no id, which is what the spec asks for when the
        # failure is about the HTTP request rather than any one message.
        return Response(
            json.dumps(_err(None, code, message)).encode(),
            status=status,
            content_type="application/json",
        )

    async def _modern(self, message: dict, request: Any) -> Any:
        """One self-contained request on the 2026-07-28 wire.

        No session to look up and none to create. A notification is
        acknowledged and dropped — this wire defines no client-to-server
        notifications, so there is nothing one could be asking for.
        """
        method = message.get("method")
        request_id = message.get("id")

        if request_id is None:
            return Response(b"", status=202, content_type="application/json")

        try:
            if method == DISCOVER:
                result: Any = self.discover()
            elif method == LISTEN:
                # The only answer that is a stream rather than a value.
                return await self.listen(message)
            else:
                reply = await self.dispatch(message, request, None)
                if reply is not None and "result" in reply:
                    self._stamp(reply["result"])
                return Response(
                    encode(reply),
                    content_type="application/json",
                )
        except CapabilityError as exc:
            return self._refuse(INVALID_PARAMS, str(exc), 400)

        return Response(
            encode(_ok(request_id, self._stamp(result))),
            content_type="application/json",
        )

    async def post(self, request: Any) -> Response:
        """One JSON-RPC message in, one answer out."""
        if not self.origin_allowed(request):
            return self._refuse(INVALID_REQUEST, "origin not allowed", 403)

        body = request.body if request is not None else b""
        try:
            message = json.loads(body)
        except ValueError:
            return self._refuse(PARSE_ERROR, "invalid JSON", 400)

        if isinstance(message, list):
            # Batching was removed from MCP in 2025-06-18. Say so rather than
            # half-supporting it.
            return self._refuse(INVALID_REQUEST, "batched requests are not supported", 400)
        if not isinstance(message, dict):
            return self._refuse(INVALID_REQUEST, "expected a JSON object", 400)

        era = self._era(message)
        if era == "mismatch":
            asked = ((message.get("params") or {}).get(META) or {}).get(PROTOCOL_META)
            # Named, so the client can retry at a version both ends speak
            # rather than guessing which way to step.
            return Response(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {
                            "code": UNSUPPORTED_VERSION,
                            "message": f"unsupported protocol version {asked!r}",
                            "data": {
                                "supported": [*MODERN_VERSIONS, *sorted(SUPPORTED_VERSIONS)]
                            },
                        },
                    }
                ).encode(),
                status=400,
                content_type="application/json",
            )

        if era == "modern":
            return await self._modern(message, request)

        sid = request.header(SESSION_HEADER)
        initializing = message.get("method") == "initialize"
        session = None

        if not initializing:
            if not sid:
                return self._refuse(INVALID_REQUEST, "missing MCP-Session-Id", 400)
            session = self.sessions.get(sid)
            if session is None:
                # 404 is load-bearing: it is how a client is told to start a
                # new session rather than retrying into a session that is gone.
                return self._refuse(INVALID_REQUEST, "unknown or expired session", 404)

        reply = await self.dispatch(message, request, session)

        headers = {}
        if initializing and reply is not None and "result" in reply:
            fresh = self.sessions.create(reply["result"]["protocolVersion"])
            if fresh is None:
                logger.warning("mcp session refused", extra={"live": len(self.sessions)})
                return self._refuse(INTERNAL_ERROR, "too many sessions", 503)
            headers[SESSION_HEADER] = fresh.id

        if reply is None:
            # A notification or a response gets acknowledgement and no body.
            return Response(b"", status=202, content_type="application/json", headers=headers)
        return Response(
            encode(reply),
            content_type="application/json",
            headers=headers,
        )

    async def get(self, request: Any) -> Any:
        """Open the server-to-client stream for a session.

        Everything the server says on its own initiative goes here. Without it
        an agent can only ask; with it, it can be told.
        """
        from ._sse import SSE

        if not self.origin_allowed(request):
            return self._refuse(INVALID_REQUEST, "origin not allowed", 403)

        sid = request.header(SESSION_HEADER)
        if not sid:
            return self._refuse(INVALID_REQUEST, "missing MCP-Session-Id", 400)
        session = self.sessions.get(sid)
        if session is None:
            return self._refuse(INVALID_REQUEST, "unknown or expired session", 404)

        return SSE(self._channel(session))

    async def _channel(self, session: Session) -> Any:
        """The session's outbound messages, as SSE events.

        The channel carries JSON-RPC messages, which is what they are; the
        `message` event name is put on at this boundary, because that is a fact
        about the wire format and not about the message.
        """
        from ._sse import Event

        subscription = session.outbound().subscribe()
        try:
            async for message in subscription:
                yield Event(data=message, event="message")
        finally:
            # The client leaving must not leave a subscriber attached to the
            # channel for the rest of the session's life.
            subscription.close()

    async def delete(self, request: Any) -> Response:
        """End a session at the client's request, closing its stream."""
        if not self.origin_allowed(request):
            return self._refuse(INVALID_REQUEST, "origin not allowed", 403)

        sid = request.header(SESSION_HEADER)
        if not sid:
            return self._refuse(INVALID_REQUEST, "missing MCP-Session-Id", 400)
        if not self.sessions.drop(sid):
            return self._refuse(INVALID_REQUEST, "unknown or expired session", 404)
        return Response(b"", status=204, content_type="application/json")
