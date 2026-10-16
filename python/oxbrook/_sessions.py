"""Signed cookie sessions.

    sessions = Sessions(secret=os.environ["SECRET_KEY"])
    app.middleware(sessions.middleware)

    @app.get("/count")
    async def count(_: Request, session = Depends(sessions.load)):
        session["views"] = session.get("views", 0) + 1
        return {"views": session["views"]}

The session is a dict. The dependency reads and verifies the cookie; the
middleware writes it back afterwards, but only if the handler changed it, so an
unmodified session costs no `Set-Cookie`.

**The data lives in the cookie, signed but not encrypted.** The client cannot
forge it and cannot change it without the signature failing, but can read it.
Put an identifier in a session, not a password, and not anything the person
holding the cookie should not see. Server-side storage is a different design
and is not this.
"""

import base64
import hashlib
import hmac
import json
import time
from contextvars import ContextVar
from typing import Any

#: Set by the dependency, read by the middleware after the handler returns.
#: They run in the same task, so the value set inside the dependency is visible
#: to the middleware on the way back out.
_current: ContextVar[Any] = ContextVar("oxbrook_session")

DEFAULT_MAX_AGE = 14 * 24 * 3600


class Session(dict):
    """A dict that remembers whether anything changed."""

    __slots__ = ("modified",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.modified = False

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key, value)
        self.modified = True

    def __delitem__(self, key: Any) -> None:
        super().__delitem__(key)
        self.modified = True

    def clear(self) -> None:
        """Remove everything; the cookie is rewritten empty."""
        super().clear()
        self.modified = True

    def pop(self, *args: Any) -> Any:
        """Remove a key and return its value, as `dict.pop` does."""
        self.modified = True
        return super().pop(*args)

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Set several keys at once, as `dict.update` does."""
        super().update(*args, **kwargs)
        self.modified = True


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Sessions:
    """Reads and writes a signed session cookie."""

    __slots__ = ("cookie", "max_age", "path", "same_site", "secret", "secure")

    def __init__(
        self,
        secret: str | bytes,
        *,
        cookie: str = "oxbrook_session",
        max_age: int = DEFAULT_MAX_AGE,
        secure: bool = True,
        same_site: str = "Lax",
        path: str = "/",
    ) -> None:
        if not secret:
            raise ValueError("sessions need a secret")
        self.secret = secret.encode() if isinstance(secret, str) else secret
        self.cookie = cookie
        self.max_age = max_age
        self.secure = secure
        self.same_site = same_site
        self.path = path

    # ---- signing ----------------------------------------------------------

    def _sign(self, payload: str) -> str:
        digest = hmac.new(self.secret, payload.encode(), hashlib.sha256).digest()
        return _b64encode(digest)

    def encode(self, data: dict) -> str:
        """The signed cookie value for `data`, stamped with the time it was issued."""
        payload = _b64encode(
            json.dumps({"d": data, "t": int(time.time())}, separators=(",", ":")).encode()
        )
        return f"{payload}.{self._sign(payload)}"

    def decode(self, raw: str) -> dict:
        """Return the data, or an empty dict if the cookie is not trustworthy."""
        payload, _, signature = raw.partition(".")
        if not payload or not signature:
            return {}
        # Constant time: a fast reject on the first wrong byte would leak the
        # signature one byte at a time.
        if not hmac.compare_digest(signature, self._sign(payload)):
            return {}
        try:
            body = json.loads(_b64decode(payload))
        except (ValueError, TypeError):
            return {}
        issued = body.get("t", 0)
        if self.max_age and (time.time() - issued) > self.max_age:
            return {}
        data = body.get("d")
        return data if isinstance(data, dict) else {}

    # ---- wiring -----------------------------------------------------------

    def load(self, request: Any) -> Session:
        """Dependency: the session for this request."""
        raw = request.cookies.get(self.cookie)
        session = Session(self.decode(raw) if raw else {})
        _current.set(session)
        return session

    def cookie_header(self, session: Session) -> str:
        """A complete `Set-Cookie` value for the session, with this object's
        path, lifetime and flags."""
        parts = [
            f"{self.cookie}={self.encode(dict(session))}",
            f"Path={self.path}",
            f"Max-Age={self.max_age}",
            "HttpOnly",
            f"SameSite={self.same_site}",
        ]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)

    async def middleware(self, request: Any, call_next: Any) -> Any:
        """Writes the cookie back when the handler changed the session, and only
        then. Register it with `app.middleware(sessions.middleware)`."""
        token = _current.set(None)
        try:
            reply = await call_next(request)
        finally:
            session = _current.get(None)
            _current.reset(token)

        # Only when the handler actually touched it: rewriting an unchanged
        # session on every response is wasted bytes and a needless refresh.
        if isinstance(session, Session) and session.modified:
            reply.headers["set-cookie"] = self.cookie_header(session)
        return reply
