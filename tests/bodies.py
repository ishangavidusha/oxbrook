#!/usr/bin/env python3
"""Request body validation and model responses via pydantic."""
import sys
import threading

import httpx
from oxbrook import App, Request
from pydantic import BaseModel, Field

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"

app = App()


class UserIn(BaseModel):
    name: str = Field(min_length=1)
    age: int = Field(ge=0)
    email: str | None = None


class UserOut(BaseModel):
    id: int
    name: str
    age: int


@app.post("/users")
async def create_user(_: Request, body: UserIn):
    return UserOut(id=1, name=body.name, age=body.age)


@app.put("/users/{user_id}")
async def replace_user(_: Request, user_id: int, body: UserIn):
    return UserOut(id=user_id, name=body.name, age=body.age)


@app.post("/raw")
async def raw(_: Request, body: UserIn):
    return {"name": body.name, "kind": type(body).__name__}


@app.get("/none")
async def none(_: Request):
    return None


CASES = [
    ("POST", "/users", '{"name":"ada","age":36}', 200, {"id": 1, "name": "ada", "age": 36}),
    ("POST", "/users", '{"name":"ada","age":36,"email":"a@b.c"}', 200, {"id": 1, "name": "ada"}),
    ("POST", "/users", '{"name":"ada"}', 422, None),
    ("POST", "/users", '{"name":"","age":36}', 422, None),
    ("POST", "/users", '{"name":"ada","age":-1}', 422, None),
    ("POST", "/users", '{"name":1,"age":"x"}', 422, None),
    ("POST", "/users", "not json at all", 422, None),
    ("POST", "/users", "", 422, None),
    ("PUT", "/users/7", '{"name":"grace","age":45}', 200, {"id": 7, "name": "grace"}),
    ("PUT", "/users/abc", '{"name":"grace","age":45}', 422, None),
    ("POST", "/raw", '{"name":"ada","age":1}', 200, {"name": "ada", "kind": "UserIn"}),
    ("GET", "/none", None, 204, None),
]


def registration_checks() -> list[str]:
    failures = []

    def two_bodies():
        bad = App()

        @bad.post("/x")
        async def h(_: Request, a: UserIn, b: UserOut):
            return {}

    def unknown_arg():
        bad = App()

        @bad.post("/x")
        async def h(_: Request, mystery: dict):
            return {}

    for label, fn in [("two body models", two_bodies), ("unannotated extra arg", unknown_arg)]:
        try:
            fn()
            failures.append(f"{label}: no error raised")
        except TypeError:
            pass
        except Exception as e:
            failures.append(f"{label}: raised {type(e).__name__}: {e}")
    return failures


def main() -> None:
    failures = registration_checks()
    print(f"registration checks: {'PASS' if not failures else 'FAIL'}")

    threading.Thread(target=lambda: app.run(port=PORT), daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/none", timeout=0.3)
            break
        except Exception:
            threading.Event().wait(0.1)

    with httpx.Client(base_url=BASE, timeout=5) as c:
        for method, path, payload, want_status, want_body in CASES:
            r = c.request(method, path, content=payload)
            label = f"{method} {path} {(payload or '')[:28]!r}"
            if r.status_code != want_status:
                failures.append(f"{label}: got {r.status_code}, expected {want_status}")
                continue
            if want_status == 422:
                if r.headers.get("content-type", "").split(";")[0] != "application/problem+json":
                    failures.append(f"{label}: 422 was not problem details")
                elif not r.json().get("errors"):
                    failures.append(f"{label}: 422 body has no 'errors'")
            if want_body:
                got = r.json()
                for k, v in want_body.items():
                    if got.get(k) != v:
                        failures.append(f"{label}: {k}={got.get(k)!r}, expected {v!r}")

        # A model response must not leak fields the output model does not declare.
        leaked = c.post("/users", content='{"name":"ada","age":36,"email":"a@b.c"}').json()
        if "email" in leaked:
            failures.append("response model leaked a field it does not declare")

    print(f"http checks ({len(CASES)} cases): {'PASS' if not failures else 'FAIL'}")
    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
