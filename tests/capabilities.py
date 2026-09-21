#!/usr/bin/env python3
"""The capability registry, checked with the official MCP SDK client.

Named capabilities.py, not mcp.py: a test module called mcp shadows the SDK
package it imports.

The claim milestone 5 makes is that one set of declarations serves three
audiences. So this asserts all three against the same running server: a plain
HTTP client, the OpenAPI document, and a real MCP client driving the protocol.
"""
import asyncio
import json
import sys
import threading

import httpx
from oxbrook import App, Request
from oxbrook._mcp import SUPPORTED_VERSIONS
from oxbrook._workers import gil_enabled
from pydantic import BaseModel, Field

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
except ImportError as exc:
    ClientSession = streamable_http_client = None
    SDK_MISSING = str(exc)
else:
    SDK_MISSING = None

#: The one environment where the SDK may be absent: `mcp` 2.x needs `pywin32`
#: on Windows, which publishes no free-threaded wheel, so uv resolves an `mcp`
#: too old to have the client this uses (I-090). Everywhere else a missing or
#: unusable SDK is a failure — checking against a real client is the point of
#: this suite.
SDK_OPTIONAL = sys.platform == "win32" and not gil_enabled()

PORT = 8811
BASE = f"http://127.0.0.1:{PORT}"
MCP_URL = f"{BASE}/mcp"

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def flatten(exc, depth=0):
    """Report the leaf causes. Exception groups nest, and only the leaves say
    what actually went wrong."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            flatten(sub, depth + 1)
    else:
        failures.append(f"MCP session raised {type(exc).__name__}: {exc}")


class UserIn(BaseModel):
    name: str = Field(min_length=1)
    age: int = Field(ge=0)


class UserOut(BaseModel):
    id: int
    name: str
    age: int


app = App(title="People", version="1.0.0", docs_url=None)
DB: dict[int, dict] = {1: {"id": 1, "name": "ada", "age": 36}}


@app.get("/users/{user_id}", tool=True)
async def get_user(_: Request, user_id: int, loud: bool = False) -> UserOut:
    """Look up a user by id.

    Returns the stored record.
    """
    if user_id not in DB:
        raise KeyError(f"no user {user_id}")
    record = dict(DB[user_id])
    if loud:
        record["name"] = record["name"].upper()
    return UserOut(**record)


@app.post("/users", tool=True)
async def create_user(_: Request, body: UserIn) -> UserOut:
    """Create a user."""
    new_id = max(DB) + 1
    DB[new_id] = {"id": new_id, **body.model_dump()}
    return UserOut(**DB[new_id])


@app.delete("/users/{user_id}", tool=True)
async def delete_user(_: Request, user_id: int):
    """Delete a user permanently."""
    DB.pop(user_id, None)
    return {"deleted": user_id}


@app.get("/internal/secret")
async def secret(_: Request):
    """Deliberately not a tool."""
    return {"token": "s3cret"}


@app.get("/health")
async def health(_: Request):
    return {"ok": True}


async def drive_mcp() -> None:
    async with streamable_http_client(MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            check(
                info.server_info.name == "People",
                f"serverInfo.name was {info.server_info.name!r}",
            )
            check(
                info.protocol_version in SUPPORTED_VERSIONS,
                f"the client negotiated {info.protocol_version!r}, which the server "
                f"does not list in SUPPORTED_VERSIONS",
            )
            check(
                info.capabilities.tools is not None,
                "server did not advertise tool support",
            )

            listing = await session.list_tools()
            names = {t.name for t in listing.tools}
            check(
                names == {"get_user", "create_user", "delete_user"},
                f"tools were {sorted(names)}",
            )
            check("secret" not in names, "a route not marked tool=True was exposed")

            by_name = {t.name: t for t in listing.tools}
            get_schema = by_name["get_user"].input_schema
            check(
                set(get_schema["properties"]) == {"user_id", "loud"},
                f"get_user inputSchema properties: {sorted(get_schema['properties'])}",
            )
            check(
                get_schema["required"] == ["user_id"],
                f"get_user required: {get_schema['required']}",
            )
            create_schema = by_name["create_user"].input_schema
            check(
                set(create_schema["properties"]) == {"name", "age"},
                f"body fields were not flattened: {sorted(create_schema['properties'])}",
            )
            check(
                by_name["get_user"].annotations.read_only_hint is True,
                "GET should be marked read-only",
            )
            check(
                by_name["delete_user"].annotations.destructive_hint is True,
                "DELETE should be marked destructive",
            )

            # --- calling ---
            got = await session.call_tool("get_user", {"user_id": 1})
            check(not got.is_error, f"get_user reported an error: {got.content}")
            check(
                got.structured_content == {"id": 1, "name": "ada", "age": 36},
                f"get_user structuredContent was {got.structured_content}",
            )

            loud = await session.call_tool("get_user", {"user_id": 1, "loud": True})
            check(
                loud.structured_content["name"] == "ADA",
                f"query parameter ignored: {loud.structured_content}",
            )

            made = await session.call_tool("create_user", {"name": "grace", "age": 45})
            check(not made.is_error, f"create_user errored: {made.content}")
            check(
                made.structured_content["name"] == "grace",
                f"create_user returned {made.structured_content}",
            )
            new_id = made.structured_content["id"]

            # A handler that raises is a tool error, not a transport failure.
            missing = await session.call_tool("get_user", {"user_id": 999})
            check(missing.is_error, "a raising handler should set isError")

            # Validation still runs: the body model rejects a bad age.
            bad = await session.call_tool("create_user", {"name": "x", "age": -1})
            check(bad.is_error, "body validation should reject age=-1")

            short = await session.call_tool("create_user", {"name": "x"})
            check(short.is_error, "a missing required argument should error")

            unknown = await session.call_tool("create_user", {"name": "x", "age": 1, "nope": 2})
            check(unknown.is_error, "an unexpected argument should error")

            # --- resources ---
            resources = await session.list_resources()
            uris = {str(r.uri) for r in resources.resources}
            check(
                "topic://events" in uris,
                f"topics were not offered as resources: {uris}",
            )
            read_back = await session.read_resource("topic://events")
            payload = json.loads(read_back.contents[0].text)
            check(payload["topic"] == "events", f"resource read gave {payload}")

            # Clean up so the HTTP checks below see a known state.
            await session.call_tool("delete_user", {"user_id": new_id})
            check(new_id not in DB, "delete_user did not take effect")


#: What the installed SDK calls its newest revision, as of the last time
#: someone looked. It runs ahead of what clients actually negotiate — this
#: constant was once taken for the server's own version list, and the official
#: client then refused to connect, which is why the check below compares it
#: against a pin rather than trusting it.
ACKNOWLEDGED_SDK_LATEST = "2026-07-28"


def protocol_drift_check() -> None:
    """Fail when the SDK learns a revision the server has never been tested at.

    `SUPPORTED_VERSIONS` is hand-maintained, and the spec keeps revising. Left
    alone it goes stale silently: a client negotiating a newer revision is
    offered the newest the server knows and may simply refuse. This turns that
    into a failure the next time `mcp` is upgraded, with instructions.
    """
    from mcp.types import LATEST_PROTOCOL_VERSION

    if LATEST_PROTOCOL_VERSION == ACKNOWLEDGED_SDK_LATEST:
        return
    if LATEST_PROTOCOL_VERSION in SUPPORTED_VERSIONS:
        return
    check(
        False,
        f"the MCP SDK now names {LATEST_PROTOCOL_VERSION!r} as its latest, which the "
        f"server has never been tested at. Drive a real client at it: if it works, add "
        f"it to SUPPORTED_VERSIONS in python/oxbrook/_mcp.py; if clients do not yet "
        f"negotiate it, bump ACKNOWLEDGED_SDK_LATEST here",
    )


def wire_format_checks() -> None:
    """The SDK exposes snake_case, but the wire format is camelCase. Assert the
    bytes on the wire, not just what the client parsed."""
    with httpx.Client(base_url=BASE, timeout=10) as c:
        raw = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ).json()
        tool = next(t for t in raw["result"]["tools"] if t["name"] == "get_user")
        for field in ("inputSchema", "annotations", "outputSchema"):
            check(field in tool, f"tools/list omitted {field} on the wire")
        check(
            "readOnlyHint" in tool["annotations"],
            f"annotations used the wrong casing: {sorted(tool['annotations'])}",
        )
        called = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_user", "arguments": {"user_id": 1}},
            },
        ).json()["result"]
        check("structuredContent" in called, "tools/call omitted structuredContent")
        check(called.get("isError") is False, "tools/call used the wrong isError casing")

        notified = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        check(
            notified.status_code == 202 and not notified.content,
            f"a notification returned {notified.status_code} with a body",
        )
        unknown = c.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "nope/nope"}
        ).json()
        check(
            unknown.get("error", {}).get("code") == -32601,
            f"unknown method gave {unknown}",
        )


def http_and_openapi_checks() -> None:
    """The same declarations must still serve curl and OpenAPI."""
    with httpx.Client(base_url=BASE, timeout=10) as c:
        r = c.get("/users/1")
        check(r.status_code == 200, f"plain GET returned {r.status_code}")
        check(
            r.json() == {"id": 1, "name": "ada", "age": 36},
            f"plain GET body was {r.json()}",
        )
        check(
            c.get("/users/abc").status_code == 422,
            "path coercion should still reject a non-integer",
        )
        check(
            c.post("/users", content='{"name":"","age":1}').status_code == 422,
            "body validation should still run over HTTP",
        )
        # The unexposed route is reachable by HTTP, just not by agents.
        check(c.get("/internal/secret").status_code == 200, "non-tool route broke")

        doc = c.get("/openapi.json").json()
        for path in ("/users/{user_id}", "/users", "/internal/secret"):
            check(path in doc["paths"], f"{path} missing from the OpenAPI document")
        check(
            "/mcp" not in doc["paths"],
            "the MCP endpoint should not describe itself in OpenAPI",
        )


def main() -> None:
    app.topic("events")
    threading.Thread(target=lambda: app.run(port=PORT), daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/health", timeout=0.3)
            break
        except Exception:
            threading.Event().wait(0.1)

    if SDK_MISSING and SDK_OPTIONAL:
        print(f"mcp client (official SDK): SKIP ({SDK_MISSING})")
    elif SDK_MISSING:
        failures.append(f"the official MCP SDK client is unusable: {SDK_MISSING}")
        print("mcp client (official SDK): ERROR")
    else:
        try:
            asyncio.run(drive_mcp())
            print("mcp client (official SDK): ok")
        except BaseException as exc:
            flatten(exc)
            print("mcp client (official SDK): ERROR")

    protocol_drift_check()
    wire_format_checks()
    http_and_openapi_checks()
    label = "ok" if not failures else "see below"
    print(f"http + openapi:            {label}")

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
