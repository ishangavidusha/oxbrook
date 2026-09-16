"""A test client.

    from oxbrook.testing import TestClient

    with TestClient(app) as client:
        response = client.get("/users/1")
        assert response.status_code == 200
        assert response.json() == {"id": 1}

It starts the real server on a free port and talks to it over HTTP. That is
deliberate. A client that called handlers directly would skip the Rust half of
the request path entirely: routing, parameter coercion, body limits, header
handling, the 405 and 413 responses. Those are the parts most worth testing,
so the client goes through them.

`client.websocket(path)` and `client.mcp(payload)` cover the other two
transports.

Given `tls_cert` and `tls_key`, the server speaks HTTPS and the client trusts
that certificate file, so a test certificate issued for the host works without
touching the system trust store.
"""

import contextlib
import json
import socket
import ssl
import threading
from typing import Any

import httpx


def free_port() -> int:
    """Ask the OS for an unused port.

    Racy in principle, since it is released before the server binds it. In
    practice nothing else claims it in that window, and the alternative is
    plumbing the bound port back out of Rust.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TestClient:
    """Runs an app for the duration of a `with` block."""

    def __init__(
        self,
        app: Any,
        *,
        host: str = "127.0.0.1",
        port: int | None = None,
        timeout: float = 10.0,
        **server_options: Any,
    ) -> None:
        self.app = app
        self.host = host
        self.port = port or free_port()
        # Loaded once the server has started, so a bad certificate is reported
        # by the server, which says what is wrong with it.
        self._trust: ssl.SSLContext | None = None
        secure = "s" if server_options.get("tls_cert") is not None else ""
        self.base_url = f"http{secure}://{host}:{self.port}"
        self.ws_url = f"ws{secure}://{host}:{self.port}"
        self.timeout = timeout
        self._options = server_options
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._client: httpx.Client | None = None

    # ---- lifecycle --------------------------------------------------------

    def _serve(self) -> None:
        try:
            self._server.serve()
        except BaseException as exc:  # noqa: BLE001 - reported by start(), not lost
            self._failure = exc

    def start(self) -> "TestClient":
        self._server = self.app.build_server(
            self.host, self.port, **self._options
        )
        self._failure = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

        # Waits out a slow lifespan too, which runs before the port opens. A
        # server that fails to start raises its own exception here, rather than
        # the client reporting a refused connection with no reason attached.
        pause = threading.Event()
        for _ in range(600):
            if not self._thread.is_alive():
                failure, self._failure = self._failure, None
                self._server = None
                if failure is not None:
                    raise failure
                raise RuntimeError(f"server on {self.base_url} stopped before it started")
            try:
                with socket.create_connection((self.host, self.port), timeout=0.2):
                    break
            except OSError:
                pause.wait(0.05)
        else:
            raise RuntimeError(f"server did not start on {self.base_url}")

        cert = self._options.get("tls_cert")
        if cert is not None:
            self._trust = ssl.create_default_context(cafile=cert)
        self._client = httpx.Client(
            base_url=self.base_url, timeout=self.timeout, verify=self._trust or True
        )
        return self

    def stop(self) -> None:
        """Drain, then disconnect.

        Order matters: closing the HTTP pool first would yank connections out
        from under requests the server is still finishing, which is exactly
        what graceful shutdown exists to avoid.
        """
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "TestClient":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ---- requests ---------------------------------------------------------

    @property
    def http(self) -> httpx.Client:
        if self._client is None:
            raise RuntimeError("TestClient is not started; use it as a context manager")
        return self._client

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.request(method, path, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.post(path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.put(path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.patch(path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.delete(path, **kwargs)

    def head(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.head(path, **kwargs)

    def options(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.options(path, **kwargs)

    # ---- other transports -------------------------------------------------

    @contextlib.contextmanager
    def stream(self, method: str, path: str, **kwargs: Any):
        """A streaming response, for reading an SSE feed."""
        with self.http.stream(method, path, **kwargs) as response:
            yield response

    def websocket(self, path: str):
        """An open WebSocket, as an async context manager.

            async with client.websocket("/ws") as ws:
                await ws.send("hi")
                assert await ws.recv() == "hi"
        """
        import websockets

        return websockets.connect(f"{self.ws_url}{path}", ssl=self._trust)

    def mcp(self, method: str, params: dict | None = None, request_id: int = 1) -> Any:
        """One JSON-RPC call against the app's MCP endpoint.

        Returns the `result`, or raises with the JSON-RPC error message.
        """
        url = self.app.mcp_url or "/mcp"
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        body = self.http.post(url, json=payload).json()
        if "error" in body:
            raise RuntimeError(f"MCP {method} failed: {body['error']}")
        return body.get("result")

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """Invoke a capability the way an agent would."""
        result = self.mcp("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            text = "".join(part.get("text", "") for part in result.get("content", []))
            raise RuntimeError(f"tool {name} failed: {text}")
        if "structuredContent" in result:
            return result["structuredContent"]
        text = "".join(part.get("text", "") for part in result.get("content", []))
        try:
            return json.loads(text)
        except ValueError:
            return text
