"""
Passive ingestion: an agent learns from runs it did not submit.

.. warning::

    This module is **experimental**. The API is not yet stable and may change
    in future releases without a deprecation period.
"""

import logging
import queue
import threading
import time
from collections import Counter, deque
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ..protocols import Checkpointable, ContextFunction, Optimizer
from ..runs import CompletedRun, RunRouter
from .catalog import RunIngestor
from .points import validate_points
from .problem import ProblemDefinition

logger = logging.getLogger(__name__)

IngestionSource = Literal["subscription", "job"]
"""How a run reached ingestion: offered by the run router, or requested by UID."""

IngestionStatus = Literal["ingested", "rejected", "empty", "duplicate", "failed"]
"""What happened to a run."""


@dataclass(frozen=True)
class IngestionRecord:
    """
    The outcome of one run offered to ingestion.

    Parameters
    ----------
    run_uid : str
        The Bluesky run.
    source : {"subscription", "job"}
        How the run arrived.
    status : {"ingested", "rejected", "empty", "duplicate", "failed"}
        ``rejected``: ``accepts`` returned ``False``; ``empty``: the ingestor found no points;
        ``duplicate``: the run was already ingested.
    n_points : int
        Points ingested.
    error : str | None
        Why the run failed.
    received_at, finished_at : float
        Seconds since the epoch.
    """

    run_uid: str
    source: IngestionSource
    status: IngestionStatus
    n_points: int
    error: str | None
    received_at: float
    finished_at: float


@dataclass(frozen=True)
class _Job:
    run_uid: str
    source: IngestionSource
    start: Mapping[str, Any] | None
    received_at: float


class IngestionManager:
    """
    Feeds one agent's optimizer with points from runs it did not submit, on a worker thread.

    Each run UID is ingested at most once. Runs offered by the router go through
    :meth:`RunIngestor.accepts` first; runs requested with :meth:`jobs` skip it.

    Parameters
    ----------
    agent_id : str
        The agent, which is also the owner of the runs its runner claims; those are skipped.
    problem : ProblemDefinition
        The agent's problem, for validating points.
    ingestor : RunIngestor
        Turns a run into points.
    optimizer : Optimizer
        The agent's optimizer.
    optimizer_lock : threading.RLock
        Held for every optimizer call; shared with the agent's runner and API.
    router : RunRouter
        Router of the agent's queueserver.
    context_function : ContextFunction[str] | None, optional
        Features merged into every point of a run, for contextual agents.
    history_size : int, optional
        Number of run records kept.
    """

    def __init__(
        self,
        agent_id: str,
        problem: ProblemDefinition,
        ingestor: RunIngestor,
        optimizer: Optimizer,
        optimizer_lock: "threading.RLock",
        router: RunRouter,
        context_function: ContextFunction[str] | None = None,
        *,
        history_size: int = 200,
    ):
        self._agent_id = agent_id
        self._problem = problem
        self._ingestor = ingestor
        self._optimizer = optimizer
        self._optimizer_lock = optimizer_lock
        self._router = router
        self._context_function = context_function
        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        self._ingested: set[str] = set()
        self._records: deque[IngestionRecord] = deque(maxlen=history_size)
        self._counts: Counter[str] = Counter()
        self._token: int | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._work, name=f"{agent_id}-ingestion", daemon=True)
        self._thread.start()

    @property
    def subscribed(self) -> bool:
        """Whether runs from the router are offered to the agent."""
        with self._lock:
            return self._token is not None

    def subscribe(self) -> None:
        """Offer every completed run the agent did not submit. Idempotent."""
        with self._lock:
            if self._closed:
                raise RuntimeError("ingestion is closed")
            if self._token is None:
                self._token = self._router.subscribe(self._on_run)

    def unsubscribe(self) -> None:
        """Stop offering runs; queued runs are still processed. Idempotent."""
        with self._lock:
            token, self._token = self._token, None
        if token is not None:
            self._router.unsubscribe(token)

    def jobs(self, run_uids: Iterable[str]) -> int:
        """
        Ingest runs by UID, without :meth:`RunIngestor.accepts`.

        Parameters
        ----------
        run_uids : Iterable[str]
            Bluesky run UIDs.

        Returns
        -------
        int
            Number of runs queued.
        """
        now = time.time()
        uids = list(run_uids)
        with self._lock:
            if self._closed:
                raise RuntimeError("ingestion is closed")
        for uid in uids:
            self._queue.put(_Job(run_uid=uid, source="job", start=None, received_at=now))
        return len(uids)

    def mark_ingested(self, run_uid: str) -> None:
        """Record a run whose points reached the optimizer another way, e.g. the agent's own run."""
        with self._lock:
            self._ingested.add(run_uid)

    def summary(self) -> dict[str, Any]:
        """Subscription state, backlog and counts by status."""
        with self._lock:
            return {"subscribed": self._token is not None, "backlog": self._queue.qsize(), "counts": dict(self._counts)}

    def status(self) -> dict[str, Any]:
        """:meth:`summary` plus the most recent run records, oldest first."""
        with self._lock:
            records = [asdict(record) for record in self._records]
        return {**self.summary(), "runs": records}

    def close(self) -> None:
        """Unsubscribe and stop the worker after the run it is processing; queued runs are dropped."""
        self.unsubscribe()
        with self._lock:
            self._closed = True
        self._queue.put(None)

    def _on_run(self, run: CompletedRun, owner: str | None) -> None:
        """Router callback: queue runs this agent did not claim."""
        if owner == self._agent_id:
            return
        self._queue.put(_Job(run_uid=run.uid, source="subscription", start=run.start, received_at=time.time()))

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            with self._lock:
                closed = self._closed
            if job is None or closed:
                return
            try:
                status, n_points, error = self._process(job)
            except Exception as exc:
                logger.warning("Agent %s: ingesting run %s failed", self._agent_id, job.run_uid, exc_info=True)
                status, n_points, error = "failed", 0, f"{type(exc).__name__}: {exc}"
            record = IngestionRecord(
                run_uid=job.run_uid,
                source=job.source,
                status=status,
                n_points=n_points,
                error=error,
                received_at=job.received_at,
                finished_at=time.time(),
            )
            with self._lock:
                self._records.append(record)
                self._counts[status] += 1

    def _process(self, job: _Job) -> tuple[IngestionStatus, int, str | None]:
        with self._lock:
            if job.run_uid in self._ingested:
                return "duplicate", 0, None
        if job.start is not None and not self._ingestor.accepts(job.start):
            return "rejected", 0, None
        points = [dict(point) for point in self._ingestor(job.run_uid)]
        if not points:
            return "empty", 0, None
        if self._context_function is not None:
            features = dict(self._context_function(job.run_uid))
            points = [{**point, **features} for point in points]
        checked = validate_points(points, self._problem, allow_ids=False)
        with self._optimizer_lock:
            self._optimizer.ingest(checked)
            with self._lock:
                self._ingested.add(job.run_uid)
            if isinstance(self._optimizer, Checkpointable):
                try:
                    self._optimizer.checkpoint()
                except Exception:
                    # The points are ingested; a failed checkpoint only loses the on-disk copy.
                    logger.warning("Agent %s: checkpoint after run %s failed", self._agent_id, job.run_uid, exc_info=True)
        logger.info("Agent %s: ingested %d points from run %s", self._agent_id, len(checked), job.run_uid)
        return "ingested", len(checked), None
