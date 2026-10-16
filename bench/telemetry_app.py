"""The app bench/telemetry.py measures: one small JSON reply, traced or not.

`--mode` picks what is behind it:

- `off`: no `telemetry=`, the server every app without tracing runs
- `api`: `Telemetry()` with no SDK configured, so every span is
  OpenTelemetry's non-recording one; the adapter's own cost
- `sdk`: the SDK with a batch processor and an exporter that discards,
  so everything up to the network is paid
- `sampled-out`: the SDK sampling nothing, the cost of a request a
  sampler turned away
"""
import argparse

from oxbrook import App, Request


class Discard:
    """An exporter that accepts every batch and sends it nowhere."""

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def build(mode: str) -> App:
    options = {}
    if mode != "off":
        from oxbrook import Telemetry

        provider = None
        if mode in ("sdk", "sampled-out"):
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON

            provider = TracerProvider(sampler=ALWAYS_OFF if mode == "sampled-out" else ALWAYS_ON)
            provider.add_span_processor(BatchSpanProcessor(Discard()))
        options["telemetry"] = Telemetry(tracer_provider=provider)
    app = App(openapi_url=None, docs_url=None, mcp_url=None, **options)

    @app.get("/items/{item_id}")
    async def item(_: Request, item_id: int):
        return {"id": item_id, "ok": True}

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--mode", choices=("off", "api", "sdk", "sampled-out"), default="off")
    args = ap.parse_args()
    build(args.mode).run(port=args.port, workers=args.workers)
