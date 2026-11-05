from __future__ import annotations

import asyncio

from bluesky_adaptive.server.events import EventBroker


def test_resume_and_invalid_event_ids():
    broker = EventBroker(history_size=2)
    first = broker.publish("optimization", {"id": "one"})
    second = broker.publish("trial", {"number": 1}, "one")
    third = broker.publish("trial", {"number": 2}, "one")

    async def subscribe():
        resumed = broker.subscribe(first.id)
        unknown = broker.subscribe("other-1")
        future = broker.subscribe(f"{first.id.rsplit('-', 1)[0]}-99")
        stale = broker.subscribe(f"{first.id.rsplit('-', 1)[0]}-0")
        result = ([event.id for event in resumed.backlog], resumed.resumed, unknown.resumed, future.resumed, stale.resumed)
        for subscription in (resumed, unknown, future, stale):
            subscription.close()
        return result

    assert asyncio.run(subscribe()) == ([second.id, third.id], True, False, False, False)


def test_overflow_closes_subscription():
    broker = EventBroker(queue_size=1)

    async def overflow():
        subscription = broker.subscribe()
        broker.publish("trial", {"number": 1})
        broker.publish("trial", {"number": 2})
        await asyncio.sleep(0)
        assert [event async for event in subscription] == []

    asyncio.run(overflow())
