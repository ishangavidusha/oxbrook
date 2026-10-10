import weatherlib
from oxbrook import App, HTTPError, Request

app = App()


@app.get("/forecast/{city}", blocking=True)
def forecast(_: Request, city: str):
    try:
        found = weatherlib.get_forecast(city)
    except weatherlib.UnknownCity:
        raise HTTPError(404, f"No forecast for {city}.") from None
    return {**found, "source": "weatherlib"}


@app.get("/ping")
async def ping(_: Request):
    return {"ok": True}
