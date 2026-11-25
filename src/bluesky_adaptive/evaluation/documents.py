"""Evaluation helpers that read acquisition data from Bluesky documents."""

import threading
from collections.abc import Hashable
from typing import Any, Literal

from bluesky.callbacks import CallbackBase
from event_model import Event, EventDescriptor, RunStart

from ..plans import ACQUISITION_ORDER_KEY

_PRIMARY_STREAM: Literal["primary"] = "primary"


class RunEventBuffer(CallbackBase):
    """Buffer the ``"primary"`` event data of acquisition runs by suggestion ID.

    Subscribe with ``RE.subscribe(buffer)``, or to the dispatcher of a remote document stream alongside
    :class:`~bluesky_adaptive.evaluation.queueserver.QueueItemRuns`, and pass :meth:`pop` to
    :func:`~bluesky_adaptive.evaluation.per_suggestion`.
    Buffers runs that record ``start["bluesky-adaptive"]["acquisition_order"]``, such as from
    :func:`~bluesky_adaptive.plans.default_acquire`, pairing the n-th event with the n-th suggestion ID.
    Use one buffer per consumer, such as one per runner: popping a run drops the runs that started before it. Its
    methods may be called from different threads, such as a dispatcher's and an evaluation's.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._acquisition_orders: dict[str, list[Hashable]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._primary_descriptors: dict[str, str] = {}

    def start(self, doc: RunStart) -> None:
        """Begin buffering the run if it records its acquisition order."""
        acquisition_order = doc.get("bluesky-adaptive", {}).get(ACQUISITION_ORDER_KEY)
        if acquisition_order is None:
            return
        with self._lock:
            self._acquisition_orders[doc["uid"]] = list(acquisition_order)
            self._events[doc["uid"]] = []

    def descriptor(self, doc: EventDescriptor) -> None:
        """Track the ``"primary"`` descriptor of buffered runs."""
        with self._lock:
            if doc.get("name") == _PRIMARY_STREAM and doc["run_start"] in self._events:
                self._primary_descriptors[doc["uid"]] = doc["run_start"]

    def event(self, doc: Event) -> Event:
        """Buffer ``"primary"`` event data."""
        with self._lock:
            run_uid = self._primary_descriptors.get(doc["descriptor"])
            if run_uid is not None:
                self._events[run_uid].append(doc["data"])
        return doc

    def pop(self, uid: str) -> dict[Hashable, dict[str, Any]]:
        """Remove a buffered run and return its event data keyed by suggestion ID.

        Also drops the runs that started before it: an evaluation pops each acquisition run before the next one
        starts, so earlier runs are never evaluated.

        Parameters
        ----------
        uid : str
            Run UID returned by the acquisition plan.

        Returns
        -------
        dict[Hashable, dict[str, Any]]
            Event data keyed by suggestion ID, in acquisition order.

        Raises
        ------
        KeyError
            If the run is not buffered.
        ValueError
            If the number of ``"primary"`` events differs from the number of suggestion IDs.
        """
        with self._lock:
            if uid not in self._events:
                raise KeyError(
                    f"No buffered run with uid {uid!r}. Check that the buffer receives the run's documents, that the "
                    f"run's start document records start['bluesky-adaptive'][{ACQUISITION_ORDER_KEY!r}] (as "
                    "default_acquire does), and that neither this run nor a later one was already popped."
                )
            # Runs are buffered in start order.
            for run_uid in list(self._events):
                if run_uid == uid:
                    break
                del self._acquisition_orders[run_uid], self._events[run_uid]
            acquisition_order = self._acquisition_orders.pop(uid)
            events = self._events.pop(uid)
            self._primary_descriptors = {
                descriptor_uid: run_uid
                for descriptor_uid, run_uid in self._primary_descriptors.items()
                if run_uid in self._events
            }
        return dict(zip(acquisition_order, events, strict=True))
