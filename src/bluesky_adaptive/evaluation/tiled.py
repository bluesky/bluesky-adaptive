"""Evaluation helpers that read acquisition data from Tiled."""

import time
from collections.abc import Callable, Hashable, Mapping
from typing import TYPE_CHECKING, Any, Literal

try:
    from tiled.client.container import Container
    from tiled.queries import Eq
except ImportError as e:
    raise ImportError(
        "The Tiled evaluation helpers require additional dependencies. "
        "Install them with: pip install bluesky-adaptive[tiled]"
    ) from e

from event_model import RunStart

from ..plans import ACQUISITION_ORDER_KEY
from ._queue_items import finished_item_run_uids

if TYPE_CHECKING:
    # Annotations only: TiledRunPoller must import without the queueserver extra.
    import bluesky_queueserver_api.http
    import bluesky_queueserver_api.zmq

_PRIMARY_STREAM: Literal["primary"] = "primary"
_QUEUE_ITEM_UID_KEY = "start.queue_server.queue_item_uid"


class TiledRunPoller:
    """Poll Tiled for a stopped acquisition run and return its ``"primary"`` data by suggestion ID.

    Pass an instance to :func:`~bluesky_adaptive.evaluation.per_suggestion`. Every call polls Tiled until the run has a
    stop document, so the run's documents may reach Tiled after the acquisition plan returns, e.g. through a separate
    ``TiledWriter`` service. Reads runs that record ``start["bluesky-adaptive"]["acquisition_order"]``, such as from
    :func:`~bluesky_adaptive.plans.default_acquire`, pairing the n-th ``"primary"`` row with the n-th suggestion ID.

    Parameters
    ----------
    client : Container
        Tiled container keyed by run UID that a ``TiledWriter`` writes to, e.g. ``tiled.client.from_uri(...)`` or a
        ``CatalogOfBlueskyRuns``.
    poll_interval : float, optional
        Seconds between polls. Default is 0.5.
    timeout : float | None, optional
        Seconds to wait for the run's stop document before raising :class:`TimeoutError`, or ``None`` to wait
        indefinitely. Default is 60.

    Raises
    ------
    ValueError
        If ``poll_interval`` is not positive or ``timeout`` is negative.
    """

    def __init__(self, client: Container, *, poll_interval: float = 0.5, timeout: float | None = 60.0) -> None:
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be positive, got {poll_interval!r}.")
        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout must be non-negative or None, got {timeout!r}.")
        self._client = client
        self._poll_interval = poll_interval
        self._timeout = timeout

    def __call__(self, uid: str) -> dict[Hashable, dict[str, Any]]:
        """Wait for a run to stop and return its ``"primary"`` data keyed by suggestion ID.

        Parameters
        ----------
        uid : str
            Run UID returned by the acquisition plan.

        Returns
        -------
        dict[Hashable, dict[str, Any]]
            Each ``"primary"`` row as a mapping of data key to value, keyed by suggestion ID in acquisition order.

        Raises
        ------
        TimeoutError
            If the run has no stop document in Tiled within ``timeout`` seconds.
        KeyError
            If the run does not record its acquisition order.
        ValueError
            If the number of ``"primary"`` rows differs from the number of suggestion IDs.
        """
        run = self._wait_for_stop(uid)
        acquisition_order = run.metadata["start"].get("bluesky-adaptive", {}).get(ACQUISITION_ORDER_KEY)
        if acquisition_order is None:
            raise KeyError(
                f"Run {uid!r} does not record start['bluesky-adaptive'][{ACQUISITION_ORDER_KEY!r}]. TiledRunPoller "
                "reads runs from acquisition plans that record it, such as default_acquire."
            )
        stream = run[_PRIMARY_STREAM]
        # Read each data key on its own: reading a whole stream at once is not supported for every data format.
        columns = {key: stream[key].read() for key in stream.metadata["data_keys"]}
        rows = [dict(zip(columns, values, strict=True)) for values in zip(*columns.values(), strict=True)]
        return dict(zip(acquisition_order, rows, strict=True))

    def _wait_for_stop(self, uid: str) -> Any:
        """Poll until the run is in Tiled with a stop document and return its client."""
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        while True:
            # Mapping.get returns None until the run's start document reaches Tiled. TiledWriter writes the stop
            # document after all of the run's data, so a run with a stop document is complete.
            run = self._client.get(uid)
            if run is not None and run.metadata.get("stop") is not None:
                return run
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Run {uid!r} has no stop document in Tiled after {self._timeout}s. Check that its documents are "
                    "written to this Tiled container, e.g. by a TiledWriter, or increase the timeout."
                )
            time.sleep(self._poll_interval)


