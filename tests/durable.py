#!/usr/bin/env python3
"""Durable topics: persistence, cross-process fan-out, and at-least-once.

Needs a Redis on OXBROOK_TEST_REDIS (default redis://127.0.0.1:6399).

This is the only suite covering milestone 4, so a run that skips it has proved
nothing about durable topics. With OXBROOK_REQUIRE_REDIS set an unreachable
Redis is a failure rather than a SKIP; `make verify` and CI both set it.

    python tests/durable.py --check    report reachability and stop

`--check` is what the runners call before the suites, so a missing Redis is
reported in the first second rather than fifteen suites in.
"""
import asyncio
import os
import subprocess
import sys
import textwrap
import uuid

from oxbrook import App
from oxbrook._redis import HAVE_REDIS, Consumer, RedisBackend

URL = os.environ.get("OXBROOK_TEST_REDIS", "redis://127.0.0.1:6399")
PREFIX = f"oxbrooktest:{uuid.uuid4().hex[:8]}:"

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


async def reachable() -> bool:
    if not HAVE_REDIS:
        return False
    try:
        backend = RedisBackend(URL, prefix=PREFIX)
        await asyncio.wait_for(backend.client().ping(), 2)
        await backend.close()
        return True
    except Exception:
        return False


async def persistence_and_history():
    app = App(redis_url=URL)
    app._backend = RedisBackend(URL, prefix=PREFIX)
    topic = app.topic("orders", durable=True)

    sub = topic.subscribe()
    for i in range(3):
        await topic.emit({"n": i})

    # The publisher delivers locally itself, so this must not wait on the tail.
    got = [await asyncio.wait_for(sub.__anext__(), 5) for _ in range(3)]
    check(got == [{"n": 0}, {"n": 1}, {"n": 2}], f"local delivery gave {got}")

    history = await topic.history(count=10)
    check(len(history) == 3, f"history has {len(history)} entries, expected 3")
    check([v for _, v in history] == got, "history does not match what was emitted")

    # Exactly one copy: the tail must skip what this node published.
    await asyncio.sleep(0.5)
    check(sub.pending == 0, f"publisher's own subscriber saw {sub.pending} duplicate(s)")
    topic.close()
    await app._backend.close()


async def cross_node():
    """Two backends with different node ids, which is what two processes are."""
    publisher = RedisBackend(URL, prefix=PREFIX)
    listener = RedisBackend(URL, prefix=PREFIX)

    from oxbrook._streams import Topic

    remote = Topic("bus", backend=listener)
    sub = remote.subscribe()
    await asyncio.sleep(0.3)  # let the tail reach the stream head

    await publisher.publish("bus", {"from": "elsewhere"})
    got = await asyncio.wait_for(sub.__anext__(), 5)
    check(got == {"from": "elsewhere"}, f"cross-node delivery gave {got}")

    remote.close()
    await publisher.close()
    await listener.close()


