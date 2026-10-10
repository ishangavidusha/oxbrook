from contextlib import asynccontextmanager
from datetime import datetime

import asyncpg
from oxbrook import App, HTTPError, Reply, Request, Settings
from pydantic import BaseModel, field_validator


class Config(Settings):
    database_url: str


config = Config()


class BookmarkIn(BaseModel):
    url: str
    title: str
    tags: list[str] = []

    @field_validator("url")
    @classmethod
    def http_only(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return value


class Bookmark(BookmarkIn):
    id: int
    created_at: datetime


@asynccontextmanager
async def lifespan(_app):
    conn = await asyncpg.connect(config.database_url)
    try:
        await conn.execute(
            "create table if not exists bookmarks (id bigserial primary key, url text not null,"
            " title text not null, tags text[] not null default '{}',"
            " created_at timestamptz not null default now())")
    finally:
        await conn.close()
    yield {}


@asynccontextmanager
async def worker_lifespan(app):
    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=app.per_worker(20))
    try:
        yield {"db": pool}
    finally:
        await pool.close()


app = App(lifespan=lifespan, worker_lifespan=worker_lifespan)
COLUMNS = "id, url, title, tags, created_at"


@app.post("/bookmarks")
async def create(request: Request, bookmark: BookmarkIn):
    row = await request.state.db.fetchrow(
        f"insert into bookmarks (url, title, tags) values ($1, $2, $3) returning {COLUMNS}",
        bookmark.url, bookmark.title, bookmark.tags)
    return Reply(Bookmark(**dict(row)), status=201)


@app.get("/bookmarks")
async def list_all(request: Request, tag: str | None = None):
    if tag is None:
        rows = await request.state.db.fetch(f"select {COLUMNS} from bookmarks order by id desc")
    else:
        rows = await request.state.db.fetch(
            f"select {COLUMNS} from bookmarks where $1 = any(tags) order by id desc", tag)
    return [Bookmark(**dict(r)) for r in rows]


@app.get("/bookmarks/{bookmark_id}")
async def read(request: Request, bookmark_id: int):
    row = await request.state.db.fetchrow(
        f"select {COLUMNS} from bookmarks where id = $1", bookmark_id)
    if row is None:
        raise HTTPError(404)
    return Bookmark(**dict(row))


@app.delete("/bookmarks/{bookmark_id}")
async def delete(request: Request, bookmark_id: int):
    deleted = await request.state.db.fetchval(
        "delete from bookmarks where id = $1 returning id", bookmark_id)
    if deleted is None:
        raise HTTPError(404)
