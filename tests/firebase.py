#!/usr/bin/env python3
"""The Firebase pattern, run against the Firestore and Realtime Database
emulators.

Needs the emulators on OXBROOK_TEST_FIRESTORE and OXBROOK_TEST_FIREBASE_DATABASE
(default 127.0.0.1:8299 and 127.0.0.1:9099, which `make up` starts). Every run
uses a `demo-` project of its own, so runs never see each other's data.

What is asserted, each against a running server:

* the Realtime Database half of the Admin SDK loads on the free-threaded
  build without turning the GIL back on; Firestore, which loads grpc, is
  reported rather than asserted, since grpc decides that (see the guide)
* the example serves across worker loops through the blocking pool: one
  Firebase app for the process, its clients shared by every thread
* a Firestore transaction keeps a title unique under concurrent creates, and
  a request that raises inside one writes nothing
* a Realtime Database transaction counts every one of a concurrent burst

With OXBROOK_REQUIRE_FIREBASE set an unreachable emulator is a failure rather
than a SKIP; `make verify` and CI both set it.

    python tests/firebase.py --check    report reachability and stop
"""
import concurrent.futures
import importlib.util
import logging
import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import httpx
from oxbrook import HTTPError, Request
from oxbrook.testing import TestClient

FIRESTORE = os.environ.get("OXBROOK_TEST_FIRESTORE", "127.0.0.1:8299")
DATABASE = os.environ.get("OXBROOK_TEST_FIREBASE_DATABASE", "127.0.0.1:9099")
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "firebase"
WORKERS = 4
TAG = uuid.uuid4().hex[:8]

HAVE_SDK = importlib.util.find_spec("firebase_admin") is not None

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def reachable() -> bool:
    if not HAVE_SDK:
        return False
    for address in (FIRESTORE, DATABASE):
        host, port = address.rsplit(":", 1)
        try:
            with socket.create_connection((host, int(port)), timeout=3):
                pass
        except OSError:
            return False
    return True


def load_example(purpose: str):
    """The example, against a project of its own. It reads its settings when
    imported, as an application would."""
    project = f"demo-oxbrook-{purpose}-{TAG}"
    os.environ["FIRESTORE_EMULATOR_HOST"] = FIRESTORE
    os.environ["FIREBASE_DATABASE_EMULATOR_HOST"] = DATABASE
    os.environ["FIREBASE_PROJECT"] = project
    os.environ["FIREBASE_DATABASE_URL"] = f"http://{DATABASE}?ns={project}"
    spec = importlib.util.spec_from_file_location(f"firebase_notes_{purpose}", EXAMPLE / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def concurrently(client: TestClient, calls: list) -> list:
    """Run (method, path, json) calls at once, a client per thread (I-113)."""
    local = threading.local()
    opened: list[httpx.Client] = []

    def one(call):
        if not hasattr(local, "http"):
            local.http = httpx.Client(base_url=client.base_url, timeout=30)
            opened.append(local.http)
        method, path, body = call
        return local.http.request(method, path, json=body)

    try:
        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            return list(pool.map(one, calls))
    finally:
        for http in opened:
            http.close()


# ---- the SDK ----------------------------------------------------------------


def the_sdk_and_the_gil() -> None:
    if not hasattr(sys, "_is_gil_enabled") or sys._is_gil_enabled():
        return  # the standard build: nothing to turn back on

    def gil_after(imports: str) -> str:
        script = f"import sys, warnings; warnings.simplefilter('ignore'); {imports}; " \
                 "print(sys._is_gil_enabled())"
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                timeout=120)
        return result.stdout.strip() or result.stderr.strip()[-300:]

    rtdb = gil_after("import firebase_admin; from firebase_admin import db, auth")
    check(rtdb == "False", f"the Realtime Database half of the Admin SDK turned the GIL on: {rtdb}")
    store = gil_after("from firebase_admin import firestore")
    print(f"  (firestore on the free-threaded build: GIL enabled = {store}; the guide says True)")


# ---- the example --------------------------------------------------------------