async def separate_process():
    """The actual claim: another OS process publishes, we receive."""
    from oxbrook._streams import Topic

    backend = RedisBackend(URL, prefix=PREFIX)
    topic = Topic("interproc", backend=backend)
    sub = topic.subscribe()
    await asyncio.sleep(0.3)

    script = textwrap.dedent(f"""
        import asyncio, sys
        sys.path.insert(0, {os.getcwd()!r} + "/python")
        from oxbrook._redis import RedisBackend

        async def main():
            b = RedisBackend({URL!r}, prefix={PREFIX!r})
            await b.publish("interproc", {{"pid": "child"}})
            await b.close()

        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        failures.append(f"publisher subprocess failed: {result.stderr[-400:]}")
    else:
        try:
            got = await asyncio.wait_for(sub.__anext__(), 10)
            check(got == {"pid": "child"}, f"received {got} from the child process")
        except TimeoutError:
            failures.append("nothing arrived from a separate publishing process")

    topic.close()
    await backend.close()


async def at_least_once():
    """The milestone's exit criterion: killed mid-work, resumes losing nothing."""
    backend = RedisBackend(URL, prefix=PREFIX)
    for i in range(5):
        await backend.publish("jobs", {"n": i})

    first = await Consumer(
        backend, "jobs", "g", "w1", count=3, block_ms=200, claim_after_ms=None
    ).start()
    taken = [await first.__anext__() for _ in range(3)]
    check([m.data["n"] for m in taken] == [0, 1, 2], f"first pass got {taken}")
    for message in taken[:2]:
        await message.ack()
    check(await first.pending() == 1, "one message should still be pending")

    # "Crash": drop the consumer without acking, then start it again.
    restarted = await Consumer(
        backend, "jobs", "g", "w1", count=10, block_ms=200, claim_after_ms=None
    ).start()
    redelivered = await asyncio.wait_for(restarted.__anext__(), 5)
    check(redelivered.data["n"] == 2, f"redelivered {redelivered.data} instead of 2")
    await redelivered.ack()

    rest = [await asyncio.wait_for(restarted.__anext__(), 5) for _ in range(2)]
    check([m.data["n"] for m in rest] == [3, 4], f"remaining messages were {rest}")
    for message in rest:
        await message.ack()
    check(await restarted.pending() == 0, "nothing should be pending at the end")
    await backend.close()


async def competing_consumers():
    """Within a group each message goes to exactly one member."""
    backend = RedisBackend(URL, prefix=PREFIX)
    for i in range(10):
        await backend.publish("split", {"n": i})

    a = await Consumer(backend, "split", "g", "a", count=10, block_ms=300,
                       claim_after_ms=None).start()
    b = await Consumer(backend, "split", "g", "b", count=10, block_ms=300,
                       claim_after_ms=None).start()

    seen: list[int] = []
    for consumer in (a, b):
        for _ in range(10 - len(seen)):
            try:
                message = await asyncio.wait_for(consumer.__anext__(), 1)
            except TimeoutError:
                break
            seen.append(message.data["n"])
            await message.ack()
        if len(seen) == 10:
            break

    check(sorted(seen) == list(range(10)), f"group saw {sorted(seen)}")
    check(len(seen) == len(set(seen)), "a message was delivered to more than one member")
    await backend.close()


async def claiming_from_a_dead_consumer():
    backend = RedisBackend(URL, prefix=PREFIX)
    await backend.publish("claim", {"n": 1})

    dead = await Consumer(backend, "claim", "g", "dead", count=1, block_ms=200,
                          claim_after_ms=None).start()
    held = await dead.__anext__()
    check(held.data["n"] == 1, "setup: first consumer should receive the message")

    # It never acks. Another member claims anything idle for more than 100ms.
    await asyncio.sleep(0.4)
    rescuer = await Consumer(backend, "claim", "g", "rescuer", count=10,
                             block_ms=200, claim_after_ms=100).start()
    claimed = await asyncio.wait_for(rescuer.__anext__(), 5)
    check(claimed.data["n"] == 1, f"rescuer got {claimed.data}")
    await claimed.ack()
    check(await rescuer.pending() == 0, "claimed message was not acked away")
    await backend.close()


async def survives_a_dropped_connection():
    """The tail must reconnect, not die quietly.

    A tail that gave up on the first dropped connection would leave the process
    deaf to every other node with nothing to show for it: local delivery would
    still work, so it would look fine until a message failed to arrive.
    """
    from oxbrook._streams import Topic

    listener = RedisBackend(URL, prefix=PREFIX)
    publisher = RedisBackend(URL, prefix=PREFIX)
    topic = Topic("resilient", backend=listener)
    sub = topic.subscribe()
    await asyncio.sleep(0.4)

    await publisher.publish("resilient", {"n": 1})
    first = await asyncio.wait_for(sub.__anext__(), 5)
    check(first == {"n": 1}, f"setup: expected the first message, got {first}")

    # Cut every client connection out from under the tail.
    await publisher.client().execute_command("CLIENT", "KILL", "TYPE", "NORMAL")
    await asyncio.sleep(1.5)

    await publisher.publish("resilient", {"n": 2})
    try:
        second = await asyncio.wait_for(sub.__anext__(), 20)
        check(second == {"n": 2}, f"after reconnect got {second}")
    except TimeoutError:
        failures.append("tail did not reconnect after its connection was killed")

    topic.close()
    await listener.close()
    await publisher.close()


async def refusals():
    from oxbrook._streams import Topic

    backend = RedisBackend(URL, prefix=PREFIX)
    durable = Topic("refuse", backend=backend)
    try:
        durable.emit_nowait({"x": 1})
        failures.append("emit_nowait should be refused on a durable topic")
    except RuntimeError:
        pass
    durable.close()
    await backend.close()

    memory = Topic("plain")
    for label, call in [
        ("history", lambda: memory.history()),
        ("consumer", lambda: memory.consumer("g", "c")),
    ]:
        try:
            result = call()
            if asyncio.iscoroutine(result):
                await result
            failures.append(f"{label}() should be refused on a memory topic")
        except RuntimeError:
            pass
    check(memory.emit_nowait({"x": 1}) == 0, "emit_nowait broke on a memory topic")


async def trimming():
    backend = RedisBackend(URL, prefix=PREFIX, maxlen=None)
    for i in range(50):
        await backend.publish("trim", {"n": i})
    check(await backend.length("trim") == 50, "setup: 50 entries expected")
    await backend.trim("trim", 10)
    check(await backend.length("trim") == 10, f"after trim: {await backend.length('trim')}")
    await backend.close()


async def main_async():
    for step in (persistence_and_history, cross_node, separate_process, at_least_once,
                 competing_consumers, claiming_from_a_dead_consumer,
                 survives_a_dropped_connection, refusals, trimming):
        try:
            await asyncio.wait_for(step(), 60)
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR")


def main() -> None:
    # --check answers "can this suite run?" and stops, for a runner that wants
    # to know before it spends ten minutes on the other twenty-seven suites.
    check_only = "--check" in sys.argv[1:]

    if not asyncio.run(reachable()):
        why = "redis package not installed" if not HAVE_REDIS else f"no redis at {URL}"
        print(f"redis: unavailable ({why})")
        # A suite that passes without testing anything is worse than one that
        # fails, and this is the only cover milestone 4 has.
        if os.environ.get("OXBROOK_REQUIRE_REDIS"):
            print("start one with `make up`, or accept the gap with `make verify REDIS=`")
            print("\nRESULT: FAIL (redis is required and unreachable)")
            sys.exit(1)
        print("\nRESULT: SKIP")
        sys.exit(0)

    if check_only:
        print(f"redis: {URL}")
        return

    print(f"redis: {URL}  prefix: {PREFIX}")
    asyncio.run(main_async())

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
