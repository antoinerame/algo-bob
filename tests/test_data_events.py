import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from app.events import Bus  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_publish_from_thread_and_order():
    async def main():
        bus = Bus()
        got = []

        async def reader():
            async for ev in bus.listen():
                got.append(ev)
                if len(got) == 3:
                    return
        task = asyncio.create_task(reader())
        await asyncio.sleep(0.05)
        th = threading.Thread(target=lambda: [bus.publish('bar', {'id': 'X', 't': i}) for i in range(3)])
        th.start()
        th.join()
        await asyncio.wait_for(task, 2)
        return got
    got = run(main())
    assert [e['data']['t'] for e in got] == [0, 1, 2] and got[0]['type'] == 'bar'


def test_tick_throttle_leading_and_trailing():
    async def main():
        bus = Bus(tick_interval=0.3)
        got = []

        async def reader():
            async for ev in bus.listen():
                got.append(ev)
        task = asyncio.create_task(reader())
        await asyncio.sleep(0.05)
        for i in range(50):  # burst for XAU, one tick for EUR
            bus.publish('tick', {'id': 'XAUUSD', 't': i, 'bid': i, 'ask': None, 'source': 'mt5'})
        bus.publish('tick', {'id': 'EURUSD', 't': 1, 'bid': 1, 'ask': None, 'source': 'mt5'})
        await asyncio.sleep(0.15)
        first = [e['data'] for e in got]
        await asyncio.sleep(0.4)
        task.cancel()
        return first, [e['data'] for e in got]
    first, allv = run(main())
    assert {d['id'] for d in first} == {'XAUUSD', 'EURUSD'} and len(first) == 2 and [d for d in first if d['id'] == 'XAUUSD'][0]['t'] == 0
    xau = [d['t'] for d in allv if d['id'] == 'XAUUSD']
    assert xau == [0, 49]  # leading tick, then the NEWEST tick of the burst once the window ends: <= 1 per window


def test_other_types_not_throttled():
    async def main():
        bus = Bus(tick_interval=10)
        got = []

        async def reader():
            async for ev in bus.listen():
                got.append(ev)
        task = asyncio.create_task(reader())
        await asyncio.sleep(0.05)
        for i in range(5):
            bus.publish('status', {'i': i})
        await asyncio.sleep(0.1)
        task.cancel()
        return got
    assert len(run(main())) == 5


def test_bounded_queue_drops_oldest():
    async def main():
        bus = Bus(queue_size=3)
        agen = bus.listen()
        t = asyncio.create_task(agen.__anext__())  # registers the subscriber
        await asyncio.sleep(0.05)
        for i in range(10):
            bus._send({'type': 'bar', 'data': {'i': i}})
        first = await t
        rest = [await agen.__anext__() for _ in range(2)]
        await agen.aclose()
        return [first] + rest, len(bus._subs)
    evs, subs = run(main())
    assert [e['data']['i'] for e in evs] == [7, 8, 9] and subs == 0


def test_publish_without_loop_is_noop():
    Bus().publish('tick', {'id': 'X'})  # must not raise
