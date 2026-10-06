#!/usr/bin/env python3
"""The database pattern, run against a real PostgreSQL.

Needs a PostgreSQL on OXBROOK_TEST_POSTGRES (default
postgresql://oxbrook:oxbrook@127.0.0.1:5499/oxbrook, which `make up` starts).
Every run makes its own databases and drops them afterwards.

What is asserted, each against a running server:

* the example's migrations apply, reverse, and apply again; two replicas
  migrating at once both succeed rather than racing
* the example serves across worker loops, each with its own pool, and the
  connections the process holds stay inside the budget it declared
* a request is one transaction: kept if the handler returned, gone if it
  raised, gone if the commit itself failed (and the client is told so), gone
  if the client left and the handler was cancelled
* SQLAlchemy's async engine works the same way when it is made per loop
* migrating from the process lifespan works, through a thread

With OXBROOK_REQUIRE_POSTGRES set an unreachable database is a failure rather
than a SKIP; `make verify` and CI both set it.

    python tests/database.py --check    report reachability and stop
"""
import asyncio
import concurrent.futures
import importlib.util
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from oxbrook import App, Depends, HTTPError, Request
from oxbrook.testing import TestClient

try:
    import asyncpg

    HAVE_ASYNCPG = True
except ModuleNotFoundError:
    HAVE_ASYNCPG = False

URL = os.environ.get("OXBROOK_TEST_POSTGRES", "postgresql://oxbrook:oxbrook@127.0.0.1:5499/oxbrook")
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "database"
WORKERS = 4
TAG = uuid.uuid4().hex[:8]

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def database_url(name: str) -> str:
    return URL.rsplit("/", 1)[0] + "/" + name


async def reachable() -> bool:
    if not HAVE_ASYNCPG:
        return False
    try:
        conn = await asyncio.wait_for(asyncpg.connect(URL), 3)
        await conn.close()
        return True
    except Exception:
        return False


async def admin(sql: str) -> None:
    conn = await asyncpg.connect(URL)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def query(name: str, sql: str, *args):
    conn = await asyncpg.connect(database_url(name))
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def execute(name: str, sql: str) -> None:
    conn = await asyncpg.connect(database_url(name))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


def fresh(label: str) -> str:
    name = f"oxbrook_{label}_{TAG}"
    asyncio.run(admin(f'create database "{name}"'))
    created.append(name)
    return name


created: list[str] = []


def alembic_env(name: str) -> dict[str, str]:
    # Under `make coverage` this process is measured with COVERAGE_CORE=sysmon,
    # and coverage follows it into alembic through COVERAGE_PROCESS_CONFIG. On
    # Linux, CPython 3.14.8t with sys.monitoring active and greenlet loaded
    # (SQLAlchemy's async layer) hangs at interpreter exit, after the migration
    # has finished; 3.14.7t does not. Alembic never imports oxbrook, so there is
    # nothing to measure there: leave it unmeasured.
    env = {**os.environ, "DATABASE_URL": database_url(name)}
    env.pop("COVERAGE_PROCESS_CONFIG", None)
    return env


def alembic(name: str, *args: str) -> subprocess.CompletedProcess:
    # The documented command, run the way a deploy step runs it: its own
    # process, in the example's directory, configured by DATABASE_URL.
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=EXAMPLE,
        env=alembic_env(name),
        capture_output=True,
        text=True,
        timeout=60,
    )


def tables(name: str) -> set[str]:
    rows = asyncio.run(
        query(name, "select tablename from pg_tables where schemaname = 'public'")
    )
    return {row["tablename"] for row in rows}


