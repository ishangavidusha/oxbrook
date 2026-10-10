"""Acceptance checks for the scoreboard task, against four worker loops.

Watchers connect on separate connections, so they are spread across the loops,
and the posts arrive on others. An `asyncio.Queue` per watcher kept in a module
dict looks right on one loop and drops or crashes across them.
"""

import json
import threading
import time
import uuid

import httpx


def watch(base_url: str, path: str, want: int, got: list, ready: threading.Event,
          initial: dict) -> None:
    """Collect `want` updates. A first event repeating the score at connect time
    is allowed — sending the current state on connect is a reasonable extra —
    and skipped."""
    try:
        with httpx.Client(base_url=base_url, timeout=httpx.Timeout(10, read=6)) as own:
            with own.stream("GET", path, headers={"accept": "text/event-stream"}) as response:
                if response.status_code != 200:
                    got.append(("status", response.status_code))
                    ready.set()
                    return
                ready.set()
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        event = json.loads(line[5:].strip())
                        if not got and event == initial:
                            initial = None
                            continue
                        got.append((time.monotonic(), event))
                        if len([g for g in got if g[0] != "status"]) >= want:
                            return
    except Exception as exc:
        got.append(("error", f"{type(exc).__name__}: {exc}"))
        ready.set()


def check(c) -> None:
    game = f"g{uuid.uuid4().hex[:8]}"
    with c.serve(workers=4) as client:
        def basics():
            assert client.get(f"/games/{game}").status_code == 404, "an unposted game is not 404"
            for bad in ({"home": -1, "away": 0}, {"home": "x", "away": 0}, {"home": 1}):
                r = client.post(f"/games/{game}/score", json=bad)
                assert r.status_code == 422, f"{bad} answered {r.status_code}"
            r = client.post(f"/games/{game}/score", json={"home": 0, "away": 0})
            assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
            assert r.json() == {"game_id": game, "home": 0, "away": 0}, r.json()
            assert client.get(f"/games/{game}").json() == {"game_id": game, "home": 0, "away": 0}
        c.run("post, read, validation, 404", basics)

        def live():
            scores = [(1, 0), (1, 1), (2, 1)]
            watchers = []
            for _ in range(4):
                got: list = []
                ready = threading.Event()
                t = threading.Thread(target=watch, args=(
                    client.base_url, f"/games/{game}/live", len(scores), got, ready,
                    {"game_id": game, "home": 0, "away": 0}))
                t.start()
                watchers.append((t, got, ready))
            for _, _, ready in watchers:
                ready.wait(5)
            time.sleep(0.3)
            posted = []
            with httpx.Client(base_url=client.base_url, timeout=10) as poster:
                for home, away in scores:
                    posted.append(time.monotonic())
                    r = poster.post(f"/games/{game}/score", json={"home": home, "away": away})
                    assert r.status_code == 200, f"post answered {r.status_code}"
                    time.sleep(0.2)
            for t, _, _ in watchers:
                t.join(8)
            expected = [{"game_id": game, "home": h, "away": a} for h, a in scores]
            problems = []
            for n, (_, got, _) in enumerate(watchers):
                events = [g for g in got if not isinstance(g[0], str)]
                other = [g for g in got if isinstance(g[0], str)]
                if other:
                    problems.append(f"watcher {n}: {other[:2]}")
                received = [e[1] for e in events]
                if received != expected:
                    problems.append(f"watcher {n} received {received}")
                    continue
                late = [e[0] - p for e, p in zip(events, posted, strict=True) if e[0] - p > 1.0]
                if late:
                    problems.append(f"watcher {n} got an update {max(late):.1f}s late")
            assert not problems, "; ".join(problems)
        c.run("four watchers get every update in order", live)

        def latest():
            assert client.get(f"/games/{game}").json() == {"game_id": game, "home": 2, "away": 1}
        c.run("latest score after updates", latest)
