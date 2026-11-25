"""Evaluation helpers that read acquisition data from Bluesky documents."""

import threading
from collections.abc import Callable, Hashable
from typing import Any, Literal

from bluesky.callbacks import CallbackBase
from event_model import Event, EventDescriptor, RunStart, RunStop

from ..plans import ACQUISITION_ORDER_KEY

_PRIMARY_STREAM: Literal["primary"] = "primary"


class RunEventBuffer(CallbackBase):
    """Buffer the ``"primary"`` event data of acquisition runs by suggestion ID.

    Subscribe with ``RE.subscribe(buffer)``, or to the dispatcher of a remote document stream alongside
    :class:`QueueItemRuns`, and pass :meth:`pop` to :func:`~bluesky_adaptive.evaluation.per_suggestion`.
    Buffers runs that record ``start["bluesky-adaptive"]["acquisition_order"]``, such as from
    :func:`~bluesky_adaptive.plans.default_acquire`, pairing the n-th event with the n-th suggestion ID.
    Its methods may be called from different threads, such as a dispatcher's and an evaluation's.
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


class QueueItemRuns(CallbackBase):
    """Resolve Queue Server item UIDs to the UIDs of the runs they open, from start and stop documents.

    bluesky-queueserver 0.0.26 and later record ``start["queue_server"]["queue_item_uid"]`` in every run a queue item
    opens. Subscribe this callback to the dispatcher of the Queue Server's document stream before submitting plans,
    and pass :meth:`run_uid` to :func:`~bluesky_adaptive.evaluation.per_run`. :meth:`run_uid` returns once the run's
    stop document reaches this callback, so read the run's data from the same dispatcher, e.g. with
    :class:`RunEventBuffer`, or with a reader that waits for the run's stop document. The item's first run, or the
    first that passes ``run_filter``, is used.

    Use one instance per runner: consuming an item drops the items whose runs started earlier, including other
    clients' items. Its methods may be called from different threads, such as a dispatcher's and an evaluation's.

    Parameters
    ----------
    timeout : float
        Seconds :meth:`run_uid` waits for the item's run to stop, including the time the item waits in the queue.
        Required because an item that fails before opening a run emits no documents, while the runner's ``stop()``
        and interpreter exit wait for the evaluation to return.
    run_filter : Callable[[RunStart], bool] | None, optional
        Select runs by their start document, e.g. ``lambda start: not start.get("dark_frame")``. It is called
        on the dispatcher's thread, which receives its exceptions.

    Raises
    ------
    ValueError
        If ``timeout`` is negative.

    Examples
    --------
    Evaluate the ``"primary"`` events of the run each submitted plan opens:

    .. code-block:: python

        import threading

        from bluesky.callbacks.zmq import RemoteDispatcher

        from bluesky_adaptive.ax.queueserver_agent import QueueserverAgent
        from bluesky_adaptive.evaluation import per_run, per_suggestion
        from bluesky_adaptive.evaluation.documents import QueueItemRuns, RunEventBuffer

        def score(data):
            return {"objective": data["det"]}

        buffer = RunEventBuffer()
        runs = QueueItemRuns(timeout=600)
        dispatcher = RemoteDispatcher("localhost:5578")
        dispatcher.subscribe(buffer)
        dispatcher.subscribe(runs)
        threading.Thread(target=dispatcher.start, daemon=True).start()
        agent = QueueserverAgent(
            ...,
            evaluation_function=per_run(per_suggestion(score, buffer.pop), runs.run_uid),
        )
    """

    def __init__(
        self,
        *,
        timeout: float,
        run_filter: Callable[[RunStart], bool] | None = None,
    ) -> None:
        if timeout < 0:
            raise ValueError(f"timeout must be non-negative, got {timeout!r}.")
        super().__init__()
        self._timeout = timeout
        self._run_filter = run_filter
        self._condition = threading.Condition()
        self._run_uids: dict[str, str] = {}  # item UID -> selected run UID, in run start order
        self._item_uids: dict[str, str] = {}  # selected run UID -> item UID, until the run stops
        self._stops: dict[str, RunStop] = {}  # item UID -> stop document of its selected run

    def start(self, doc: RunStart) -> None:
        """Select the run if it is the first of its queue item to pass ``run_filter``."""
        item_uid = doc.get("queue_server", {}).get("queue_item_uid")
        if item_uid is None or (self._run_filter is not None and not self._run_filter(doc)):
            return
        with self._condition:
            if item_uid not in self._run_uids:
                self._run_uids[item_uid] = doc["uid"]
                self._item_uids[doc["uid"]] = item_uid

    def stop(self, doc: RunStop) -> None:
        """Record the stop document of a selected run."""
        with self._condition:
            item_uid = self._item_uids.pop(doc["run_start"], None)
            if item_uid is not None:
                self._stops[item_uid] = doc
                self._condition.notify_all()

    def run_uid(self, item_uid: str) -> str:
        """Wait for the queue item's run to stop and return its UID.

        Removes the item and the items whose runs started earlier.

        Parameters
        ----------
        item_uid : str
            Queue item UID, such as the one the Queue Server runner passes to the evaluation function.

        Returns
        -------
        str
            UID of the item's selected run.

        Raises
        ------
        TimeoutError
            If no selected run of the item stops within ``timeout`` seconds.
        RuntimeError
            If the run's exit status is not ``"success"``.
        """
        with self._condition:
            if not self._condition.wait_for(lambda: item_uid in self._stops, timeout=self._timeout):
                raise TimeoutError(
                    f"No run of queue item {item_uid!r} stopped within {self._timeout} s. Check the queue history for a "
                    "plan that failed before opening a run, that the dispatcher feeding this callback is running and was "
                    "subscribed before the plan was submitted, and that the run passes this callback's run_filter, if "
                    "given."
                )
            stop = self._stops[item_uid]
            run_uid = self._run_uids[item_uid]
            # Items run one at a time, so runs that started earlier belong to items that were consumed, timed out,
            # or submitted by other clients.
            for older_item_uid in list(self._run_uids):
                self._item_uids.pop(self._run_uids.pop(older_item_uid), None)
                self._stops.pop(older_item_uid, None)
                if older_item_uid == item_uid:
                    break
        if stop["exit_status"] != "success":
            raise RuntimeError(
                f"Run {run_uid!r} of queue item {item_uid!r} ended with status {stop['exit_status']!r}: "
                f"{stop.get('reason') or '(no reason given)'}"
            )
        return run_uid
