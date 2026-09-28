"""An OpenID provider's signing keys: fetched once per process, kept fresh.

`OIDC` is the public half. This is the part that talks to the provider, and
it is shaped by who asks: every worker loop verifies tokens, so the keys are
plain data shared by all of them rather than a client per loop, and a fetch
runs on a small thread pool of its own that any loop can wait on. One fetch
is in flight at a time however many requests want one, and how often a fetch
may start is bounded, because a token's key id is chosen by whoever sent it.
"""

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("oxbrook.auth")

#: A token naming a key id not in the set fetches the set again, at most this
#: often. Rotation needs one fetch; a stream of made-up key ids gets one a
#: minute rather than one each.
REFETCH_INTERVAL = 60.0

#: After a failed fetch, the next is not started for this long, so a provider
#: that is down is not asked again by every request that arrives meanwhile.
RETRY_INTERVAL = 5.0

#: A discovery document or key set is a few kilobytes. Anything this large is
#: not one, and is not read into memory to find out.
MAX_BYTES = 1 << 20

#: The key type, and curve where it matters, each algorithm may be used with.
#: A key is used only with its own kind, whatever the token's header claims.
COMPATIBLE: dict[str, tuple[str, frozenset[str] | None]] = {
    **{a: ("RSA", None) for a in ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512")},
    "ES256": ("EC", frozenset({"P-256"})),
    "ES384": ("EC", frozenset({"P-384"})),
    "ES512": ("EC", frozenset({"P-521"})),
    "ES256K": ("EC", frozenset({"secp256k1"})),
    "EdDSA": ("OKP", frozenset({"Ed25519", "Ed448"})),
}

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class Unavailable(Exception):
    """No keys to check a token with: the provider could not be reached."""


class Refused(Exception):
    """This token cannot be checked with these keys. The message is for the log."""


def check_url(url: str, allow_http: bool, what: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname:
        if allow_http or parsed.hostname in _LOOPBACK:
            return
        raise ValueError(
            f"{what} {url!r} is plain HTTP: keys fetched that way can be replaced in "
            f"transit. Use HTTPS, or allow_http=True for a provider on a private network"
        )
    raise ValueError(f"{what} must be an http(s) URL, got {url!r}")


class _NoDowngrade(urllib.request.HTTPRedirectHandler):
    """Redirects are followed, but never from HTTPS to plain HTTP."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if (urllib.parse.urlsplit(req.full_url).scheme == "https"
                and urllib.parse.urlsplit(newurl).scheme != "https"):
            raise urllib.error.HTTPError(
                newurl, code, "redirect from https to plain http refused", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_NoDowngrade)


def _get_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url, headers={"accept": "application/json", "user-agent": "oxbrook"}
    )
    with _opener.open(request, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"{url}: more than {MAX_BYTES} bytes")
    return json.loads(body)


def call_json(
    url: str,
    *,
    form: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> tuple[int, Any]:
    """GET, or POST a form, and read a JSON answer: `(status, document)`.

    For the login flow's token exchange and profile lookups. Blocking, so it
    runs on a thread. An error status is returned rather than raised, because
    a token endpoint says why it refused in the body of its `400`; the
    document is None when the body is not JSON.
    """
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(
        url, data=data,
        headers={"accept": "application/json", "user-agent": "oxbrook", **(headers or {})},
    )
    try:
        response = _opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        status = response.status if response.status is not None else response.code
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"{url}: more than {MAX_BYTES} bytes")
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


_pool_lock = threading.Lock()
_pool_instance: concurrent.futures.ThreadPoolExecutor | None = None


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _pool_instance
    with _pool_lock:
        if _pool_instance is None:
            # Its own threads, not a loop's default executor or the blocking
            # pool: a fetch is shared by every loop, and must not queue behind
            # an app's blocking handlers.
            _pool_instance = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="oxbrook-oidc"
            )
        return _pool_instance


def _parse(document: Any, jwt: Any) -> tuple[dict[str | None, tuple], frozenset[str]]:
    """Signing keys by key id, and a fingerprint to tell when they change."""
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise ValueError("not a JWK set: no 'keys' list")
    keys: dict[str | None, tuple] = {}
    seen: set[str] = set()
    for jwk in document["keys"]:
        if not isinstance(jwk, dict):
            continue
        # Encryption keys share the set with signing keys; `oct` is a shared
        # secret, and a published one would let anyone sign.
        if jwk.get("use", "sig") != "sig" or jwk.get("kty") not in ("RSA", "EC", "OKP"):
            continue
        operations = jwk.get("key_ops")
        if isinstance(operations, list) and "verify" not in operations:
            continue
        alg, kid = jwk.get("alg"), jwk.get("kid")
        if (alg is not None and alg not in COMPATIBLE) or not (kid is None or isinstance(kid, str)):
            continue
        if kid in keys:
            logger.warning("oidc key set has two signing keys with id %r; using the first", kid)
            continue
        try:
            key = jwt.PyJWK(jwk).key
        except Exception as exc:  # noqa: BLE001 - one unusable key is skipped, not fatal
            logger.info("oidc key %r skipped: %s", kid, exc)
            continue
        keys[kid] = (jwk["kty"], jwk.get("crv"), alg, key)
        seen.add(json.dumps(jwk, sort_keys=True))
    return keys, frozenset(seen)


class KeySet:
    """One provider's keys, as every worker loop sees them."""

    def __init__(
        self,
        discovery_url: str,
        issuer: str,
        *,
        algorithms: frozenset[str] | None,
        allow_http: bool,
        timeout: float,
        max_age: float,
        changed: Any,
    ) -> None:
        import jwt

        self._jwt = jwt
        self.discovery_url = discovery_url
        self.issuer = issuer
        self.allow_http = allow_http
        self.timeout = timeout
        self.max_age = max_age
        self._changed = changed
        self._explicit = algorithms
        self._lock = threading.Lock()
        self._fetching: concurrent.futures.Future | None = None
        #: kid -> (kty, crv, alg, key). None until the first fetch succeeds.
        self.keys: dict[str | None, tuple] | None = None
        self.algorithms: frozenset[str] = algorithms or frozenset()
        self.jwks_uri: str | None = None
        #: The discovery document, once fetched: the login flow's endpoints.
        self.document: dict[str, Any] | None = None
        self._fingerprint: frozenset[str] = frozenset()
        self._fetched = 0.0
        self._attempted = -float("inf")
        #: Fetches started, for tests and for anyone wondering how often.
        self.fetches = 0

    # ---- fetching: on the pool's threads ---------------------------------------

    def _start(self, spacing: float) -> concurrent.futures.Future | None:
        """The fetch in flight, or a new one if none started in `spacing` seconds."""
        with self._lock:
            if self._fetching is None:
                now = time.monotonic()
                if now - self._attempted < spacing:
                    return None
                self._attempted = now
                self.fetches += 1
                self._fetching = _pool().submit(self._fetch)
            return self._fetching

    def _fetch(self) -> dict[str | None, tuple]:
        # Results are stored here, before the future completes, so a loop
        # woken by its completion always finds them in place.
        try:
            jwks_uri, algorithms, document = self.jwks_uri, self.algorithms, self.document
            if jwks_uri is None:
                jwks_uri, algorithms, document = self._discover()
            keys, fingerprint = _parse(_get_json(jwks_uri, self.timeout), self._jwt)
            if not keys:
                raise ValueError(f"{jwks_uri} holds no usable signing key")
        except Exception as exc:
            with self._lock:
                self._fetching = None
            logger.warning("oidc keys for %s could not be fetched: %s", self.issuer, exc)
            raise
        with self._lock:
            changed = self.keys is not None and fingerprint != self._fingerprint
            self.keys, self._fingerprint = keys, fingerprint
            self.jwks_uri, self.algorithms, self.document = jwks_uri, algorithms, document
            self._fetched = time.monotonic()
            self._fetching = None
        if changed:
            logger.info("oidc keys for %s changed", self.issuer)
            self._changed()
        return keys

    def _discover(self) -> tuple[str, frozenset[str], dict[str, Any]]:
        document = _get_json(self.discovery_url, self.timeout)
        if not isinstance(document, dict):
            raise ValueError(f"{self.discovery_url} is not a discovery document")
        # OpenID Connect Discovery requires the document's issuer to be the
        # one asked about, exactly; anything else is a different provider.
        if document.get("issuer") != self.issuer:
            raise ValueError(
                f"the discovery document names issuer {document.get('issuer')!r}, "
                f"not {self.issuer!r}"
            )
        jwks_uri = document.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise ValueError(f"{self.discovery_url} has no jwks_uri")
        check_url(jwks_uri, self.allow_http, "jwks_uri")
        if self._explicit is not None:
            return jwks_uri, self._explicit, document
        advertised = document.get("id_token_signing_alg_values_supported")
        if not isinstance(advertised, list):
            return jwks_uri, frozenset(COMPATIBLE), document
        algorithms = frozenset(a for a in advertised if a in COMPATIBLE)
        if not algorithms:
            raise ValueError(f"{self.issuer} advertises no public-key signing algorithm")
        return jwks_uri, algorithms, document

    # ---- asking: on any worker loop ----------------------------------------------

    async def _wait(self, future: concurrent.futures.Future) -> dict[str | None, tuple]:
        # Shielded: a request that gives up must not cancel a fetch that other
        # requests, on other loops, are waiting for too.
        try:
            return await asyncio.shield(asyncio.wrap_future(future))
        except Exception as exc:  # noqa: BLE001 - any failure to fetch is the same to a caller
            raise Unavailable(str(exc) or type(exc).__name__) from None

    async def load(self) -> None:
        future = self._start(0.0)
        if future is not None:
            await self._wait(future)

    async def discovered(self) -> dict[str, Any]:
        """The discovery document, fetching it (and the keys) if need be."""
        document = self.document
        if document is None:
            future = self._start(RETRY_INTERVAL)
            if future is None:
                raise Unavailable("the last attempt failed moments ago")
            await self._wait(future)
            document = self.document
            if document is None:
                raise Unavailable("no discovery document")
        return document

    def refresh_if_stale(self) -> None:
        # Stale: fetch again in the background, and meanwhile use the keys in
        # hand, so a slow provider slows nobody.
        if self.keys is not None and time.monotonic() - self._fetched > self.max_age:
            # Spaced only after a failure: after a success, being stale is
            # itself the spacing.
            self._start(RETRY_INTERVAL if self._attempted > self._fetched else 0.0)

    async def find(self, kid: str | None, alg: str) -> Any:
        """The key to check a token signed with `alg` under `kid`."""
        keys = self.keys
        if keys is None:
            future = self._start(RETRY_INTERVAL)
            if future is None:
                raise Unavailable("the last attempt failed moments ago")
            keys = await self._wait(future)
        else:
            self.refresh_if_stale()
        if alg not in self.algorithms:
            raise Refused(f"algorithm {alg!r} is not accepted")
        entry = _pick(keys, kid)
        if entry is None and kid is not None:
            future = self._start(REFETCH_INTERVAL)
            if future is not None:
                try:
                    keys = await self._wait(future)
                except Unavailable:
                    keys = self.keys or {}
                entry = _pick(keys, kid)
        if entry is None:
            raise Refused(f"no key {kid!r}" if kid is not None else "no key id")
        kty, crv, key_alg, key = entry
        wanted_kty, curves = COMPATIBLE[alg]
        if kty != wanted_kty or (curves is not None and crv not in curves):
            raise Refused(f"algorithm {alg} with a {kty} key")
        if key_alg is not None and key_alg != alg:
            raise Refused(f"algorithm {alg} with a key for {key_alg}")
        return key


def _pick(keys: dict[str | None, tuple], kid: str | None) -> tuple | None:
    if kid is None:
        # A token without a key id is checked only against a set of one: with
        # more, which key signed it would be a guess.
        return next(iter(keys.values())) if len(keys) == 1 else None
    return keys.get(kid)


__all__ = ["COMPATIBLE", "KeySet", "Refused", "Unavailable", "call_json", "check_url"]
