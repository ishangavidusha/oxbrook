"""Acceptance checks for the forecast task, on one worker loop.

One loop makes the mistake visible: a blocking vendor call made directly in an
`async def` handler holds the loop for half a second, so eight concurrent
forecasts take four seconds and `/ping` waits behind them.
"""

import threading
import time

import httpx


def check(c) -> None:
    with c.serve(workers=1) as client:
        def forecast():
            r = client.get("/forecast/London")
            assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
            body = r.json()
            expected = {"city": "london", "temp_c": 11.5, "conditions": "rain",
                        "source": "weatherlib"}
            assert body == expected, f"answered {body}"
        c.run("forecast", forecast)

        def unknown():
            r = client.get("/forecast/atlantis")
            assert r.status_code == 404, f"{r.status_code} {r.text[:200]}"
        c.run("unknown city is 404", unknown)

        def ping():
            r = client.get("/ping")
            assert r.status_code == 200 and r.json() == {"ok": True}, r.text[:200]
        c.run("ping", ping)

        timings: dict = {}

        def under_load():
            codes: list[int] = []

            def one(city: str) -> None:
                with httpx.Client(base_url=client.base_url, timeout=15) as own:
                    codes.append(own.get(f"/forecast/{city}").status_code)

            cities = ["london", "lisbon", "oslo", "colombo", "tokyo", "london", "oslo", "tokyo"]
            threads = [threading.Thread(target=one, args=(city,)) for city in cities]
            started = time.monotonic()
            for t in threads:
                t.start()
            time.sleep(0.15)
            with httpx.Client(base_url=client.base_url, timeout=15) as own:
                ping_started = time.monotonic()
                own.get("/ping")
                timings["ping"] = time.monotonic() - ping_started
            for t in threads:
                t.join()
            timings["batch"] = time.monotonic() - started
            assert codes == [200] * 8, f"forecasts answered {codes}"
        c.run("eight forecasts at once", under_load)

        if "ping" in timings:
            c.record("ping stays fast under load", timings["ping"] < 0.25,
                     f"/ping took {timings['ping']:.2f}s while forecasts ran")
            c.record("forecasts run concurrently", timings["batch"] < 2.0,
                     f"eight forecasts took {timings['batch']:.2f}s (serial would be 4s)")