def load_example(name: str):
    # The example reads DATABASE_URL when imported, as an application would.
    os.environ["DATABASE_URL"] = database_url(name)
    spec = importlib.util.spec_from_file_location(f"notes_{name}", EXAMPLE / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def burst(client: TestClient, path: str, count: int = 200) -> dict[int, int]:
    # A client per thread, not the TestClient's shared one: httpcore 1.0.9
    # checks a pooled connection's expiry and then reads it, and on the
    # free-threaded build another thread can clear it in between. That race is
    # the client library's, and it must not read as a server failure.
    local = threading.local()

    def one(_):
        if not hasattr(local, "http"):
            local.http = httpx.Client(base_url=client.base_url)
            opened.append(local.http)
        return local.http.get(path).status_code

    opened: list[httpx.Client] = []
    codes: dict[int, int] = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(32) as pool:
            for code in pool.map(one, range(count)):
                codes[code] = codes.get(code, 0) + 1
    finally:
        for http in opened:
            http.close()
    return codes


# ---- migrations -------------------------------------------------------------


def migrations_apply_and_reverse() -> None:
    name = fresh("migrate")
    up = alembic(name, "upgrade", "head")
    check(up.returncode == 0, f"alembic upgrade head failed:\n{up.stderr[-800:]}")
    check("notes" in tables(name), "upgrade head did not create the notes table")

    down = alembic(name, "downgrade", "base")
    check(down.returncode == 0, f"alembic downgrade base failed:\n{down.stderr[-800:]}")
    check("notes" not in tables(name), "downgrade base left the notes table behind")

    again = alembic(name, "upgrade", "head")
    check(again.returncode == 0, "a second upgrade after a downgrade failed")


def replicas_migrating_at_once_do_not_race() -> None:
    # Two replicas that each migrate as they start. Without the advisory lock
    # in env.py both see an empty database and one fails creating the table
    # the other just created.
    name = fresh("race")
    env = alembic_env(name)
    runs = [
        subprocess.Popen(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=EXAMPLE,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    outcomes = [(run.wait(60), run.stderr.read()) for run in runs]
    for code, err in outcomes:
        check(code == 0, f"one of four concurrent migrations failed:\n{err[-600:]}")
    version = asyncio.run(query(name, "select version_num from alembic_version"))
    check(
        [row["version_num"] for row in version] == ["0001"],
        f"alembic_version holds {[row['version_num'] for row in version]}",
    )


# ---- the example ------------------------------------------------------------


def the_example_serves() -> None:
    name = fresh("example")
    check(alembic(name, "upgrade", "head").returncode == 0, "could not migrate the example")
    example = load_example(name)

    with TestClient(example.app, workers=WORKERS) as c:
        r = c.post("/notes", json={"title": "first", "body": "hello"})
        check(r.status_code == 201, f"creating a note answered {r.status_code}: {r.text}")
        note = r.json() if r.status_code == 201 else {}
        check(
            isinstance(note.get("created_at"), str) and "T" in note["created_at"],
            f"created_at came back as {note.get('created_at')!r}, not ISO 8601",
        )

        r = c.post("/notes", json={"title": "first", "body": "again"})
        check(r.status_code == 409, f"a duplicate title answered {r.status_code}, not 409")

        listed = c.get("/notes")
        check(
            listed.status_code == 200 and len(listed.json()) == 1,
            f"listing gave {listed.status_code} {listed.text[:200]}; the rejected "
            f"duplicate must not have been kept",
        )

        nid = note.get("id")
        r = c.patch(f"/notes/{nid}", json={"body": "edited"})
        check(r.status_code == 200 and r.json()["body"] == "edited", f"patch gave {r.text}")
        check(c.get(f"/notes/{nid}").json()["body"] == "edited", "an update was not kept")

        check(c.delete(f"/notes/{nid}").status_code == 204, "deleting a note did not answer 204")
        check(c.delete(f"/notes/{nid}").status_code == 404, "deleting it twice did not 404")
        check(c.get(f"/notes/{nid}").status_code == 404, "a deleted note is still served")

        # Every loop reads through its own pool. A pool shared between loops
        # fails most of a burst like this one with "another operation is in
        # progress" or "attached to a different loop".
        codes = burst(c, "/notes")
        check(codes == {200: 200}, f"a concurrent burst across loops gave {codes}")

        held = asyncio.run(
            query(
                name,
                "select count(*) as n from pg_stat_activity "
                "where datname = $1 and pid <> pg_backend_pid()",
                name,
            )
        )[0]["n"]
        loops = example.app.workers
        check(
            loops <= held <= example.CONNECTIONS,
            f"{loops} loops hold {held} connections; the process declared "
            f"{example.CONNECTIONS} for all of them",
        )


# ---- transactions -----------------------------------------------------------


SCHEMA = """
create table kept (k text primary key);
create table deferred (
    k text not null,
    constraint deferred_k unique (k) deferrable initially deferred
);
"""


def transactional_app(name: str, entered: list) -> App:
    @asynccontextmanager
    async def worker_lifespan(app):
        pool = await asyncpg.create_pool(
            database_url(name), min_size=1, max_size=app.per_worker(8)
        )
        try:
            yield {"pool": pool}
        finally:
            await pool.close()

    app = App(worker_lifespan=worker_lifespan)

    async def transaction(request):
        async with request.state.pool.acquire() as db, db.transaction():
            yield db

    @app.exception_handler(asyncpg.UniqueViolationError)
    async def duplicate(request, exc):
        raise HTTPError(409, "already exists")

    @app.post("/kept/{k}")
    async def write(_: Request, k: str, db=Depends(transaction)):
        await db.execute("insert into kept values ($1)", k)
        return {"k": k}

    @app.post("/refused/{k}")
    async def refused(_: Request, k: str, db=Depends(transaction)):
        await db.execute("insert into kept values ($1)", k)
        raise HTTPError(400, "changed my mind")

    @app.post("/twice/{k}")
    async def twice(_: Request, k: str, db=Depends(transaction)):
        await db.execute("insert into kept values ($1)", k + "-first")
        await db.execute("insert into kept values ($1)", k)
        await db.execute("insert into kept values ($1)", k)
        return {"k": k}

    @app.post("/deferred/{k}")
    async def deferred(_: Request, k: str, db=Depends(transaction)):
        # Both inserts succeed: the constraint is checked at COMMIT, which is
        # in the dependency's teardown, after this has returned.
        await db.execute("insert into deferred values ($1)", k)
        await db.execute("insert into deferred values ($1)", k)
        return {"written": k}

    @app.post("/slow/{k}")
    async def slow(_: Request, k: str, db=Depends(transaction)):
        await db.execute("insert into kept values ($1)", k)
        entered.append(k)
        await asyncio.sleep(10)
        return {"k": k}

    @app.get("/count")
    async def count(request: Request):
        async with request.state.pool.acquire() as db:
            return {"n": await db.fetchval("select count(*) from kept")}

    return app


def a_request_is_one_transaction() -> None:
    name = fresh("tx")
    asyncio.run(execute(name, SCHEMA))
    entered: list = []

    def rows(table: str) -> list[str]:
        return sorted(r["k"] for r in asyncio.run(query(name, f"select k from {table}")))

    with TestClient(transactional_app(name, entered), workers=WORKERS) as c:
        r = c.post("/kept/a")
        check(r.status_code == 200, f"a plain write answered {r.status_code}")
        check(
            rows("kept") == ["a"],
            f"a handler that returned left {rows('kept')}: its transaction was not "
            f"committed",
        )

        r = c.post("/refused/b")
        check(r.status_code == 400, f"a refusing handler answered {r.status_code}")
        check("b" not in rows("kept"), "a handler that raised HTTPError had its write kept")

        r = c.post("/twice/c")
        check(r.status_code == 409, f"a unique violation in the handler answered {r.status_code}")
        check(
            "c-first" not in rows("kept"),
            "a write made before the failing statement was kept: the request was not "
            "one transaction",
        )

        r = c.post("/deferred/d")
        check(
            r.status_code == 409,
            f"a commit that failed answered {r.status_code}; the client must not be "
            f"told a write succeeded when it was not kept",
        )
        check(rows("deferred") == [], f"a failed commit left {rows('deferred')}")

        # The client leaves while the handler is inside its transaction. The
        # handler is cancelled, CancelledError reaches the dependency's yield,
        # and the transaction is rolled back rather than left open.
        try:
            c.post("/slow/e", timeout=0.5)
        except httpx.TimeoutException:
            pass
        deadline = time.monotonic() + 5
        while not entered and time.monotonic() < deadline:
            time.sleep(0.05)
        check(entered == ["e"], "the slow handler never started")
        time.sleep(0.5)
        check("e" not in rows("kept"), "a cancelled request's write was committed")

        # And its connection went back to its pool: nothing is wedged.
        codes = burst(c, "/count", 100)
        check(codes == {200: 100}, f"after a cancelled transaction, a burst gave {codes}")


# ---- SQLAlchemy -------------------------------------------------------------


def sqlalchemy_per_loop() -> None:
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    except ModuleNotFoundError:
        failures.append("sqlalchemy is not installed; the guide's SQLAlchemy section is untested")
        return

    name = fresh("sqla")
    check(alembic(name, "upgrade", "head").returncode == 0, "could not migrate for sqlalchemy")
    sa_url = database_url(name).replace("postgresql://", "postgresql+asyncpg://", 1)

    @asynccontextmanager
    async def worker_lifespan(app):
        # Per loop, like any async pool: an engine made at import time is one
        # pool shared by every loop, and fails intermittently under load.
        engine = create_async_engine(sa_url, pool_size=app.per_worker(8), max_overflow=0)
        try:
            yield {"sessions": async_sessionmaker(engine, expire_on_commit=False)}
        finally:
            await engine.dispose()

    app = App(worker_lifespan=worker_lifespan)

    async def session(request):
        async with request.state.sessions() as s, s.begin():
            yield s

    @app.post("/notes/{title}")
    async def create(_: Request, title: str, s=Depends(session)):
        await s.execute(text("insert into notes (title) values (:t)"), {"t": title})
        return {"title": title}

    @app.post("/refused/{title}")
    async def refused(_: Request, title: str, s=Depends(session)):
        await s.execute(text("insert into notes (title) values (:t)"), {"t": title})
        raise HTTPError(400, "no")

    @app.get("/count")
    async def count(_: Request, s=Depends(session)):
        return {"n": await s.scalar(text("select count(*) from notes"))}

    with TestClient(app, workers=WORKERS) as c:
        check(c.post("/notes/kept").status_code == 200, "a SQLAlchemy write failed")
        check(c.post("/refused/gone").status_code == 400, "a refusing SQLAlchemy route failed")
        titles = sorted(
            r["title"] for r in asyncio.run(query(name, "select title from notes"))
        )
        check(titles == ["kept"], f"SQLAlchemy transactions left {titles}")
        codes = burst(c, "/count")
        check(codes == {200: 200}, f"a SQLAlchemy burst across loops gave {codes}")


# ---- migrating at startup ---------------------------------------------------


def migrating_from_the_lifespan() -> None:
    from alembic import command
    from alembic.config import Config

    name = fresh("startup")
    os.environ["DATABASE_URL"] = database_url(name)

    @asynccontextmanager
    async def lifespan(app):
        config = Config(str(EXAMPLE / "alembic.ini"))
        config.attributes["configure_logger"] = False
        # env.py calls asyncio.run, which refuses to run inside a running
        # loop, and the lifespan is one. A thread has no loop.
        await asyncio.to_thread(command.upgrade, config, "head")
        yield {}

    app = App(lifespan=lifespan)

    @app.get("/")
    async def root(_: Request):
        return {"ok": True}

    with TestClient(app) as c:
        check(c.get("/").status_code == 200, "an app that migrated at startup does not serve")
    check("notes" in tables(name), "migrating from the lifespan did not create the table")


def main() -> None:
    check_only = "--check" in sys.argv[1:]

    if not asyncio.run(reachable()):
        why = "asyncpg not installed" if not HAVE_ASYNCPG else f"no postgres at {URL}"
        print(f"postgres: unavailable ({why})")
        if os.environ.get("OXBROOK_REQUIRE_POSTGRES"):
            print("start one with `make up`, or accept the gap with `make verify POSTGRES=`")
            print("\nRESULT: FAIL (postgres is required and unreachable)")
            sys.exit(1)
        print("\nRESULT: SKIP")
        sys.exit(0)

    if check_only:
        print(f"postgres: {URL}")
        return

    logging.getLogger("oxbrook").setLevel(logging.CRITICAL)
    try:
        for step in (
            migrations_apply_and_reverse,
            replicas_migrating_at_once_do_not_race,
            the_example_serves,
            a_request_is_one_transaction,
            sqlalchemy_per_loop,
            migrating_from_the_lifespan,
        ):
            try:
                step()
                print(f"  {step.__name__}: ok")
            except Exception as exc:
                failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
                print(f"  {step.__name__}: ERROR")
    finally:
        for name in created:
            try:
                asyncio.run(admin(f'drop database if exists "{name}" with (force)'))
            except Exception as exc:
                print(f"  could not drop {name}: {exc}")

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
