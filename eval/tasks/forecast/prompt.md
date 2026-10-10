Build a small weather API on top of our vendor's Python client, which is
`weatherlib.py` in this directory. Do not change `weatherlib.py`; it is the
vendor's code and only its public function may be used:
`weatherlib.get_forecast(city)` returns a dict, and raises
`weatherlib.UnknownCity` for a city it does not know. Each call takes about
half a second.

- `GET /forecast/{city}` answers the forecast as JSON: the vendor's dict as it
  is, with one extra field, `"source": "weatherlib"`. An unknown city answers
  `404`.
- `GET /ping` answers `{"ok": true}`. Monitoring calls it every few seconds and
  it must stay fast even while forecasts are being fetched.

Expect many forecast requests at the same time.
