"""Weather forecasts. A synchronous client: each call blocks while it waits."""

import time

_CITIES = {
    "london": (11.5, "rain"),
    "lisbon": (21.0, "sun"),
    "oslo": (3.5, "snow"),
    "colombo": (29.0, "showers"),
    "tokyo": (17.0, "cloud"),
}


class UnknownCity(Exception):
    """The vendor has no forecast for this city."""


def get_forecast(city: str) -> dict:
    """The forecast for `city`. Blocks for about half a second."""
    time.sleep(0.5)
    key = city.strip().lower()
    if key not in _CITIES:
        raise UnknownCity(city)
    temp, conditions = _CITIES[key]
    return {"city": key, "temp_c": temp, "conditions": conditions}
