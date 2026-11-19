"""Evaluation helpers that read acquisition data from Tiled."""

import time
from collections.abc import Hashable
from typing import Any, Literal

try:
    from tiled.client.container import Container
except ImportError as e:
    raise ImportError(
        "The Tiled evaluation helpers require additional dependencies. "
        "Install them with: pip install bluesky-adaptive[tiled]"
    ) from e

from ..plans import ACQUISITION_ORDER_KEY

_PRIMARY_STREAM: Literal["primary"] = "primary"


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
