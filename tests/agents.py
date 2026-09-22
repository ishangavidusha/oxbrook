#!/usr/bin/env python3
"""The agent-facing transport: sessions, the server stream, and following a topic.

`capabilities.py` asserts the claim that one declaration serves three
audiences. This asserts the transport that carries the agent's half of it:
MCP's Streamable HTTP, which is what lets a server say something an agent did
not ask for.

Every case here names a rule from D-048. The ones that matter most are the two
that are easy to get wrong and impossible to notice:

* a message posted on one worker loop reaching a stream held by another, which
  is what a multi-loop process makes hard and what invariant 11 governs;
* one notification per quiet period rather than one per message, because the
  notification carries no payload and repeating it before the client has read
  changes nothing.
"""
import json
import sys
import threading
import time

import httpx
from oxbrook import App, Request

PORT = 8814
BASE = f"http://127.0.0.1:{PORT}"
MCP = f"{BASE}/mcp"
VERSION = "2025-11-25"

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


app = App(title="agents", version="1")
orders = app.topic("orders")


@app.get("/health")
async def health(request: Request):
    """Liveness, and something to warm the server with."""
    return {"ok": True}


@app.post("/order/{item}", tool=True)
async def place(request: Request, item: str):
    """Place an order."""
    await orders.emit({"item": item})
    return {"placed": item}


def initialize(client, headers=None):
    """The handshake. Returns the response, so a case can assert on it."""
    return client.post(
        MCP,
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": VERSION,
                "capabilities": {},
                "clientInfo": {"name": "suite", "version": "1"},
            },
        },
        headers=headers or {},
    )


