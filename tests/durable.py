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
import time
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


async def a_burst_of_publishes():
    """I-152: more commands in flight on one loop than the pool has
    connections. redis-py's pool raised past a hundred, so most of a burst of
    publishes failed; they wait for a connection now."""
    backend = RedisBackend(URL, prefix=PREFIX)
    results = await asyncio.gather(*(backend.publish("burst", {"i": i}) for i in range(1000)),
                                   return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    check(not errors, f"{len(errors)} of 1000 concurrent publishes failed: {errors[:1]!r}")
    check(await backend.client().xlen(backend.key("burst")) == 1000,
          "a burst of 1000 publishes did not all land")
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


async def resuming():
    """`subscribe(since=...)` replays what came after a position, then follows
    live, as Messages carrying their ids."""
    from oxbrook import Message
    from oxbrook._streams import Topic

    backend = RedisBackend(URL, prefix=PREFIX)
    topic = Topic("resume", backend=backend)
    for i in range(5):
        await topic.emit({"n": i})
    ids = [ident for ident, _ in await topic.history(count=10)]

    sub = topic.subscribe(since=ids[1])
    got = [await asyncio.wait_for(sub.__anext__(), 5) for _ in range(3)]
    check(all(isinstance(m, Message) for m in got), f"since= yielded {got}")
    check([m.data["n"] for m in got] == [2, 3, 4], f"replay after #1 gave {got}")
    check([m.id for m in got] == ids[2:], "replayed ids are not the stream's")

    await topic.emit({"n": 5})
    live = await asyncio.wait_for(sub.__anext__(), 5)
    check(live.data == {"n": 5} and live.id not in ids, f"live after replay gave {live}")
    await asyncio.sleep(0.3)
    check(sub.pending == 0, f"{sub.pending} duplicate(s) after the replay met live delivery")
    sub.close()

    everything = topic.subscribe(since="0")
    first = await asyncio.wait_for(everything.__anext__(), 5)
    check(first.data == {"n": 0}, f'since="0" started at {first}')
    everything.close()

    fresh = topic.subscribe(since="$")
    await topic.emit({"n": 6})
    only = await asyncio.wait_for(fresh.__anext__(), 5)
    check(isinstance(only, Message) and only.data == {"n": 6}, f'since="$" gave {only}')
    fresh.close()

    # A client inventing a Last-Event-ID gets new messages, not a failure.
    invented = topic.subscribe(since="not-an-id")
    await topic.emit({"n": 7})
    got = await asyncio.wait_for(invented.__anext__(), 5)
    check(got.data == {"n": 7}, f"a malformed since gave {got}")
    invented.close()

    try:
        topic.subscribe(since=0)
        failures.append("a non-string since was accepted")
    except TypeError:
        pass
    try:
        Topic("plain").subscribe(since="0")
        failures.append("since= was accepted on an in-memory topic")
    except RuntimeError:
        pass

    topic.close()
    await backend.close()


async def resuming_under_load():
    """Nothing lost or repeated where the replay hands over to live delivery,
    with another node and this one both publishing throughout."""
    from oxbrook._streams import Topic

    listener = RedisBackend(URL, prefix=PREFIX)
    elsewhere = RedisBackend(URL, prefix=PREFIX)
    topic = Topic("handover", backend=listener)
    total, start = 400, 50

    async def produce():
        for i in range(total):
            if i % 2:
                await elsewhere.publish("handover", {"n": i})
            else:
                await topic.emit({"n": i})
            if i % 25 == 0:
                await asyncio.sleep(0)

    producer = asyncio.create_task(produce())
    while await listener.length("handover") <= start + 100:
        await asyncio.sleep(0.005)
    after = (await topic.history(count=start + 1))[start][0]
    sub = topic.subscribe(since=after)
    seen = []
    try:
        while len(seen) < total - start - 1:
            seen.append((await asyncio.wait_for(sub.__anext__(), 10)).data["n"])
    except TimeoutError:
        pass
    await producer
    expected = list(range(start + 1, total))
    wrong = next((a for a, b in zip(seen, expected, strict=False) if a != b), None)
    check(seen == expected,
          f"handover gave {len(seen)} messages; first gap or repeat near {wrong}")
    # What the reader gets next is the test: nothing. The tail delivered live
    # copies of entries the replay was still reading, and the reader may well
    # have its last message before the replay has found the end, so copies can
    # be buffered at this point; they must never come out.
    try:
        extra = await asyncio.wait_for(sub.__anext__(), 1.0)
        failures.append(f"a message repeated after the handover: {extra}")
    except TimeoutError:
        pass
    check(sub.pending == 0,
          f"{sub.pending} copies still counted as pending after the handover")
    topic.close()
    await listener.close()
    await elsewhere.close()


def resuming_over_sse():
    """The browser's side: ids on the wire, and Last-Event-ID resuming."""
    from oxbrook import SSE, Request
    from oxbrook.testing import TestClient

    app = App(redis_url=URL)
    app._backend = RedisBackend(URL, prefix=PREFIX)

    @app.get("/feed")
    async def feed(request: Request):
        since = request.headers.get("last-event-id", "$")
        return SSE(app.topic("resume-sse", durable=True).subscribe(since=since), ping=None)

    async def publish(n: int) -> str:
        backend = RedisBackend(URL, prefix=PREFIX)
        try:
            return await backend.publish("resume-sse", {"n": n})
        finally:
            await backend.close()

    def events(lines, count):
        out, current = [], {}
        for line in lines:
            if not line:
                if current:
                    out.append(current)
                    current = {}
                    if len(out) == count:
                        return out
                continue
            name, _, value = line.partition(": ")
            current[name] = value
        return out

    ids = [asyncio.run(publish(n)) for n in range(3)]
    with TestClient(app) as client:
        with client.stream("GET", "/feed", headers={"last-event-id": ids[0]},
                           timeout=10) as r:
            lines = r.iter_lines()
            got = events(lines, 2)
            check([e.get("id") for e in got] == ids[1:],
                  f"resumed events carried ids {[e.get('id') for e in got]}")
            check([e.get("data") for e in got] == ['{"n":1}', '{"n":2}'],
                  f"resumed events were {got}")
            later = asyncio.run(publish(3))
            got = events(lines, 1)
            check(got and got[0].get("id") == later, f"the live event after it was {got}")

        with client.stream("GET", "/feed", timeout=10) as r:
            later = asyncio.run(publish(4))
            got = events(r.iter_lines(), 1)
            check(got and got[0] == {"id": later, "data": '{"n":4}'},
                  f"a first connection got {got}")
    # The tail ran on a worker loop that has now closed. Left pending, it was
    # destroyed with the loop, mid-read on its Redis connection.
    check(app.topic("resume-sse", durable=True)._tail is None,
          "the topic's tail outlived the server that started it")
    asyncio.run(app._backend.close())


def tickets_across_processes():
    """A WebSocket ticket issued by one process opens a socket on another, once."""
    import hashlib
    import json

    import httpx
    from oxbrook import Depends, HTTPError, Request
    from oxbrook.auth import Principal, Tickets, principal
    from oxbrook.testing import TestClient

    script = textwrap.dedent(f"""
        import asyncio
        from oxbrook.auth import Principal, Tickets

        async def main():
            who = Principal(subject="p1", scheme="keys", scopes={{"read"}},
                            roles={{"member"}}, claims={{"tid": "t", "n": 3}})
            print(await Tickets(redis_url={URL!r}).issue(who))
            print(await Tickets(redis_url={URL!r}, ttl=0.2).issue(who))

        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        failures.append(f"ticket subprocess failed: {result.stderr[-400:]}")
        return
    ticket, short = result.stdout.split()

    tickets = Tickets(redis_url=URL)
    app = App()

    @app.websocket("/ws", auth=tickets)
    async def ws(_: Request, sock, who=Depends(principal)):
        await sock.send(json.dumps({
            "subject": who.subject, "scheme": who.scheme, "scopes": sorted(who.scopes),
            "roles": sorted(who.roles), "claims": dict(who.claims),
        }))

    upgrade = {
        "connection": "upgrade", "upgrade": "websocket", "sec-websocket-version": "13",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
    }
    with TestClient(app) as c:
        async def receive(path):
            async with c.websocket(path) as sock:
                return json.loads(await asyncio.wait_for(sock.recv(), 5))

        got = asyncio.run(receive(f"/ws?ticket={ticket}"))
        check(got == {"subject": "p1", "scheme": "keys", "scopes": ["read"],
                      "roles": ["member"], "claims": {"tid": "t", "n": 3}},
              f"a ticket from another process opened a socket as {got}")
        r = httpx.get(f"{c.base_url}/ws?ticket={ticket}", headers=upgrade)
        check(r.status_code == 401, f"a used ticket answered {r.status_code}")
        time.sleep(0.3)
        r = httpx.get(f"{c.base_url}/ws?ticket={short}", headers=upgrade)
        check(r.status_code == 401, f"an expired ticket answered {r.status_code}")

    class Upgrade:
        def __init__(self, ticket):
            self.query = f"ticket={ticket}"

        def header(self, name):
            return "websocket" if name == "upgrade" else None

    async def race():
        # One process per redeemer: each its own Tickets, so its own connection.
        ticket = await Tickets(redis_url=URL).issue(Principal(subject="once"))
        redeemers = [Tickets(redis_url=URL) for _ in range(8)]

        async def attempt(t):
            try:
                return await t.authenticate(Upgrade(ticket))
            except HTTPError as exc:
                return exc.status

        got = await asyncio.gather(*(attempt(t) for t in redeemers))
        for t in redeemers:
            await t._redis.close()
        return ticket, got

    ticket, got = asyncio.run(race())
    opened = [g for g in got if isinstance(g, Principal)]
    check(len(opened) == 1 and got.count(401) == 7,
          f"eight processes offered one ticket at once: {got}")

    async def keys(ticket):
        client = tickets._redis.client()
        hashed = hashlib.sha256(ticket.encode()).hexdigest()
        try:
            return (await client.exists(f"oxbrook:ticket:{ticket}"),
                    await client.exists(f"oxbrook:ticket:{hashed}"))
        finally:
            await tickets._redis.close()

    ticket = asyncio.run(tickets.issue(Principal(subject="k")))
    check(asyncio.run(keys(ticket)) == (0, 1),
          "a ticket is stored under its own value rather than a hash of it")

    try:
        asyncio.run(tickets.issue(Principal(subject="u", user=object())))
        failures.append("a principal with a user was issued a ticket kept in Redis")
    except TypeError:
        pass

    gone = Tickets(redis_url="redis://127.0.0.1:1")
    try:
        asyncio.run(gone.issue(Principal(subject="x")))
        failures.append("issuing a ticket with Redis unreachable succeeded")
    except HTTPError as exc:
        check(exc.status == 503, f"issuing with Redis unreachable raised {exc.status}")
    try:
        asyncio.run(gone.authenticate(Upgrade("anything")))
        failures.append("redeeming a ticket with Redis unreachable succeeded")
    except HTTPError as exc:
        check(exc.status == 503, f"redeeming with Redis unreachable raised {exc.status}")


async def main_async():
    for step in (persistence_and_history, cross_node, separate_process, a_burst_of_publishes,
                 at_least_once, competing_consumers, claiming_from_a_dead_consumer,
                 survives_a_dropped_connection, refusals, trimming, resuming,
                 resuming_under_load):
        try:
            await asyncio.wait_for(step(), 60)
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR")
    for step in (resuming_over_sse, tickets_across_processes):
        try:
            await asyncio.to_thread(step)
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
