#!/usr/bin/env python3
"""The MongoDB pattern, run against a real MongoDB replica set.

Needs a MongoDB on OXBROOK_TEST_MONGO (default
mongodb://127.0.0.1:27099/?directConnection=true, which `make up` starts as a
replica set of one, since transactions need a replica set). Every run uses
databases of its own and drops them afterwards.

What is asserted, each against a running server:

* PyMongo's C extensions load on the free-threaded build without turning the
  GIL back on, which is what makes the pattern usable on the primary target
* the example serves across worker loops, each with its own client: PyMongo
  refuses a client from another loop outright, so a shared one would turn a
  burst into 500s
* a document returned as it is encodes, `_id` and all
* a request is one transaction: kept if the handler returned, gone if it
  raised, gone if a later write in it failed, gone if the client left and the
  handler was cancelled

With OXBROOK_REQUIRE_MONGO set an unreachable server is a failure rather than
a SKIP; `make verify` and CI both set it.

    python tests/mongo.py --check    report reachability and stop
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
    from pymongo import AsyncMongoClient

    HAVE_PYMONGO = True
except ModuleNotFoundError:
    HAVE_PYMONGO = False

URL = os.environ.get("OXBROOK_TEST_MONGO", "mongodb://127.0.0.1:27099/?directConnection=true")
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "mongo"
WORKERS = 4
TAG = uuid.uuid4().hex[:8]

failures: list[str] = []
created: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def fresh(purpose: str) -> str:
    name = f"oxbrook_{purpose}_{TAG}"
    created.append(name)
    return name


async def reachable() -> bool:
    """Reachable and a writable primary: a standalone server would serve the
    reads and fail every transaction."""
    if not HAVE_PYMONGO:
        return False
    client = AsyncMongoClient(URL, serverSelectionTimeoutMS=3000)
    try:
        hello = await client.admin.command("hello")
        return bool(hello.get("isWritablePrimary") and hello.get("setName"))
    except Exception:
        return False
    finally:
        await client.close()


def run(coro_fn, *args):
    """Run a coroutine function against a client of its own, on a loop of its
    own: the client must not outlive the loop it was made on."""

    async def go():
        client = AsyncMongoClient(URL, tz_aware=True)
        try:
            return await coro_fn(client, *args)
        finally:
            await client.close()

    return asyncio.run(go())


def load_example(name: str):
    # The example reads its settings when imported, as an application would.
    os.environ["MONGO_URL"] = URL
    os.environ["MONGO_DATABASE"] = name
    spec = importlib.util.spec_from_file_location(f"mongo_notes_{name}", EXAMPLE / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def burst(client: TestClient, path: str, count: int = 200) -> dict[int, int]:
    # A client per thread, not the TestClient's shared one (I-113: httpcore
    # races when one client is shared across threads on 3.14t).
    local = threading.local()
    opened: list[httpx.Client] = []

    def one(_):
        if not hasattr(local, "http"):
            local.http = httpx.Client(base_url=client.base_url)
            opened.append(local.http)
        return local.http.get(path).status_code

    codes: dict[int, int] = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(32) as pool:
            for code in pool.map(one, range(count)):
                codes[code] = codes.get(code, 0) + 1
    finally:
        for http in opened:
            http.close()
    return codes


# ---- the driver ---------------------------------------------------------------


def the_driver_keeps_the_gil_off() -> None:
    if not hasattr(sys, "_is_gil_enabled") or sys._is_gil_enabled():
        return  # the standard build: nothing to turn back on
    script = (
        "import sys, pymongo, bson; "
        "assert pymongo.has_c() and bson.has_c(), 'pure-Python fallback'; "
        "print(sys._is_gil_enabled())"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            timeout=60)
    check(result.returncode == 0 and result.stdout.strip() == "False",
          f"importing PyMongo turned the GIL on or failed: {result.stdout} {result.stderr[-400:]}")


# ---- the example ----------------------------------------------------------------


def the_example_serves() -> None:
    name = fresh("example")
    example = load_example(name)

    with TestClient(example.app, workers=WORKERS) as c:
        r = c.post("/notes", json={"title": "first", "body": "hello"})
        check(r.status_code == 201, f"creating a note answered {r.status_code}: {r.text}")
        note = r.json() if r.status_code == 201 else {}
        nid = note.get("id", "")
        check(isinstance(nid, str) and len(nid) == 24,
              f"the note's id came back as {nid!r}, not an ObjectId's hex string")
        created_at = note.get("created_at")
        check(isinstance(created_at, str) and "T" in created_at
              and (created_at.endswith("Z") or "+00:00" in created_at),
              f"created_at came back as {created_at!r}, not ISO 8601 in UTC")

        r = c.post("/notes", json={"title": "first", "body": "again"})
        check(r.status_code == 409, f"a duplicate title answered {r.status_code}, not 409")

        listed = c.get("/notes")
        check(listed.status_code == 200 and len(listed.json()) == 1,
              f"listing gave {listed.status_code} {listed.text[:200]}; the rejected "
              "duplicate must not have been kept")
        got = c.get(f"/notes/{nid}")
        check(got.status_code == 200 and got.json().get("created_at") == created_at,
              f"reading the note back gave {got.status_code} {got.text[:200]}")

        r = c.patch(f"/notes/{nid}", json={"body": "edited"})
        check(r.status_code == 200 and r.json().get("body") == "edited", f"patch gave {r.text}")
        check(c.get(f"/notes/{nid}").json().get("body") == "edited", "an update was not kept")

        check(c.get("/notes/not-an-object-id").status_code == 404,
              "an id that is not an ObjectId did not answer 404")
        check(c.delete(f"/notes/{nid}").status_code == 204, "deleting a note did not answer 204")
        check(c.delete(f"/notes/{nid}").status_code == 404, "deleting it twice did not 404")
        check(c.get(f"/notes/{nid}").status_code == 404, "a deleted note is still served")

        # Every loop reads through its own client. PyMongo raises for a client
        # used from a loop other than its own, so a shared one fails here.
        codes = burst(c, "/notes")
        check(codes == {200: 200}, f"a concurrent burst across loops gave {codes}")

        spec = c.get("/openapi.json").json()
        schema = spec["paths"]["/notes/{note_id}"]["get"]["responses"]["200"]
        check("content" in schema, "the Note model is not in the OpenAPI document")

    activity = run(lambda client: client[name].activity.count_documents({}))
    check(activity == 1, f"{activity} activity records for one note created; the "
          "transaction must keep exactly the one beside the note that was kept")


# ---- transactions -------------------------------------------------------------


def transactional_app(name: str, example, entered: list) -> App:
    @asynccontextmanager
    async def worker_lifespan(app):
        client = AsyncMongoClient(URL, maxPoolSize=app.per_worker(20), tz_aware=True)
        try:
            yield {"mongo": client, "db": client[name]}
        finally:
            await client.close()

    app = App(worker_lifespan=worker_lifespan)
    app.exception_handler(example.DuplicateKeyError)(example.duplicate)
    transaction = example.transaction

    @app.post("/kept/{k}")
    async def kept(request: Request, k: str, session=Depends(transaction)):
        await request.state.db.kept.insert_one({"_id": k}, session=session)
        return {"written": k}

    @app.post("/refused/{k}")
    async def refused(request: Request, k: str, session=Depends(transaction)):
        await request.state.db.kept.insert_one({"_id": k}, session=session)
        raise HTTPError(400, "changed my mind")

    @app.post("/twice/{k}")
    async def twice(request: Request, k: str, session=Depends(transaction)):
        await request.state.db.kept.insert_one({"_id": f"{k}-first"}, session=session)
        await request.state.db.kept.insert_one({"_id": k}, session=session)
        await request.state.db.kept.insert_one({"_id": k}, session=session)
        return {"written": k}

    @app.post("/slow/{k}")
    async def slow(request: Request, k: str, session=Depends(transaction)):
        await request.state.db.kept.insert_one({"_id": k}, session=session)
        entered.append(k)
        await asyncio.sleep(10)
        return {"k": k}

    @app.get("/raw/{k}")
    async def raw(request: Request, k: str):
        # A document as PyMongo returns it: an ObjectId and a datetime inside.
        return await request.state.db.raw.find_one({"k": k})

    @app.get("/count")
    async def count(request: Request):
        return {"n": await request.state.db.kept.count_documents({})}

    return app


def a_request_is_one_transaction() -> None:
    name = fresh("tx")
    example = load_example(fresh("tx_example"))
    entered: list = []

    def kept() -> list[str]:
        async def ids(client):
            return sorted([doc["_id"] async for doc in client[name].kept.find()])

        return run(ids)

    async def seed(client):
        await client[name].raw.insert_one({"k": "a", "at": example.datetime.now(example.UTC)})
        # A collection must exist before a transaction writes to it on older
        # servers; creating it here keeps the test about the pattern.
        await client[name].create_collection("kept")

    run(seed)

    with TestClient(transactional_app(name, example, entered), workers=WORKERS) as c:
        r = c.post("/kept/a")
        check(r.status_code == 200, f"a plain write answered {r.status_code}: {r.text}")
        check(kept() == ["a"], f"a handler that returned left {kept()}: its transaction "
              "was not committed")

        r = c.post("/refused/b")
        check(r.status_code == 400, f"a refusing handler answered {r.status_code}")
        check("b" not in kept(), "a handler that raised HTTPError had its write kept")

        r = c.post("/twice/c")
        check(r.status_code == 409, f"a duplicate key in the handler answered {r.status_code}")
        check("c-first" not in kept() and "c" not in kept(),
              f"writes made before the failing one were kept ({kept()}): the request "
              "was not one transaction")

        # The client leaves while the handler is inside its transaction: the
        # handler is cancelled, the session's context aborts the transaction.
        try:
            c.post("/slow/e", timeout=0.5)
        except httpx.TimeoutException:
            pass
        deadline = time.monotonic() + 5
        while not entered and time.monotonic() < deadline:
            time.sleep(0.05)
        check(entered == ["e"], "the slow handler never started")
        time.sleep(0.5)
        check("e" not in kept(), "a cancelled request's write was committed")

        codes = burst(c, "/count", 100)
        check(codes == {200: 100}, f"after a cancelled transaction, a burst gave {codes}")

        r = c.get("/raw/a")
        body = r.json() if r.status_code == 200 else {}
        check(r.status_code == 200 and isinstance(body.get("_id"), str)
              and len(body["_id"]) == 24,
              f"a raw document answered {r.status_code} {r.text[:200]}; its ObjectId "
              "should encode as a hex string")


def main() -> None:
    check_only = "--check" in sys.argv[1:]

    if not asyncio.run(reachable()):
        why = "pymongo not installed" if not HAVE_PYMONGO else f"no replica set at {URL}"
        print(f"mongo: unavailable ({why})")
        if os.environ.get("OXBROOK_REQUIRE_MONGO"):
            print("start one with `make up`, or accept the gap with `make verify MONGO=`")
            print("\nRESULT: FAIL (mongo is required and unreachable)")
            sys.exit(1)
        print("\nRESULT: SKIP")
        sys.exit(0)

    if check_only:
        print(f"mongo: {URL}")
        return

    logging.getLogger("oxbrook").setLevel(logging.CRITICAL)
    try:
        for step in (the_driver_keeps_the_gil_off, the_example_serves,
                     a_request_is_one_transaction):
            try:
                step()
                print(f"  {step.__name__}: ok")
            except Exception as exc:
                failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
                print(f"  {step.__name__}: ERROR")
    finally:
        async def drop(client):
            for name in created:
                await client.drop_database(name)

        try:
            run(drop)
        except Exception as exc:
            print(f"  could not drop the test databases: {exc}")

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
