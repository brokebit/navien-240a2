import asyncio

from navien.hub import Hub


def test_fanout_and_topics():
    async def go():
        hub = Hub()
        a = hub.subscribe({"cycle"}, name="a")
        b = hub.subscribe({"cycle", "frame"}, name="b")
        hub.publish("cycle", 1)
        hub.publish("frame", "f")
        assert await a.get() == ("cycle", 1)
        assert a.queue.empty()
        assert [await b.get(), await b.get()] == [("cycle", 1), ("frame", "f")]
    asyncio.run(go())


def test_slow_subscriber_drops_oldest_and_does_not_affect_others():
    async def go():
        hub = Hub()
        slow = hub.subscribe({"cycle"}, maxsize=3, name="slow")
        fast = hub.subscribe({"cycle"}, maxsize=100, name="fast")
        for i in range(10):
            hub.publish("cycle", i)
        assert [slow.queue.get_nowait()[1] for _ in range(3)] == [7, 8, 9]
        assert slow.dropped == 7 and slow.gap
        assert fast.queue.qsize() == 10 and fast.dropped == 0
        assert {s["name"]: s["dropped"] for s in hub.stats()} == {"slow": 7, "fast": 0}
    asyncio.run(go())


def test_unsubscribe():
    async def go():
        hub = Hub()
        s = hub.subscribe({"cycle"})
        s.close()
        hub.publish("cycle", 1)
        assert s.queue.empty() and hub.subscribers == []
    asyncio.run(go())
