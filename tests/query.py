#!/usr/bin/env python3
"""Query parameter binding: types, defaults, optionality, and error shapes."""
import datetime
import sys
import threading
import uuid

import httpx
from oxbrook import App, Request

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"

app = App()


@app.get("/search")
async def search(_: Request, q: str, limit: int = 10, exact: bool = False):
    return {"q": q, "limit": limit, "exact": exact,
            "types": [type(q).__name__, type(limit).__name__, type(exact).__name__]}


@app.get("/maybe")
async def maybe(_: Request, cursor: str | None):
    return {"cursor": cursor, "is_none": cursor is None}


@app.get("/mixed/{group}")
async def mixed(_: Request, group: str, page: int = 1):
    return {"group": group, "page": page}


@app.get("/typed")
async def typed(
    _: Request,
    ident: uuid.UUID,
    day: datetime.date,
    at: datetime.datetime | None = None,
):
    return {
        "ident": str(ident),
        "ident_type": type(ident).__name__,
        "day": day.isoformat(),
        "day_type": type(day).__name__,
        "at": at.isoformat() if at else None,
    }


@app.get("/many")
# A list default is how an optional repeated parameter is declared, which is
# the thing under test here.
async def many(_: Request, tag: list[str], n: list[int] = []):  # noqa: B006
    return {"tag": tag, "n": n, "n_types": sorted({type(v).__name__ for v in n})}


@app.get("/ratio")
async def ratio(_: Request, value: float = 0.5):
    return {"value": value}


CASES = [
    ("/search?q=abc", 200, {"q": "abc", "limit": 10, "exact": False}),
    ("/search?q=abc&limit=5", 200, {"q": "abc", "limit": 5}),
    ("/search?q=abc&exact=true", 200, {"exact": True}),
    ("/search?q=abc&exact=yes", 200, {"exact": True}),
    ("/search?q=abc&exact=off", 200, {"exact": False}),
    ("/search?q=a%20b", 200, {"q": "a b"}),
    ("/search?q=a+b", 200, {"q": "a b"}),
    ("/search?q=abc&limit=5&limit=9", 200, {"limit": 5}),
    ("/search", 422, None),
    ("/search?q=abc&limit=x", 422, None),
    ("/search?q=abc&exact=maybe", 422, None),
    ("/maybe", 200, {"cursor": None, "is_none": True}),
    ("/maybe?cursor=abc", 200, {"cursor": "abc", "is_none": False}),
    ("/mixed/alpha", 200, {"group": "alpha", "page": 1}),
    ("/mixed/alpha?page=3", 200, {"group": "alpha", "page": 3}),
    ("/ratio", 200, {"value": 0.5}),
    ("/ratio?value=2.25", 200, {"value": 2.25}),
    ("/ratio?value=nope", 422, None),
    # UUID, date and datetime are validated in Rust, so a bad one never
    # reaches a handler.
    ("/typed?ident=f47ac10b-58cc-4372-a567-0e02b2c3d479&day=2026-09-06", 200,
     {"ident_type": "UUID", "day_type": "date", "day": "2026-09-06"}),
    ("/typed?ident=nope&day=2026-09-06", 422, None),
    ("/typed?ident=f47ac10b-58cc-4372-a567-0e02b2c3d479&day=not-a-date", 422, None),
    # A real calendar check, not a shape check.
    ("/typed?ident=f47ac10b-58cc-4372-a567-0e02b2c3d479&day=2026-02-30", 422, None),
    ("/typed?ident=f47ac10b-58cc-4372-a567-0e02b2c3d479&day=2026-09-06"
     "&at=2026-09-06T14:30:00Z", 200, {"at": "2026-09-06T14:30:00+00:00"}),
    # Repeated keys become a list rather than the first value winning.
    ("/many?tag=a&tag=b&n=1&n=2", 200, {"tag": ["a", "b"], "n": [1, 2]}),
    ("/many?tag=solo", 200, {"tag": ["solo"], "n": []}),
    ("/many", 422, None),
    ("/many?tag=a&n=x", 422, None),
]


def registration_checks() -> list[str]:
    failures = []

    def unannotated():
        bad = App()

        @bad.get("/x")
        async def h(_: Request, q):
            return {}

    def unsupported():
        bad = App()

        @bad.get("/x")
        async def h(_: Request, q: dict):
            return {}

    for label, fn in [("unannotated query param", unannotated),
                      ("unsupported query type", unsupported)]:
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
            httpx.get(f"{BASE}/ratio", timeout=0.3)
            break
        except Exception:
            threading.Event().wait(0.1)

    with httpx.Client(base_url=BASE, timeout=5) as c:
        for path, want_status, want in CASES:
            r = c.get(path)
            label = f"GET {path}"
            if r.status_code != want_status:
                failures.append(f"{label}: got {r.status_code}, expected {want_status}")
                continue
            if want_status == 422:
                body = r.json()
                if not body.get("errors"):
                    failures.append(f"{label}: 422 body has no 'errors'")
                elif body["errors"][0]["loc"][0] != "query":
                    failures.append(f"{label}: 422 loc is {body['errors'][0]['loc']!r}")
            if want:
                got = r.json()
                for k, v in want.items():
                    if got.get(k) != v:
                        failures.append(f"{label}: {k}={got.get(k)!r}, expected {v!r}")

        types_ok = c.get("/search?q=a&limit=2&exact=1").json()["types"]
        if types_ok != ["str", "int", "bool"]:
            failures.append(f"coerced types were {types_ok}")

    print(f"http checks ({len(CASES)} cases): {'PASS' if not failures else 'FAIL'}")
    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
