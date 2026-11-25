"""
Evaluation helpers for plans run by a Bluesky Queue Server.

.. warning::

    This module is part of the **experimental** queueserver integration. The API may change in future releases
    without a deprecation period.
"""

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

try:
    import bluesky_queueserver_api.http
    import bluesky_queueserver_api.zmq
except ImportError as e:
    raise ImportError(
        "The Queue Server evaluation helpers require additional dependencies. "
        "Install them with: pip install bluesky-adaptive[queueserver]"
    ) from e

from bluesky.callbacks import CallbackBase
from event_model import RunStart, RunStop


class QueueItemRuns(CallbackBase):
    """Resolve Queue Server item UIDs to the UIDs of the runs they open, from start and stop documents.

    bluesky-queueserver 0.0.26 and later record ``start["queue_server"]["queue_item_uid"]`` in every run a queue item
    opens. Subscribe this callback to the dispatcher of the Queue Server's document stream before submitting plans,
    and pass :meth:`run_uid` to :func:`~bluesky_adaptive.evaluation.per_run`. :meth:`run_uid` returns once the run's
    stop document reaches this callback, so read the run's data from the same dispatcher, e.g. with
    :class:`~bluesky_adaptive.evaluation.documents.RunEventBuffer`, or with a reader that waits for the run's stop
    document. The item's first run, or the first that passes ``run_filter``, is used.

    The documents tell when the data is ready; the RE Manager tells whether the item is still queued or running.
    While it is, :meth:`run_uid` waits without a time limit, so time spent in the queue does not count; stopping,
    aborting, or removing the item ends the wait. An item that ends without opening a run raises at once.

    Use one instance per runner: consuming an item drops the items whose runs started earlier, including other
    clients' items. Its methods may be called from different threads, such as a dispatcher's and an evaluation's.

    .. warning::

        This class is part of the **experimental** queueserver integration. The API may change in future releases
        without a deprecation period.

    Parameters
    ----------
    re_manager_api : bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI
        The RE Manager that runs the items, asked for their queue and history state every ``poll_interval`` seconds
        until the item's run has stopped or the item has finished.
    timeout : float, optional
        Seconds to wait for the selected run's stop document after the RE Manager reports that the item finished.
        Default is 60.
    poll_interval : float, optional
        Seconds between requests to the RE Manager. Default is 0.5.
    run_filter : Callable[[RunStart], bool] | None, optional
        Select runs by their start document, e.g. ``lambda start: not start.get("dark_frame")``. It is called
        on the dispatcher's thread; if it raises, :meth:`run_uid` raises for that item instead of waiting.

    Raises
    ------
    ValueError
        If ``timeout`` is negative or ``poll_interval`` is not positive.

    Examples
    --------
    Evaluate the ``"primary"`` events of the run each submitted plan opens:

    .. code-block:: python

        import threading

        from bluesky.callbacks.zmq import RemoteDispatcher
        from bluesky_queueserver_api.zmq import REManagerAPI

        from bluesky_adaptive.ax.queueserver_agent import QueueserverAgent
        from bluesky_adaptive.evaluation import per_run, per_suggestion
        from bluesky_adaptive.evaluation.documents import RunEventBuffer
        from bluesky_adaptive.evaluation.queueserver import QueueItemRuns

        def score(data):
            return {"objective": data["det"]}

        RM = REManagerAPI()
        buffer = RunEventBuffer()
        runs = QueueItemRuns(RM)
        dispatcher = RemoteDispatcher("localhost:5578")
        dispatcher.subscribe(buffer)
        dispatcher.subscribe(runs)
        threading.Thread(target=dispatcher.start, daemon=True).start()
        agent = QueueserverAgent(
            RM,
            ...,
            evaluation_function=per_run(per_suggestion(score, buffer.pop), runs.run_uid),
        )
    """

    def __init__(
        self,
        re_manager_api: bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
        run_filter: Callable[[RunStart], bool] | None = None,
    ) -> None:
        if timeout < 0:
            raise ValueError(f"timeout must be non-negative, got {timeout!r}.")
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be positive, got {poll_interval!r}.")
        super().__init__()
        self._rm = re_manager_api
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._run_filter = run_filter
        self._condition = threading.Condition()
        self._run_uids: dict[str, str] = {}  # item UID -> selected run UID, in run start order
        self._item_uids: dict[str, str] = {}  # selected run UID -> item UID, until the run stops
        # item UID -> stop document of its selected run, or the exception run_filter raised on its run
        self._outcomes: dict[str, RunStop | Exception] = {}

    def start(self, doc: RunStart) -> None:
        """Select the run if it is the first of its queue item to pass ``run_filter``."""
        item_uid = doc.get("queue_server", {}).get("queue_item_uid")
        if item_uid is None:
            return
        with self._condition:
            if item_uid in self._run_uids:
                return
        try:
            selected = self._run_filter is None or self._run_filter(doc)
        except Exception as error:
            # Raised from run_uid for this item: the dispatcher cannot report it to the evaluation waiting on it.
            with self._condition:
                if item_uid not in self._run_uids:
                    self._run_uids[item_uid] = doc["uid"]
                    self._outcomes[item_uid] = error
                    self._condition.notify_all()
            return
        if not selected:
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
                self._outcomes[item_uid] = doc
                self._condition.notify_all()

    def run_uid(self, item_uid: str) -> str:
        """Wait for the queue item's run to stop and return its UID.

        Removes the item and the items whose runs started earlier. A run that an operator stops with ``re_stop``
        also ends with ``"success"``, so the data reader must check that the run is complete, as
        :meth:`RunEventBuffer.pop <bluesky_adaptive.evaluation.documents.RunEventBuffer.pop>` does.

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
            If the item finished, but no selected run of it stopped within ``timeout`` seconds.
        RuntimeError
            If the run's exit status is not ``"success"``, the item ended without opening a run, or ``run_filter``
            raised on one of its runs.
        KeyError
            If the RE Manager has no record of the item, e.g. because it was removed from the queue or the history
            was cleared.
        """
        deadline: float | None = None  # Set once the RE Manager reports that the item finished.
        while True:
            with self._condition:
                wait = self._poll_interval if deadline is None else max(deadline - time.monotonic(), 0.0)
                if self._condition.wait_for(lambda: item_uid in self._outcomes, timeout=wait):
                    run_uid = self._run_uids[item_uid]
                    outcome = self._outcomes[item_uid]
                    self._consume(item_uid)
                    break
            if deadline is not None:
                raise TimeoutError(
                    f"Queue item {item_uid!r} finished, but no selected run of it stopped within {self._timeout} s. "
                    "Check that the dispatcher feeding this callback is running and was subscribed before the plan "
                    "was submitted, and that a run passes this callback's run_filter, if given."
                )
            result = self._item_result(item_uid)
            if result is not None:
                if not result["run_uids"]:
                    raise RuntimeError(
                        f"Queue item {item_uid!r} ended with status {result['exit_status']!r} without opening a run: "
                        f"{result.get('msg') or '(no message)'}"
                    )
                deadline = time.monotonic() + self._timeout
        if isinstance(outcome, Exception):
            raise RuntimeError(f"run_filter raised on run {run_uid!r} of queue item {item_uid!r}.") from outcome
        if outcome["exit_status"] != "success":
            raise RuntimeError(
                f"Run {run_uid!r} of queue item {item_uid!r} ended with status {outcome['exit_status']!r}: "
                f"{outcome.get('reason') or '(no reason given)'}"
            )
        return run_uid

    def _item_result(self, item_uid: str) -> Mapping[str, Any] | None:
        """Return the ``result`` of the item's history entry, or None while it is queued or running."""
        # Items only move from the queue to running to the history, and each response is one consistent state of
        # the RE Manager, so checking in that order cannot miss an item in transit. A failed item is put back in the
        # queue under a new UID, so its original UID resolves to the failure in the history.
        queue = self._rm.queue_get(reload=True)
        if queue["running_item"].get("item_uid") == item_uid or any(item["item_uid"] == item_uid for item in queue["items"]):
            return None
        for item in reversed(self._rm.history_get(reload=True)["items"]):
            if item["item_uid"] == item_uid:
                return item["result"]
        raise KeyError(
            f"The RE Manager has no record of queue item {item_uid!r}. Check whether it was removed from the queue "
            "or the history was cleared."
        )

    def _consume(self, item_uid: str) -> None:
        """Remove the item and the items whose runs started earlier; call with the condition held."""
        # Items run one at a time, so runs that started earlier belong to items that were consumed, timed out,
        # or submitted by other clients.
        for older_item_uid in list(self._run_uids):
            self._item_uids.pop(self._run_uids.pop(older_item_uid), None)
            self._outcomes.pop(older_item_uid, None)
            if older_item_uid == item_uid:
                break
