#!/usr/bin/env python3
"""OpenAPI generation and the routes that serve it."""
import json
import sys
import threading

import httpx
from oxbrook import App, Request
from pydantic import BaseModel, Field

PORT = 8801
BASE = f"http://127.0.0.1:{PORT}"


class Tag(BaseModel):
    label: str


class UserIn(BaseModel):
    name: str = Field(min_length=1)
    age: int
    tag: Tag | None = None


class UserOut(BaseModel):
    id: int
    name: str


app = App(title="Demo API", version="2.0.0", description="A demo.")


@app.get("/users/{user_id}")
async def get_user(_: Request, user_id: int, verbose: bool = False) -> UserOut:
    """Fetch a user.

    The longer description.
    """
    return UserOut(id=user_id, name="x")


@app.post("/users")
async def create_user(_: Request, body: UserIn) -> UserOut:
    return UserOut(id=1, name=body.name)


@app.get("/files/{*rest}")
async def get_file(_: Request, rest: str):
    return {"rest": rest}


@app.get("/search")
async def search(_: Request, q: str, cursor: str | None = None):
    return {}


@app.get("/plain")
async def plain(_: Request):
    return {"ok": True}


def document_checks(doc: dict) -> list[str]:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    try:
        from openapi_spec_validator import validate

        validate(doc)
    except ImportError:
        failures.append("openapi-spec-validator not installed, cannot validate")
    except Exception as e:
        failures.append(f"document failed OpenAPI 3.1 validation: {e}")

    check(doc["openapi"] == "3.1.0", f"openapi version is {doc['openapi']}")
    check(doc["info"]["title"] == "Demo API", "title not carried through")
    check(doc["info"]["version"] == "2.0.0", "version not carried through")
    check(doc["info"].get("description") == "A demo.", "description not carried through")

    paths = doc["paths"]
    check("/files/{rest}" in paths, f"wildcard path not converted: {list(paths)}")
    check("/users/{user_id}" in paths, "path parameter route missing")

    get_user_op = paths["/users/{user_id}"]["get"]
    check(get_user_op["summary"] == "Fetch a user.", "summary not taken from docstring")
    check("longer description" in get_user_op["description"].lower(),
          "description not taken from docstring")
    check(get_user_op["operationId"] == "get_user", "operationId wrong")

    by_name = {p["name"]: p for p in get_user_op["parameters"]}
    check(by_name["user_id"]["in"] == "path", "user_id not marked as a path parameter")
    check(by_name["user_id"]["required"] is True, "path parameter not required")
    check(by_name["user_id"]["schema"]["type"] == "integer", "path parameter type wrong")
    check(by_name["verbose"]["in"] == "query", "verbose not marked as a query parameter")
    check(by_name["verbose"]["required"] is False, "defaulted query param marked required")
    check(by_name["verbose"]["schema"].get("default") is False, "default not recorded")

    search_params = {p["name"]: p for p in paths["/search"]["get"]["parameters"]}
    check(search_params["q"]["required"] is True, "query param without default not required")
    check("anyOf" in search_params["cursor"]["schema"],
          "optional query param not rendered as nullable")

    post_op = paths["/users"]["post"]
    ref = post_op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    check(ref.endswith("/UserIn"), f"request body ref is {ref}")
    ok_ref = post_op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    check(ok_ref.endswith("/UserOut"), f"response ref is {ok_ref}")
    check("422" in post_op["responses"], "no 422 documented for a route with a body")

    schemas = doc["components"]["schemas"]
    check("Tag" in schemas, f"nested model not hoisted into components: {sorted(schemas)}")
    check("ValidationProblem" in schemas and "Problem" in schemas,
          "validation error schema missing")
    check(
        "application/problem+json" in post_op["responses"]["422"]["content"],
        "the documented 422 is not problem details",
    )

    plain_op = paths["/plain"]["get"]
    check("422" not in plain_op["responses"],
          "route with no inputs should not document a 422")

    check("/openapi.json" not in paths, "the openapi route documented itself")
    check("/docs" not in paths, "the docs route appears in the document")
    return failures


def main() -> None:
    failures = document_checks(app.openapi())
    print(f"document checks: {'PASS' if not failures else 'FAIL'}")

    off = App(openapi_url=None, docs_url=None)

    @off.get("/x")
    async def x(_: Request):
        return {}

    threading.Thread(target=lambda: app.run(port=PORT), daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/plain", timeout=0.3)
            break
        except Exception:
            threading.Event().wait(0.1)

    served = []
    with httpx.Client(base_url=BASE, timeout=5) as c:
        r = c.get("/openapi.json")
        if r.status_code != 200:
            served.append(f"/openapi.json returned {r.status_code}")
        elif r.headers.get("content-type", "").split(";")[0] != "application/json":
            served.append(f"/openapi.json content-type is {r.headers.get('content-type')}")
        elif json.loads(r.content)["info"]["title"] != "Demo API":
            served.append("/openapi.json body is not the document")

        d = c.get("/docs")
        if d.status_code != 200:
            served.append(f"/docs returned {d.status_code}")
        elif "text/html" not in d.headers.get("content-type", ""):
            served.append(f"/docs content-type is {d.headers.get('content-type')}")
        elif "/openapi.json" not in d.text:
            served.append("/docs does not point at the schema")

    print(f"served routes:   {'PASS' if not served else 'FAIL'}")
    failures += served

    disabled = {(r.method, r.path) for r in off.routes}
    off._register_docs()
    if {(r.method, r.path) for r in off.routes} != disabled:
        failures.append("openapi_url=None still registered routes")

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
