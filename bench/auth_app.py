"""The app bench/auth.py measures: one route per scheme, and one without.

JWT routes come in pairs, with the verified-token cache and without it, since
the cache is what the first request pays and every later one skips. The OIDC
route's issuer is this app itself, serving its own discovery document and
key set, so the measured path is the whole of it: key lookup included.

`--eager` collects a protected route's body before authenticating, as an
unprotected route does, so the deferred body's cost can be measured against
the alternative rather than argued about.
"""
import argparse
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from oxbrook import App, Request
from oxbrook.auth import JWT, OIDC, APIKey, Principal
from pydantic import BaseModel

KEY = "sk-bench-" + "k" * 40
SECRET = "s" * 32
DIGESTS = {APIKey.digest(KEY): Principal(subject="bench")}

_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC = _private.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)
PRIVATE = _private.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)


def claims() -> dict:
    return {"sub": "bench", "aud": "bench", "exp": int(time.time()) + 3600}


def hs256_token() -> str:
    return jwt.encode(claims(), SECRET, algorithm="HS256")


def rs256_token(private: bytes) -> str:
    return jwt.encode(claims(), private, algorithm="RS256")


def issuer(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def oidc_token(private: bytes, port: int) -> str:
    return jwt.encode({**claims(), "iss": issuer(port)}, private, algorithm="RS256",
                      headers={"kid": "bench"})


def build(public_key: bytes, port: int) -> App:
    keys = APIKey(header="x-api-key", verify=DIGESTS.get)
    hs = JWT(key=SECRET, algorithms=["HS256"], audience="bench")
    hs_nocache = JWT(key=SECRET, algorithms=["HS256"], audience="bench", cache_size=0,
                     name="hs_nocache")
    rs = JWT(key=public_key, algorithms=["RS256"], audience="bench", name="rs")
    rs_nocache = JWT(key=public_key, algorithms=["RS256"], audience="bench", cache_size=0,
                     name="rs_nocache")
    provider = OIDC(issuer(port), audience="bench")
    provider_nocache = OIDC(issuer(port), audience="bench", cache_size=0, name="oidc_nocache")
    jwk = {**RSAAlgorithm.to_jwk(serialization.load_pem_public_key(public_key), as_dict=True),
           "kid": "bench", "alg": "RS256", "use": "sig"}
    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/.well-known/openid-configuration")
    async def discovery(_: Request):
        return {"issuer": issuer(port), "jwks_uri": issuer(port) + "/keys",
                "id_token_signing_alg_values_supported": ["RS256"]}

    @app.get("/keys")
    async def key_set(_: Request):
        return {"keys": [jwk]}

    class Item(BaseModel):
        name: str
        size: int

    @app.get("/public")
    async def public(_: Request):
        return {"hello": "world"}

    @app.get("/key", auth=keys)
    async def key(_: Request):
        return {"hello": "world"}

    @app.get("/hs256", auth=hs)
    async def hs256(_: Request):
        return {"hello": "world"}

    @app.get("/rs256", auth=rs)
    async def rs256(_: Request):
        return {"hello": "world"}

    @app.get("/hs256-nocache", auth=hs_nocache)
    async def hs256_nocache(_: Request):
        return {"hello": "world"}

    @app.get("/rs256-nocache", auth=rs_nocache)
    async def rs256_nocache(_: Request):
        return {"hello": "world"}

    @app.get("/oidc", auth=provider)
    async def oidc(_: Request):
        return {"hello": "world"}

    @app.get("/oidc-nocache", auth=provider_nocache)
    async def oidc_nocache(_: Request):
        return {"hello": "world"}

    @app.post("/public-body")
    async def public_body(_: Request, item: Item):
        return {"name": item.name}

    @app.post("/key-body", auth=keys)
    async def key_body(_: Request, item: Item):
        return {"name": item.name}

    return app


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--public-key", required=True)
    p.add_argument("--eager", action="store_true")
    a = p.parse_args()
    app = build(open(a.public_key, "rb").read(), a.port)
    if a.eager:
        # Measurement only: the body mode is not a public setting.
        spec = App._spec

        def eager(self, route):
            built = spec(self, route)
            return built[:6] + ("collect" if built[6] == "defer" else built[6],) + built[7:]

        App._spec = eager
    app.run(port=a.port, workers=a.workers)
