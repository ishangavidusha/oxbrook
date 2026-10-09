"""Server metrics, in the Prometheus text format.

    app = App(metrics=Metrics())

Served by the server itself at `/metrics`, without a worker loop: the
numbers that matter most when the loops are overwhelmed are the ones a
handler could not report.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Metrics:
    """Expose the server's own metrics for Prometheus to scrape.

    `path` is where they are served. The endpoint is public, not in the
    OpenAPI document and never an agent tool. It reveals route templates,
    request counts and load, which is usually fine on a private network and
    worth keeping off a public one: expose the path only internally, as you
    would a database port.

    What is measured, all from the Rust side:

    - `oxbrook_requests_total` and `oxbrook_request_duration_seconds`, by
      route template (`/users/{id}`, never the raw path) and status
    - `oxbrook_queue_wait_seconds`: how long requests waited for a worker loop
    - `oxbrook_loop_requests`, `oxbrook_loop_queued` and
      `oxbrook_loop_wake_pending_seconds`, per worker loop
    - `oxbrook_connections` against `oxbrook_connections_limit`
    - `oxbrook_requests_shed_total` (`503` at `max_concurrency`) and
      `oxbrook_request_timeouts_total` (`504` at `request_timeout`)

    The application's own metrics belong to a client library such as
    `prometheus_client`, served on a route of their own.
    """

    path: str = "/metrics"

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ValueError(f"path must start with '/', not {self.path!r}")

    def as_spec(self) -> tuple:
        """The tuple the Rust server takes."""
        return (self.path,)
