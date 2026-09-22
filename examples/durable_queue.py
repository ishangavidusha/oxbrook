"""A durable work queue: jobs survive a restart and are never silently lost.

Needs a Redis:

    make up
    python examples/durable_queue.py

Then submit work and watch it get processed:

    curl -X POST 127.0.0.1:8000/jobs -d '{"kind":"resize","payload":"cat.png"}'
    curl 127.0.0.1:8000/status

The interesting part is what happens when you kill the server with jobs in
flight. A job is only acknowledged after its handler finishes. Anything the
worker was holding when it died is handed back on the next start, so restarting
loses nothing. Compare with an in-memory topic, where the queue dies with the
process.
"""

import asyncio
import contextlib
import datetime

from oxbrook import App, Request
from pydantic import BaseModel, Field

REDIS = "redis://127.0.0.1:6399"
TOPIC = "jobs"
GROUP = "workers"

app = App(title="Durable Queue", version="0.1.0", redis_url=REDIS)

processed: list[dict] = []
_worker: asyncio.Task | None = None


class Job(BaseModel):
    kind: str = Field(min_length=1)
    payload: str = Field(min_length=1)


@app.post("/jobs")
async def submit(_: Request, body: Job):
    """Append a job. Returning means Redis has it, not just this process."""
    topic = app.topic(TOPIC, durable=True)
    await topic.emit({"kind": body.kind, "payload": body.payload})
    start_worker()
    return {"queued": True, "depth": await topic.backend.length(TOPIC)}


@app.get("/status")
async def status(_: Request):
    topic = app.topic(TOPIC, durable=True)
    start_worker()
    return {
        "stream_length": await topic.backend.length(TOPIC),
        "unacked": await topic.backend.pending(TOPIC, GROUP),
        "processed_this_run": len(processed),
        "recent": processed[-5:],
    }


@app.get("/history")
async def history(_: Request):
    """Everything still in the stream, oldest first. This is what replay means."""
    return {"messages": await app.topic(TOPIC, durable=True).history(count=20)}


async def work() -> None:
    """One member of the consumer group.

    Run several copies of this program and the group splits the work between
    them: each job goes to exactly one worker.
    """
    topic = app.topic(TOPIC, durable=True)
    # claim_after_ms takes over anything another worker has held too long,
    # which is what makes a crashed worker's jobs finish rather than stall.
    async with topic.consumer(GROUP, "worker-1", claim_after_ms=30_000) as consumer:
        async for message in consumer:
            job = message.data
            await asyncio.sleep(0.2)  # stand-in for real work
            processed.append(
                {
                    "id": message.id,
                    "kind": job.get("kind"),
                    "at": datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S"),
                }
            )
            # Acknowledged only now. Dying before this point means the job is
            # redelivered rather than lost.
            await message.ack()


def start_worker() -> None:
    """Start the worker on first use, since it needs a running loop."""
    global _worker
    if _worker is None or _worker.done():
        _worker = asyncio.get_running_loop().create_task(work())


@app.get("/")
async def index(_: Request):
    return {
        "post": "/jobs  {'kind': ..., 'payload': ...}",
        "then": ["/status", "/history"],
        "try": "kill the server mid-job and restart it; nothing is lost",
    }


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        app.run(port=8000)