class TiledQueueItemRuns:
    """Resolve Queue Server item UIDs to the UIDs of the runs they open by searching Tiled.

    bluesky-queueserver 0.0.26 and later record ``start["queue_server"]["queue_item_uid"]`` in every run. Pass
    :meth:`resolve` to :func:`~bluesky_adaptive.evaluation.per_run`. It searches Tiled for the item's runs, uses the
    first in start order that passes ``run_filter``, and returns once that run has a stop document in Tiled.
    ``TiledWriter`` writes the stop document after the rest of the run, so the run's data is complete by then.

    Unlike :class:`~bluesky_adaptive.evaluation.queueserver.QueueItemRuns`, it needs no document stream and keeps no
    state between calls, so it also resolves items submitted before it was created. While the RE Manager reports the
    item queued or running, :meth:`resolve` waits without a time limit.

    .. warning:: Part of the **experimental** queueserver integration; the API may change without deprecation.

    Parameters
    ----------
    re_manager_api : bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI
        The RE Manager running the items.
    client : Container
        Tiled container that a ``TiledWriter`` writes runs to, e.g. ``tiled.client.from_uri(...)`` or a
        ``CatalogOfBlueskyRuns``.
    run_filter : Callable[[RunStart], bool] | None, optional
        Selects runs by start document, e.g. ``lambda start: not start.get("dark_frame")``. Called at most once per
        run in each :meth:`resolve` call; its exceptions are raised from :meth:`resolve`.
    timeout : float, optional
        Seconds to wait, once the item has finished, for its selected run's stop document to reach Tiled. Default
        is 30.
    poll_interval : float, optional
        Seconds between polls of Tiled and the RE Manager. Default is 1.

    Raises
    ------
    ValueError
        If ``timeout`` is negative or ``poll_interval`` is not positive.

    Examples
    --------
    .. code-block:: python

        runs = TiledQueueItemRuns(RM, tiled_client, run_filter=lambda start: not start.get("dark_frame"))
        evaluation_function = per_run(per_suggestion(score, TiledRunPoller(tiled_client)), runs.resolve)
        agent = QueueserverAgent(RM, ..., evaluation_function=evaluation_function)
    """

    def __init__(
        self,
        re_manager_api: "bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI",
        client: Container,
        *,
        run_filter: Callable[[RunStart], bool] | None = None,
        timeout: float = 30.0,
        poll_interval: float = 1.0,
    ) -> None:
        if timeout < 0:
            raise ValueError(f"timeout must be non-negative, got {timeout!r}.")
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be positive, got {poll_interval!r}.")
        self._rm = re_manager_api
        self._client = client
        self._run_filter = run_filter
        self._timeout = timeout
        self._poll_interval = poll_interval

    def resolve(self, item_uid: str) -> str:
        """Wait for the item's selected run to stop in Tiled and return its UID.

        An operator's ``re_stop`` also ends a run with ``"success"``, so the data reader must check it is complete.

        Parameters
        ----------
        item_uid : str
            Queue item UID, as the Queue Server runner passes to the evaluation function.

        Returns
        -------
        str
            UID of the item's selected run.

        Raises
        ------
        TimeoutError
            If the selected run has no stop document in Tiled ``timeout`` seconds after the item finished.
        RuntimeError
            If the selected run did not succeed, the item ended without opening a run, ``run_filter`` raised, or the
            item finished and none of its runs passes ``run_filter``.
        KeyError
            If the RE Manager has no record of the item.
        """
        passes: dict[str, bool] = {}  # run UID -> whether it passes run_filter; start documents never change
        run_uids: list[str] | None = None  # Set once the RE Manager reports that the item finished.
        deadline: float | None = None
        while True:
            runs = self._item_runs(item_uid)
            selected = next((run for run in runs if self._passes(item_uid, run["start"], passes)), None)
            if selected is not None and (stop := selected.get("stop")) is not None:
                run_uid = selected["start"]["uid"]
                if stop["exit_status"] != "success":
                    raise RuntimeError(
                        f"Run {run_uid!r} of queue item {item_uid!r} ended with status {stop['exit_status']!r}: "
                        f"{stop.get('reason') or '(no reason given)'}"
                    )
                return run_uid
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Queue item {item_uid!r} finished, but no selected run of it has a stop document in Tiled after "
                    f"{self._timeout} s. Check that the item's documents reach this Tiled container, e.g. through a "
                    "TiledWriter, and that the RE Manager runs bluesky-queueserver 0.0.26 or later, which records "
                    "start['queue_server']['queue_item_uid']."
                )
            if run_uids is None:
                run_uids = finished_item_run_uids(self._rm, item_uid)
                if run_uids is not None:
                    deadline = time.monotonic() + self._timeout
            # A finished item's runs are known: once all are in Tiled, no run that passes can still appear.
            if selected is None and run_uids is not None and set(run_uids) <= {run["start"]["uid"] for run in runs}:
                raise RuntimeError(f"Queue item {item_uid!r} finished, but none of its runs {run_uids!r} passes run_filter.")
            time.sleep(self._poll_interval)

    def _item_runs(self, item_uid: str) -> list[Mapping[str, Any]]:
        """Return the metadata of the item's runs in Tiled, in start order."""
        # Sort here rather than with Container.sort: an item opens few runs, and catalogs differ in the sort keys they
        # accept (bluesky-tiled-plugins stopped sorting by "start.time" because Mongo-backed catalogs reject it).
        # Read metadata rather than BlueskyRun.start/stop: plain containers lack them, and stop raises until the run stops.
        query = Eq(_QUEUE_ITEM_UID_KEY, item_uid)
        runs: list[Mapping[str, Any]] = [run.metadata for run in self._client.search(query).values()]
        return sorted(runs, key=lambda metadata: metadata["start"]["time"])

    def _passes(self, item_uid: str, start: RunStart, passes: dict[str, bool]) -> bool:
        """Return whether the run passes ``run_filter``, calling it at most once per run in a :meth:`resolve` call."""
        run_uid = start["uid"]
        if run_uid not in passes:
            try:
                passes[run_uid] = self._run_filter is None or self._run_filter(start)
            except Exception as error:
                raise RuntimeError(f"run_filter raised on run {run_uid!r} of queue item {item_uid!r}.") from error
        return passes[run_uid]