def the_example_serves() -> None:
    example = load_example("example")
    with TestClient(example.app, workers=WORKERS) as c:
        r = c.post("/notes", json={"title": "first", "body": "hello"})
        check(r.status_code == 201, f"creating a note answered {r.status_code}: {r.text}")
        note = r.json() if r.status_code == 201 else {}
        nid = note.get("id", "")
        check(isinstance(note.get("created_at"), str) and "T" in note["created_at"],
              f"created_at came back as {note.get('created_at')!r}")

        r = c.post("/notes", json={"title": "first", "body": "again"})
        check(r.status_code == 409, f"a duplicate title answered {r.status_code}, not 409")
        listed = c.get("/notes")
        check(listed.status_code == 200 and len(listed.json()) == 1,
              f"listing gave {listed.status_code} {listed.text[:200]}")

        got = c.get(f"/notes/{nid}")
        check(got.status_code == 200 and got.json().get("views") == 1,
              f"reading the note gave {got.status_code} {got.text[:200]}")
        check(got.json().get("created_at") == note.get("created_at"),
              "the created_at read back differs from the one returned at creation")

        r = c.patch(f"/notes/{nid}", json={"body": "edited"})
        check(r.status_code == 200 and r.json().get("body") == "edited", f"patch gave {r.text}")

        # Twenty readers at once, across loops and pool threads: the view
        # counter's transaction must count every one.
        reads = concurrently(c, [("GET", f"/notes/{nid}", None)] * 20)
        check(all(r.status_code == 200 for r in reads),
              f"concurrent reads answered {sorted({r.status_code for r in reads})}")
        final = c.get(f"/notes/{nid}").json().get("views")
        check(final == 22, f"22 reads counted {final} views: increments were lost")

        check(c.delete(f"/notes/{nid}").status_code == 204, "deleting a note did not answer 204")
        check(c.delete(f"/notes/{nid}").status_code == 404, "deleting it twice did not 404")
        check(c.get(f"/notes/{nid}").status_code == 404, "a deleted note is still served")
        r = c.post("/notes", json={"title": "first", "body": "reborn"})
        check(r.status_code == 201, "deleting a note did not free its title")


def a_title_is_unique_under_a_race() -> None:
    example = load_example("race")
    with TestClient(example.app, workers=WORKERS) as c:
        results = concurrently(c, [("POST", "/notes", {"title": "same", "body": str(n)})
                                   for n in range(8)])
        codes = sorted(r.status_code for r in results)
        check(codes == [201] + [409] * 7,
              f"eight creates racing for one title answered {codes}, not one 201")
        check(len(c.get("/notes").json()) == 1, "a race for one title left more than one note")


def a_raising_request_writes_nothing() -> None:
    example = load_example("raise")
    firestore = example.firestore

    @example.app.post("/half/{k}", blocking=True)
    def half(request: Request, k: str):
        client = request.state.firestore
        ref = client.collection("half").document(k)

        @firestore.transactional
        def write(transaction):
            transaction.set(ref, {"k": k})
            raise HTTPError(400, "changed my mind")

        write(client.transaction())

    @example.app.get("/half/{k}", blocking=True)
    def read_half(request: Request, k: str):
        return {"exists": request.state.firestore.collection("half").document(k).get().exists}

    with TestClient(example.app, workers=WORKERS) as c:
        check(c.post("/half/a").status_code == 400, "the refusing handler did not answer 400")
        check(c.get("/half/a").json() == {"exists": False},
              "a transaction whose function raised was committed")


def main() -> None:
    check_only = "--check" in sys.argv[1:]

    if not reachable():
        why = "firebase-admin not installed" if not HAVE_SDK else \
            f"no emulators at {FIRESTORE} and {DATABASE}"
        print(f"firebase: unavailable ({why})")
        if os.environ.get("OXBROOK_REQUIRE_FIREBASE"):
            print("start them with `make up`, or accept the gap with `make verify FIREBASE=`")
            print("\nRESULT: FAIL (the firebase emulators are required and unreachable)")
            sys.exit(1)
        print("\nRESULT: SKIP")
        sys.exit(0)

    if check_only:
        print(f"firebase: firestore {FIRESTORE}, database {DATABASE}")
        return

    logging.getLogger("oxbrook").setLevel(logging.CRITICAL)
    for step in (the_sdk_and_the_gil, the_example_serves, a_title_is_unique_under_a_race,
                 a_raising_request_writes_nothing):
        try:
            step()
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR")

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
