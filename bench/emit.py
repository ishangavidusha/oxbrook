#!/usr/bin/env python3
"""What one `topic.emit()` costs with N subscribers spread over worker loops.

    bench/emit.py [subscribers] [loops] [emits] [--profile]


The emitter runs on the main thread's loop (loop 0); N subscribers go
round-robin over that loop and L-1 others, each its own thread as worker
loops are, each reading as fast as it can. Reports µs per emit, and with
--profile a cProfile of the emitting thread.
"""
import asyncio
import cProfile
import os
import pstats
import sys
import threading
import time

from oxbrook._streams import Topic

args = [a for a in sys.argv[1:] if not a.startswith("--")]
N = int(args[0]) if len(args) > 0 else 10
LOOPS = int(args[1]) if len(args) > 1 else 4
EMITS = int(args[2]) if len(args) > 2 else 20000
PROFILE = "--profile" in sys.argv


def start_loop():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop


async def main():
    print("load", [round(x, 2) for x in os.getloadavg()], "gil", sys._is_gil_enabled())
    here = asyncio.get_running_loop()
    loops = [here] + [start_loop() for _ in range(LOOPS - 1)]
    topic = Topic("bench", maxsize=100_000)
    received = [0] * N
    done = threading.Event()
    tasks = []

    async def consume(i, sub):
        async for _ in sub:
            received[i] += 1
            if received[i] == EMITS:
                if all(r >= EMITS for r in received):
                    done.set()
                return

    async def subscribe(i):
        sub = topic.subscribe()
        tasks.append(asyncio.get_running_loop().create_task(consume(i, sub)))

    for i in range(N):
        loop = loops[i % LOOPS]
        if loop is here:
            await subscribe(i)
        else:
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(subscribe(i), loop))

    prof = cProfile.Profile() if PROFILE else None
    t = time.perf_counter()
    if prof:
        prof.enable()
    for i in range(EMITS):
        await topic.emit(i)
        if i % 64 == 0:
            await asyncio.sleep(0)  # let this loop's own subscribers run
    emitted = time.perf_counter() - t
    if prof:
        prof.disable()
    while not done.is_set() and time.perf_counter() - t < 60:
        await asyncio.sleep(0.01)
    total = time.perf_counter() - t
    print(f"N={N} loops={LOOPS}: {emitted / EMITS * 1e6:.1f} us per emit, "
          f"{N * EMITS / total:,.0f} deliveries/s, delivered {sum(received)}/{N * EMITS}")
    if prof:
        pstats.Stats(prof).sort_stats("tottime").print_stats(8)


asyncio.run(main())
