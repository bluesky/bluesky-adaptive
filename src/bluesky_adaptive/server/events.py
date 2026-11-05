"""Publish lifecycle events from any thread to asyncio subscribers, with replay for reconnecting clients."""

import asyncio
import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    """
    A lifecycle event.

    Parameters
    ----------
    sequence : int
        Position in the broker's stream, starting at 1.
    id : str
        The Server-Sent Events ID: the broker's epoch and the sequence. A client resumes from it.
    type : str
        The kind of resource that changed, for example ``"optimization"``, ``"execution"``, or ``"trial"``.
    optimization_id : str | None
        The optimization the event belongs to.
    data : Any
        The JSON-compatible representation of the changed resource.
    """

    sequence: int
    id: str
    type: str
    optimization_id: str | None
    data: Any


class EventSubscription:
    """
    An asynchronous iterator over events published after subscribing.

    Created by :meth:`EventBroker.subscribe` on the event loop that consumes it. If the
    subscriber falls more than the queue size behind, the iteration ends; the client
    reconnects with its last event ID and resumes from the broker's history.

    Attributes
    ----------
    backlog : list[Event]
        Events after the requested last event ID, to send before live events.
    resumed : bool
        Whether the requested last event ID was resumed from history. If not, the client
        missed events it cannot replay and should be sent a snapshot.
    resume_id : str | None
        The ID of the last event published before subscribing; the ID of a snapshot.
    """

    def __init__(self, broker: "EventBroker", loop: asyncio.AbstractEventLoop, queue_size: int):
        self._broker = broker
        self._loop = loop
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=queue_size)
        self.backlog: list[Event] = []
        self.resumed = False
        self.resume_id: str | None = None

    def __aiter__(self) -> "EventSubscription":
        """Iterate over live events."""
        return self

    async def __anext__(self) -> Event:
        """Wait for the next event; stop when the subscription fell behind."""
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    def close(self) -> None:
        """Stop receiving events."""
        self._broker._unsubscribe(self)

    def _offer(self, event: Event) -> None:
        """Hand an event to the subscriber's loop. Called from any thread."""
        try:
            self._loop.call_soon_threadsafe(self._put, event)
        except RuntimeError:
            # The subscriber's event loop is closed.
            self.close()

    def _put(self, event: Event) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Event subscriber fell behind; closing its stream so it can resume from history")
            self.close()
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(None)


class EventBroker:
    """
    Fans out events published from any thread to asyncio subscribers.

    Keeps the most recent events so that clients reconnecting with ``Last-Event-ID``
    receive what they missed. Event IDs include a random epoch, so IDs from an earlier
    server process are recognized as unknown rather than replayed from the wrong place.

    Parameters
    ----------
    history_size : int, optional
        How many recent events to keep for replay. Default is 1000.
    queue_size : int, optional
        How many events a subscriber may fall behind before its stream ends. Default is 1000.
    """

    def __init__(self, *, history_size: int = 1000, queue_size: int = 1000):
        self._epoch = uuid.uuid4().hex[:8]
        self._sequence = 0
        self._history: deque[Event] = deque(maxlen=history_size)
        self._subscribers: set[EventSubscription] = set()
        self._queue_size = queue_size
        # Reentrant: offering an event to a subscriber whose loop has closed unsubscribes it.
        self._lock = threading.RLock()

    def publish(self, type: str, data: Any, optimization_id: str | None = None) -> Event:
        """
        Publish an event to every subscriber.

        Parameters
        ----------
        type : str
            The kind of resource that changed.
        data : Any
            The JSON-compatible representation of the changed resource.
        optimization_id : str | None, optional
            The optimization the event belongs to.

        Returns
        -------
        Event
            The published event.
        """
        with self._lock:
            self._sequence += 1
            event = Event(
                sequence=self._sequence,
                id=f"{self._epoch}-{self._sequence}",
                type=type,
                optimization_id=optimization_id,
                data=data,
            )
            self._history.append(event)
            # Scheduled under the lock so every subscriber receives events in sequence order.
            for subscriber in tuple(self._subscribers):
                subscriber._offer(event)
        return event

    def subscribe(self, last_event_id: str | None = None) -> EventSubscription:
        """
        Subscribe to events. Must be called on the event loop that consumes the subscription.

        Parameters
        ----------
        last_event_id : str | None, optional
            The ID of the last event the client received. Later events still in the history are
            put in the subscription's ``backlog``.

        Returns
        -------
        EventSubscription
            The subscription; close it when done.
        """
        subscription = EventSubscription(self, asyncio.get_running_loop(), self._queue_size)
        with self._lock:
            backlog = self._events_after(last_event_id)
            subscription.resumed = backlog is not None
            subscription.backlog = backlog or []
            subscription.resume_id = self._history[-1].id if self._history else None
            self._subscribers.add(subscription)
        return subscription

    def _events_after(self, last_event_id: str | None) -> list[Event] | None:
        """LOCKED: the events after an ID, or ``None`` if the ID cannot be resumed from the history."""
        if last_event_id is None:
            return None
        epoch, _, sequence = last_event_id.partition("-")
        if epoch != self._epoch or not sequence.isdigit() or int(sequence) > self._sequence:
            return None
        oldest = self._history[0].sequence if self._history else self._sequence + 1
        if int(sequence) < oldest - 1:
            return None
        return [event for event in self._history if event.sequence > int(sequence)]

    def _unsubscribe(self, subscription: EventSubscription) -> None:
        with self._lock:
            self._subscribers.discard(subscription)
