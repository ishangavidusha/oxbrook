"""The app bench/auth.py measures: one route per scheme, and one without.

`--eager` collects a protected route's body before authenticating, as an
unprotected route does, so the deferred body's cost can be measured against
the alternative rather than argued about.
"""
import argparse
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from oxbrook import App, Request
from oxbrook.auth import JWT, APIKey, Principal
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


def build(public_key: bytes) -> App:
    keys = APIKey(header="x-api-key", verify=DIGESTS.get)
    hs = JWT(key=SECRET, algorithms=["HS256"], audience="bench")
    rs = JWT(key=public_key, algorithms=["RS256"], audience="bench", name="rs")
    app = App(openapi_url=None, docs_url=None, mcp_url=None)

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
    app = build(open(a.public_key, "rb").read())
    if a.eager:
        # Measurement only: the body mode is not a public setting.
        spec = App._spec

        def eager(self, route):
            built = spec(self, route)
            return built[:6] + ("collect" if built[6] == "defer" else built[6],) + built[7:]

        App._spec = eager
    app.run(port=a.port, workers=a.workers)
