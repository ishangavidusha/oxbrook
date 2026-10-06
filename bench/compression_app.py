"""The app bench/compression.py measures: one small reply, and two JSON
replies of the sizes an API sends, with compression on or off.

`/items` is about 14 KB and compresses inline on the tokio thread; `/large`
is about 140 KB, over the inline limit, and goes to the blocking pool.
"""
import argparse

from oxbrook import App, Compression, Request

ITEMS = [
    {"id": i, "title": f"Note number {i} about the quarterly plan", "owner": f"user-{i % 37}",
     "tags": ["work", "draft", "urgent" if i % 3 == 0 else "later"],
     "created": "2026-10-06T10:00:00Z", "done": i % 2 == 0, "score": i * 1.37}
    for i in range(1000)
]
SMALL = {"ok": True}
MEDIUM = ITEMS[:100]


def build(compression: bool) -> App:
    app = App(openapi_url=None, docs_url=None, mcp_url=None,
              compression=Compression() if compression else None)

    @app.get("/small")
    async def small(_: Request):
        return SMALL

    @app.get("/items")
    async def items(_: Request):
        return MEDIUM

    @app.get("/large")
    async def large(_: Request):
        return ITEMS

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--compression", action="store_true")
    args = ap.parse_args()
    build(args.compression).run(port=args.port, workers=args.workers)
