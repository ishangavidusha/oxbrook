"""Form and upload routes for bench/forms.py: the same bodies read each way."""
import argparse

from oxbrook import App, BodyStream, Request

app = App()


@app.post("/raw")
async def raw(request: Request):
    return {"bytes": len(request.body)}


@app.post("/form")
async def form(request: Request):
    return {"parts": len(request.form())}


@app.post("/stream")
async def stream(_: Request, body: BodyStream):
    size = 0
    async for chunk in body:
        size += len(chunk)
    return {"bytes": size}


@app.post("/parts")
async def parts(_: Request, body: BodyStream):
    count = size = 0
    async for part in body.parts():
        count += 1
        async for chunk in part:
            size += len(chunk)
    return {"parts": count, "bytes": size}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8774)
    args = ap.parse_args()
    app.run(port=args.port, max_body=64 * 1024 * 1024)
