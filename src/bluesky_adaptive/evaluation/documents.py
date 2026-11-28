"""Evaluation helpers that read acquisition data from Bluesky documents."""

import threading
from collections.abc import Hashable
from typing import Any, Literal

from bluesky.callbacks import CallbackBase
from event_model import Event, EventDescriptor, RunStart, RunStop

from ..plans import ACQUISITION_ORDER_KEY

_PRIMARY_STREAM: Literal["primary"] = "primary"


class RunEventBuffer(CallbackBase):
    """Buffer the ``"primary"`` event data of acquisition runs by suggestion ID.

    Subscribe with ``RE.subscribe(buffer)`` or to a remote document stream, and pass :meth:`pop` to
    :func:`~bluesky_adaptive.evaluation.per_suggestion`. Buffers runs that record
    ``start["bluesky-adaptive"]["acquisition_order"]``, as :func:`~bluesky_adaptive.plans.default_acquire` does,
    pairing the n-th event with the n-th suggestion ID. :meth:`pop` rejects runs whose stop document records an
    ``exit_status`` other than ``"success"``. Thread-safe; use one buffer per consumer.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._acquisition_orders: dict[str, list[Hashable]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._primary_descriptors: dict[str, str] = {}
        self._stops: dict[str, RunStop] = {}

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

    def stop(self, doc: RunStop) -> None:
        """Record the stop document of buffered runs."""
        with self._lock:
            if doc["run_start"] in self._events:
                self._stops[doc["run_start"]] = doc

    def pop(self, uid: str) -> dict[Hashable, dict[str, Any]]:
        """Remove a buffered run, and any that started before it, and return its event data by suggestion ID.

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
        RuntimeError
            If the run's stop document records an ``exit_status`` other than ``"success"``.
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
            run_uids = list(self._events)
            for run_uid in run_uids[: run_uids.index(uid)]:
                del self._acquisition_orders[run_uid], self._events[run_uid]
                self._stops.pop(run_uid, None)
            acquisition_order = self._acquisition_orders.pop(uid)
            events = self._events.pop(uid)
            stop = self._stops.pop(uid, None)
            self._primary_descriptors = {
                descriptor_uid: run_uid
                for descriptor_uid, run_uid in self._primary_descriptors.items()
                if run_uid in self._events
            }
        if stop is not None and stop["exit_status"] != "success":
            raise RuntimeError(
                f"Run {uid!r} ended with status {stop['exit_status']!r}: {stop.get('reason') or '(no reason given)'}"
            )
        return dict(zip(acquisition_order, events, strict=True))
