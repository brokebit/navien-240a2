"""Fan-out from the state model to consumers. PLAN.md §1.1.

Each subscriber gets its own bounded asyncio.Queue. publish() never blocks or awaits:
when a subscriber's queue is full its oldest message is dropped, the drop is counted,
and the subscriber is flagged so it can tell its client there was a gap. One slow
consumer therefore loses only its own (oldest) data and never delays anyone else.

Everything here runs on the event loop thread; it is not thread-safe by design.
"""

from __future__ import annotations

import asyncio


class Subscription:
    def __init__(self, hub: "Hub", topics: set[str], maxsize: int, name: str) -> None:
        self.hub = hub
        self.topics = topics
        self.name = name
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.gap = False                  # set on drop; the consumer clears it

    def offer(self, topic: str, msg) -> None:
        if topic not in self.topics:
            return
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
            self.gap = True
        self.queue.put_nowait((topic, msg))

    async def get(self):
        return await self.queue.get()

    def close(self) -> None:
        self.hub.unsubscribe(self)


class Hub:
    def __init__(self) -> None:
        self.subscribers: list[Subscription] = []
        self.published = 0

    def subscribe(self, topics, maxsize: int = 120, name: str = "") -> Subscription:
        sub = Subscription(self, set(topics), maxsize, name)
        self.subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self.subscribers:
            self.subscribers.remove(sub)

    def publish(self, topic: str, msg) -> None:
        self.published += 1
        for sub in self.subscribers:
            sub.offer(topic, msg)

    def stats(self) -> list[dict]:
        return [{"name": s.name, "topics": sorted(s.topics), "queued": s.queue.qsize(),
                 "dropped": s.dropped} for s in self.subscribers]