def rpc(client, sid, method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(MCP, json=body, headers={"mcp-session-id": sid})


# ---- sessions --------------------------------------------------------------


def initialize_issues_a_session(client) -> None:
    started = initialize(client)
    check(started.status_code == 200, f"initialize returned {started.status_code}")
    sid = started.headers.get("mcp-session-id")
    check(bool(sid), "initialize issued no MCP-Session-Id")
    # The spec requires visible ASCII, because the value goes in a header.
    check(
        all(0x21 <= ord(ch) <= 0x7E for ch in sid or ""),
        f"session id is not visible ASCII: {sid!r}",
    )
    result = started.json()["result"]
    check(
        result["capabilities"]["resources"]["subscribe"] is True,
        "the server does not advertise resource subscriptions",
    )


def a_message_without_a_session_is_refused(client) -> None:
    """400, not 401 or a silent success: the client is missing bookkeeping, and
    the answer has to say which."""
    cold = client.post(MCP, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    check(cold.status_code == 400, f"a sessionless message gave {cold.status_code}")


def an_unknown_session_is_404(client) -> None:
    """404 is load-bearing. It is how a client is told to start a new session
    rather than retrying for ever into one that no longer exists."""
    gone = rpc(client, "not-a-real-session", "ping")
    check(gone.status_code == 404, f"an unknown session gave {gone.status_code}")


def delete_ends_the_session(client) -> None:
    sid = initialize(client).headers["mcp-session-id"]
    ended = client.delete(MCP, headers={"mcp-session-id": sid})
    check(ended.status_code == 204, f"DELETE gave {ended.status_code}")
    after = rpc(client, sid, "ping")
    check(after.status_code == 404, f"a deleted session answered {after.status_code}")


def a_foreign_origin_is_refused(client) -> None:
    """The DNS-rebinding guard the spec requires.

    A page on another origin can make a browser resolve its name to 127.0.0.1
    and then drive a local agent server. The request looks ordinary; Origin is
    what gives it away.
    """
    evil = initialize(client, {"origin": "http://evil.test"})
    check(evil.status_code == 403, f"a foreign Origin gave {evil.status_code}")
    same = initialize(client, {"origin": BASE})
    check(same.status_code == 200, f"a same Origin gave {same.status_code}")
    absent = initialize(client)
    check(absent.status_code == 200, f"no Origin at all gave {absent.status_code}")


# ---- the server-to-client stream -------------------------------------------


def stream_requires_a_session(client) -> None:
    check(client.get(MCP).status_code == 400, "GET without a session was allowed")
    check(
        client.get(MCP, headers={"mcp-session-id": "nope"}).status_code == 404,
        "GET with an unknown session was not 404",
    )


class Listener:
    """A GET stream, read on a thread of its own."""

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.messages: list[dict] = []
        self.status = None
        self.content_type = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with httpx.Client(timeout=15) as client:
            with client.stream(
                "GET", MCP, headers={"mcp-session-id": self.sid}
            ) as response:
                self.status = response.status_code
                self.content_type = response.headers.get("content-type", "")
                self.ready.set()
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        self.messages.append(json.loads(line[6:]))

    def start(self):
        self.thread.start()
        self.ready.wait(10)
        return self

    def wait_for(self, count: int, seconds: float = 5.0) -> None:
        deadline = time.monotonic() + seconds
        while len(self.messages) < count and time.monotonic() < deadline:
            time.sleep(0.05)


def the_stream_opens(client) -> Listener:
    sid = initialize(client).headers["mcp-session-id"]
    listener = Listener(sid).start()
    check(listener.status == 200, f"GET gave {listener.status}")
    check(
        "text/event-stream" in (listener.content_type or ""),
        f"GET answered {listener.content_type!r}, not an event stream",
    )
    return listener


def emits_reach_the_stream_from_other_loops(client, listener: Listener) -> None:
    """The one that a single-loop test would never catch.

    The stream belongs to whichever worker loop accepted the GET. Every emit
    below comes from a request the dispatcher put on whatever loop it chose, so
    the delivery crosses loops unless it got lucky. Four loops, three emits.
    """
    subscribed = rpc(client, listener.sid, "resources/subscribe", {"uri": "topic://orders"})
    check(subscribed.status_code == 200, f"subscribe gave {subscribed.status_code}")
    time.sleep(0.3)

    for item in ("widget", "gizmo", "sprocket"):
        placed = client.post(f"{BASE}/order/{item}")
        check(placed.status_code == 200, f"placing {item} gave {placed.status_code}")

    listener.wait_for(1)
    time.sleep(0.5)

    updates = [
        m for m in listener.messages
        if m.get("method") == "notifications/resources/updated"
    ]
    check(bool(updates), "nothing reached the stream from the worker loops")
    check(
        all(m["params"]["uri"] == "topic://orders" for m in updates),
        f"an update named the wrong resource: {updates}",
    )
    # Coalescing: three messages, one "there is something to read". Told again
    # before the client has read, the second notification says nothing new.
    check(
        len(updates) == 1,
        f"three emits produced {len(updates)} notifications; they should coalesce into 1",
    )


def reading_returns_what_arrived_and_rearms(client, listener: Listener) -> None:
    """A followed topic buffers, because the notification carries no payload
    and an in-memory topic keeps no history to go and read."""
    read = rpc(client, listener.sid, "resources/read", {"uri": "topic://orders"})
    body = json.loads(read.json()["result"]["contents"][0]["text"])
    check(
        [m["item"] for m in body["messages"]] == ["widget", "gizmo", "sprocket"],
        f"the feed read back {body['messages']}",
    )

    again = rpc(client, listener.sid, "resources/read", {"uri": "topic://orders"})
    empty = json.loads(again.json()["result"]["contents"][0]["text"])
    check(empty["messages"] == [], f"a second read returned {empty['messages']}")

    # Reading re-arms the latch, so the next emit notifies again.
    before = len(listener.messages)
    client.post(f"{BASE}/order/restocked")
    listener.wait_for(before + 1)
    check(
        len(listener.messages) > before,
        "no notification after a read re-armed the subscription",
    )


def unsubscribing_stops_the_feed(client, listener: Listener) -> None:
    stopped = rpc(client, listener.sid, "resources/unsubscribe", {"uri": "topic://orders"})
    check(stopped.status_code == 200, f"unsubscribe gave {stopped.status_code}")
    rpc(client, listener.sid, "resources/read", {"uri": "topic://orders"})
    time.sleep(0.2)

    before = len(listener.messages)
    client.post(f"{BASE}/order/ignored")
    time.sleep(0.6)
    check(
        len(listener.messages) == before,
        "an unsubscribed session was still notified",
    )


def deleting_a_session_closes_its_stream(client) -> None:
    sid = initialize(client).headers["mcp-session-id"]
    listener = Listener(sid).start()
    check(listener.status == 200, "setup: the stream did not open")
    client.delete(MCP, headers={"mcp-session-id": sid})
    listener.thread.join(timeout=5)
    check(
        not listener.thread.is_alive(),
        "DELETE left the server-to-client stream open",
    )


def main() -> None:
    threading.Thread(target=lambda: app.run(port=PORT, workers=4), daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        print("server never came up")
        sys.exit(1)

    with httpx.Client(timeout=15) as client:
        for step in (
            initialize_issues_a_session,
            a_message_without_a_session_is_refused,
            an_unknown_session_is_404,
            delete_ends_the_session,
            a_foreign_origin_is_refused,
            stream_requires_a_session,
        ):
            step(client)
            print(f"  {step.__name__}: ok")

        listener = the_stream_opens(client)
        print("  the_stream_opens: ok")
        for step in (
            emits_reach_the_stream_from_other_loops,
            reading_returns_what_arrived_and_rearms,
            unsubscribing_stops_the_feed,
        ):
            step(client, listener)
            print(f"  {step.__name__}: ok")

        deleting_a_session_closes_its_stream(client)
        print("  deleting_a_session_closes_its_stream: ok")

    if failures:
        print("\nfailures:")
        for failure in failures:
            print(f"  - {failure}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
