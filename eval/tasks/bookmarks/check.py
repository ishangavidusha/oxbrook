"""Acceptance checks for the bookmarks task, against four worker loops.

Four loops because the classic mistake — one connection pool made at import or
in a process-wide lifespan — works on one loop and fails as soon as a request
lands on another. The concurrent check sends enough traffic, from separate
connections, to reach all of them.
"""

import asyncio
import os
import threading
import uuid
from datetime import datetime

import httpx


def check(c) -> None:
    marker = uuid.uuid4().hex[:10]
    tag = f"tag-{marker}"
    with c.serve(workers=4) as client:
        made = {}

        def create():
            r = client.post("/bookmarks", json={
                "url": f"https://example.com/{marker}", "title": "Example", "tags": [tag, "x"]})
            assert r.status_code == 201, f"{r.status_code} {r.text[:200]}"
            body = r.json()
            assert isinstance(body.get("id"), int), body
            assert body.get("url") == f"https://example.com/{marker}", body
            assert body.get("title") == "Example", body
            assert sorted(body.get("tags") or []) == sorted([tag, "x"]), body
            datetime.fromisoformat(str(body.get("created_at")).replace("Z", "+00:00"))
            made["first"] = body
        c.run("create", create)

        def default_tags():
            r = client.post("/bookmarks",
                            json={"url": f"http://example.org/{marker}", "title": "T"})
            assert r.status_code == 201 and r.json().get("tags") == [], \
                f"{r.status_code} {r.text[:200]}"
        c.run("tags default to empty", default_tags)

        def validation():
            bad = [
                {"url": f"ftp://example.com/{marker}", "title": "x"},
                {"url": "example.com", "title": "x"},
                {"url": f"https://example.com/{marker}"},
                {"title": "no url"},
            ]
            codes = [client.post("/bookmarks", json=b).status_code for b in bad]
            assert codes == [422] * 4, f"invalid bodies answered {codes}"
        c.run("invalid bodies are 422", validation)

        def read_one():
            first = made["first"]
            r = client.get(f"/bookmarks/{first['id']}")
            assert r.status_code == 200 and r.json().get("id") == first["id"], r.text[:200]
            assert client.get("/bookmarks/987654321").status_code == 404
        c.run("read one, and 404", read_one)

        def newest_first_and_filter():
            second = client.post("/bookmarks", json={
                "url": f"https://example.net/{marker}", "title": "Second", "tags": [tag]}).json()
            r = client.get("/bookmarks", params={"tag": tag})
            assert r.status_code == 200, r.text[:200]
            ids = [b["id"] for b in r.json()]
            assert ids == [second["id"], made["first"]["id"]], f"?tag= gave ids {ids}"
            everything = [b["id"] for b in client.get("/bookmarks").json()]
            assert everything.index(second["id"]) < everything.index(made["first"]["id"]), \
                "not newest first"
        c.run("newest first, and ?tag=", newest_first_and_filter)

        def delete():
            first = made["first"]
            assert client.delete(f"/bookmarks/{first['id']}").status_code == 204
            assert client.get(f"/bookmarks/{first['id']}").status_code == 404
            assert client.delete(f"/bookmarks/{first['id']}").status_code == 404
        c.run("delete, and 404 after", delete)

        def concurrent():
            errors: list[str] = []

            def worker(n: int) -> None:
                with httpx.Client(base_url=client.base_url, timeout=10) as own:
                    for i in range(12):
                        try:
                            r = own.post("/bookmarks", json={
                                "url": f"https://load.example/{marker}/{n}/{i}", "title": "load",
                                "tags": [f"load-{marker}"]})
                            if r.status_code != 201:
                                errors.append(f"POST {r.status_code} {r.text[:120]}")
                                continue
                            got = own.get(f"/bookmarks/{r.json()['id']}")
                            if got.status_code != 200:
                                errors.append(f"GET {got.status_code} {got.text[:120]}")
                        except Exception as exc:
                            errors.append(f"{type(exc).__name__}: {exc}")

            threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert not errors, f"{len(errors)} of 192 requests failed, e.g. {errors[:3]}"
        c.run("192 requests from 8 connections across 4 loops", concurrent)

    def stored_in_postgres():
        import asyncpg

        async def find() -> int:
            conn = await asyncpg.connect(os.environ["DATABASE_URL"])
            try:
                tables = await conn.fetch(
                    "select table_schema, table_name from information_schema.tables "
                    "where table_schema not in ('pg_catalog', 'information_schema')")
                found = 0
                for t in tables:
                    found += await conn.fetchval(
                        f'select count(*) from "{t[0]}"."{t[1]}" as r where r::text like $1',
                        f"%example.net/{marker}%")
                return found
            finally:
                await conn.close()

        assert asyncio.run(find()) >= 1, "the bookmark is not in the database"
    c.run("stored in PostgreSQL", stored_in_postgres)
