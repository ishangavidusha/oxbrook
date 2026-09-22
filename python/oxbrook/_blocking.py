"""Running a blocking handler without stalling its worker loop.

A worker loop is one thread serving many requests at once, and that only works
because handlers `await`. Code that takes time *without* awaiting — a sync
database driver, `requests`, `boto3`, Pillow — freezes the loop it runs on, and
with it every other request that loop was serving. Measured on a four-loop
server: eight concurrent handlers sleeping a second each made an unrelated
`GET` wait 1829 ms.

So blocking work runs on threads instead, and the loop stays free to interleave
everything else. A route asks for that explicitly with `blocking=True`, which
is what lets it be a plain `def`:

    @app.get("/report", blocking=True)
    def report(request):
        return connection.execute("select ...").fetchall()

**Why the pool is shared and bounded.** `asyncio.to_thread` uses the running
loop's default executor, and each loop makes its own — `min(32, cpu + 4)`
threads apiece, so eight worker loops quietly own eight of them. That is the
same multiplication that turns a ten-connection database pool into eighty. One
pool for the process is the same familiar number without it.

Bounded, because a ceiling is the point. Handing blocking work to an unbounded
pool moves where the server falls over rather than stopping it: when the pool
is full, blocking calls queue and the loops keep serving everything else, which
is the trade being made.
"""

import asyncio
import functools
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def default_threads() -> int:
    """What CPython would give a single loop, given to all of them together."""
    return min(32, (os.cpu_count() or 1) + 4)


class Pool:
    """The process's threadpool for blocking handlers.

    Created on first use: a server with no blocking route never starts a
    thread, and most do not have one.
    """

    __slots__ = ("_executor", "_lock", "threads")

    def __init__(self, threads: int | None = None) -> None:
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self.threads = default_threads() if threads is None else threads
        if not isinstance(self.threads, int) or self.threads < 1:
            raise ValueError(
                f"blocking_threads must be a positive integer, got {threads!r}"
            )

    @property
    def started(self) -> bool:
        return self._executor is not None

    def _executor_now(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.threads, thread_name_prefix="oxbrook-blocking"
                )
            return self._executor

    async def run(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        """Run `fn` on a pool thread and await the result.

        The handler awaits, so its worker loop is free the whole time it runs —
        which is the entire point. Cancelling the await does not stop the
        thread: a blocking call cannot be interrupted, so a client that leaves
        frees the loop but not the thread.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor_now(), functools.partial(fn, *args, **kwargs)
        )

    def close(self) -> None:
        """Explicit, never left to the collector — invariant 7.

        Queued work is dropped and running threads are not waited for. A thread
        inside a blocking call cannot be interrupted, so waiting would turn one
        stuck request into a stuck shutdown.
        """
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


class Slot:
    """Where a blocking route finds its pool.

    A `Router` builds routes before it is included anywhere, so the pool cannot
    be baked in at build time; the app fills this when the route reaches it.
    Per app rather than module state, because two apps in one process must not
    share a sizing decision neither of them made.
    """

    __slots__ = ("pool",)

    def __init__(self) -> None:
        self.pool: Pool | None = None

    async def run(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        pool = self.pool
        if pool is None:
            raise RuntimeError(
                "a blocking route has no threadpool, which means it was never "
                "registered on an App. Include its router with app.include(...)"
            )
        return await pool.run(fn, *args, **kwargs)
