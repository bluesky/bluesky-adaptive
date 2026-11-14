"""Evaluation helpers that read acquisition data from Bluesky documents."""

from collections.abc import Hashable
from typing import Any, Literal

from bluesky.callbacks import CallbackBase
from event_model import Event, EventDescriptor, RunStart

from ..plans import ACQUISITION_ORDER_KEY

_PRIMARY_STREAM: Literal["primary"] = "primary"


class RunEventBuffer(CallbackBase):
    """Buffer the ``"primary"`` event data of acquisition runs by suggestion ID.

    Subscribe with ``RE.subscribe(buffer)`` and pass :meth:`pop` to :func:`~bluesky_adaptive.evaluation.per_suggestion`.
    Buffers runs that record ``start["bluesky-adaptive"]["acquisition_order"]``, such as from
    :func:`~bluesky_adaptive.plans.default_acquire`, pairing the n-th event with the n-th suggestion ID.
    """

    def __init__(self) -> None:
        super().__init__()
        self._acquisition_orders: dict[str, list[Hashable]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._primary_descriptors: dict[str, str] = {}

    def start(self, doc: RunStart) -> None:
        """Begin buffering the run if it records its acquisition order."""
        acquisition_order = doc.get("bluesky-adaptive", {}).get(ACQUISITION_ORDER_KEY)
        if acquisition_order is not None:
            self._acquisition_orders[doc["uid"]] = list(acquisition_order)
            self._events[doc["uid"]] = []

    def descriptor(self, doc: EventDescriptor) -> None:
        """Track the ``"primary"`` descriptor of buffered runs."""
        if doc.get("name") == _PRIMARY_STREAM and doc["run_start"] in self._events:
            self._primary_descriptors[doc["uid"]] = doc["run_start"]

    def event(self, doc: Event) -> Event:
        """Buffer ``"primary"`` event data."""
        run_uid = self._primary_descriptors.get(doc["descriptor"])
        if run_uid is not None:
            self._events[run_uid].append(doc["data"])
        return doc

    def pop(self, uid: str) -> dict[Hashable, dict[str, Any]]:
        """Remove a buffered run and return its event data keyed by suggestion ID.

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
        if uid not in self._events:
            raise KeyError(
                f"No buffered run with uid {uid!r}. RunEventBuffer buffers runs that record "
                f"start['bluesky-adaptive'][{ACQUISITION_ORDER_KEY!r}], such as default_acquire runs, must be "
                "subscribed with RE.subscribe(buffer), and returns each run once."
            )
        acquisition_order = self._acquisition_orders.pop(uid)
        events = self._events.pop(uid)
        self._primary_descriptors = {
            descriptor_uid: run_uid for descriptor_uid, run_uid in self._primary_descriptors.items() if run_uid != uid
        }
        return dict(zip(acquisition_order, events, strict=True))
