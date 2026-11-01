"""
Queueserver integration for running optimization through a Bluesky queueserver.

.. warning::

    This module is **experimental**. The API is not yet stable and may change
    in future releases without a deprecation period. It is not recommended for
    production use.

This module provides components for running optimization loops remotely through
a queueserver, rather than directly through a RunEngine. Completed runs arrive
through a :class:`~bluesky_adaptive.runs.RunRouter`, which hands each runner only
the runs it submitted.
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

try:
    import bluesky_queueserver_api.http
    import bluesky_queueserver_api.zmq
    from bluesky_queueserver_api import BPlan
    from bluesky_queueserver_api.comm_base import RequestFailedError
except ImportError as e:
    raise ImportError(
        "The queueserver integration requires additional dependencies. "
        "Install them with: pip install bluesky-adaptive[queueserver]"
    ) from e

from .plans import default_acquire
from .protocols import (
    ID_KEY,
    CanRegisterSuggestions,
    ContextAware,
    QueueserverOptimizationProblem,
    TrialFaultAware,
)
from .runs import CORRELATION_UID_KEY, CompletedRun, RunRouter
from .utils import _maybe_checkpoint, _validate_outcomes, _validate_suggestions

logger = logging.getLogger(__name__)


DEFAULT_ACQUIRE_PLAN_NAME: str = default_acquire.__name__
RunnerState = Literal["idle", "running", "stopping", "completed", "stopped", "failed"]
"""Lifecycle of a runner's most recent operation."""

_FINISHED_NORMALLY = ("completed", "stopped")
"""Queueserver exit statuses after which the plan's runs closed normally."""


@dataclass(frozen=True)
class OptimizationResult:
    """
    The result of a completed or stopped optimization run.

    .. warning::

        This class is part of the **experimental** queueserver integration.
        The API may change in future releases without a deprecation period.

    Parameters
    ----------
    iterations_completed : int
        The number of suggest -> acquire -> ingest cycles that finished
        successfully. For a run stopped early via :meth:`QueueserverOptimizationRunner.stop`,
        this reflects however many iterations completed before the stop.
    num_points : int
        The number of points suggested per iteration.
    uids : tuple[str, ...]
        The Bluesky run UIDs for each completed acquisition, in order.
        These can be used to retrieve raw data from a Tiled or databroker
        catalog for post-hoc analysis.
    """

    iterations_completed: int
    num_points: int
    uids: tuple[str, ...]


@dataclass(frozen=True)
class IterationRecord:
    """
    One suggest -> acquire -> ingest cycle, finished or in flight.

    .. warning::

        This class is part of the **experimental** queueserver integration.
        The API may change in future releases without a deprecation period.

    Parameters
    ----------
    iteration : int
        0-based index within its operation.
    correlation_uid : str
        The UID put in the run's start document to route the run back to this runner.
    item_uid : str | None
        The queueserver item UID, once submitted.
    suggestions : tuple[Mapping[str, Any], ...]
        The points submitted.
    context_uid : str | None
        The run whose context the suggestions were made in.
    context : Mapping[str, Any] | None
        The context features the suggestions were made in.
    run_uid : str | None
        The Bluesky run that acquired the suggestions.
    points : tuple[Mapping[str, Any], ...]
        The points ingested: outcomes, plus the run's context features for contextual problems.
    error : str | None
        Why the iteration failed or was abandoned.
    submitted_at : float
        Time the plan was submitted (seconds since the epoch).
    finished_at : float | None
        Time the iteration finished; ``None`` while in flight.
    """

    iteration: int
    correlation_uid: str
    item_uid: str | None
    suggestions: tuple[Mapping[str, Any], ...]
    context_uid: str | None
    context: Mapping[str, Any] | None
    run_uid: str | None
    points: tuple[Mapping[str, Any], ...]
    error: str | None
    submitted_at: float
    finished_at: float | None


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class QueueserverClient:
    """
    Submits plans to a Bluesky queueserver and reads their outcome from its history.

    Queue autostart is not touched; it is a deployment setting.

    .. warning::

        This class is part of the **experimental** queueserver integration.
        The API may change in future releases without a deprecation period.

    Parameters
    ----------
    re_manager_api : bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI
        Manager instance for communication with Bluesky Queueserver
    """

    def __init__(
        self,
        re_manager_api: bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI,
    ):
        self._rm = re_manager_api

    @property
    def re_manager_api(
        self,
    ) -> bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI:
        """The manager API this client uses."""
        return self._rm

    def check_environment(self) -> None:
        """
        Verify that the queueserver environment is ready.

        Raises
        ------
        RuntimeError
            If the queueserver environment is not open.
        """
        status = self._rm.status()
        if status is None or not status.get("worker_environment_exists", False):
            raise RuntimeError("The queueserver environment is not open")

    def submit_plan(self, plan: BPlan) -> str:
        """
        Submit a plan to the queueserver queue.

        Parameters
        ----------
        plan : BPlan
            The plan to submit.

        Returns
        -------
        str
            The queue item UID.

        Raises
        ------
        bluesky_queueserver_api.comm_base.RequestFailedError
            If the queueserver rejects the plan.
        """
        response = self._rm.item_add(plan)
        logger.debug("Submitted plan to queue. Response: %s", response)
        return response["item"]["item_uid"]

    def finished_item(self, item_uid: str) -> Mapping[str, Any] | None:
        """
        Return the history result of a finished queue item.

        Parameters
        ----------
        item_uid : str
            The queue item UID.

        Returns
        -------
        Mapping[str, Any] | None
            The item's ``result`` (``exit_status``, ``run_uids``, ``msg``, ``traceback``), or ``None``
            if the item is not in the history yet.
        """
        found: Mapping[str, Any] | None = None
        for item in self._rm.history_get().get("items") or []:
            if item.get("item_uid") == item_uid:
                found = item.get("result") or {}
        return found

    def remove_queued_item(self, item_uid: str) -> bool:
        """
        Remove an item that is still waiting in the queue.

        Parameters
        ----------
        item_uid : str
            The queue item UID.

        Returns
        -------
        bool
            ``False`` if the queueserver refused, e.g. because the item already started.
        """
        try:
            self._rm.item_remove(uid=item_uid)
        except RequestFailedError as err:
            logger.info("Queue item %s was not removed: %s", item_uid, err)
            return False
        return True


@dataclass
class _InFlight:
    """An iteration whose plan is submitted and whose run has not been processed."""

    iteration: int
    correlation_uid: str
    suggestions: tuple[Mapping[str, Any], ...]
    context_uid: str | None
    context: Mapping[str, Any] | None
    submitted_at: float
    item_uid: str | None = None
    completed_seen_at: float | None = None

    def record(
        self,
        *,
        run_uid: str | None = None,
        points: Sequence[Mapping] = (),
        error: str | None = None,
        finished: bool = True,
    ) -> IterationRecord:
        return IterationRecord(
            iteration=self.iteration,
            correlation_uid=self.correlation_uid,
            item_uid=self.item_uid,
            suggestions=self.suggestions,
            context_uid=self.context_uid,
            context=self.context,
            run_uid=run_uid,
            points=tuple(points),
            error=error,
            submitted_at=self.submitted_at,
            finished_at=time.time() if finished else None,
        )


@dataclass
class _Operation:
    """One call of :meth:`QueueserverOptimizationRunner.run` or ``submit_suggestions``."""

    iterations: int
    num_points: int
    checkpoint_interval: int | None
    future: Future[OptimizationResult] = field(default_factory=Future)
    submitted: int = 0
    uids: list[str] = field(default_factory=list)
    stop_requested: bool = False
    in_flight: _InFlight | None = None

    def result(self) -> OptimizationResult:
        return OptimizationResult(iterations_completed=len(self.uids), num_points=self.num_points, uids=tuple(self.uids))


class QueueserverOptimizationRunner:
    """
    Runs optimization loops through a Bluesky queueserver.

    Each iteration suggests points, submits one acquisition plan, and ingests the outcomes of
    the run that plan opened. Runs arrive from ``run_router`` and are processed on a worker
    thread owned by the runner, so the feed is never blocked.

    .. warning::

        This class is part of the **experimental** queueserver integration.
        The API may change in future releases without a deprecation period.

    Parameters
    ----------
    optimization_problem : QueueserverOptimizationProblem
        The optimization problem to solve, containing the optimizer, actuators,
        sensors, evaluation function and optional context function.
    queueserver_client : QueueserverClient
        Client for submitting plans.
    run_router : RunRouter
        Router for the queueserver's completed runs.
    name : str | None, optional
        Owner name used for run claims and logging.
    optimizer_lock : threading.RLock | None, optional
        Held for every optimizer call. Share it with every other caller of the optimizer.
    on_iteration : Callable[[IterationRecord], None] | None, optional
        Called after every finished or abandoned iteration. Errors are logged and ignored.
    poll_interval : float, optional
        Seconds between queueserver history checks for the in-flight plan.
    run_feed_timeout : float, optional
        Seconds to wait for the run feed after the queueserver reports the plan finished.

    Raises
    ------
    ValueError
        If the problem has a context function but its optimizer is not
        :class:`~bluesky_adaptive.protocols.ContextAware`.
    """

    def __init__(
        self,
        optimization_problem: QueueserverOptimizationProblem,
        queueserver_client: QueueserverClient,
        run_router: RunRouter,
        *,
        name: str | None = None,
        optimizer_lock: "threading.RLock | None" = None,
        on_iteration: Callable[[IterationRecord], None] | None = None,
        poll_interval: float = 1.0,
        run_feed_timeout: float = 60.0,
    ):
        if optimization_problem.context_function is not None and not isinstance(
            optimization_problem.optimizer, ContextAware
        ):
            raise ValueError("A problem with a context function needs a ContextAware optimizer")
        self._problem = optimization_problem
        self._client = queueserver_client
        self._router = run_router
        self._name = name or f"runner-{uuid.uuid4().hex[:8]}"
        self._plan_name = optimization_problem.acquisition_plan or DEFAULT_ACQUIRE_PLAN_NAME
        self._optimizer_lock = optimizer_lock or threading.RLock()
        self._on_iteration = on_iteration
        self._poll_interval = poll_interval
        self._run_feed_timeout = run_feed_timeout
        self._lock = threading.RLock()
        self._state: RunnerState = "idle"
        self._operation: _Operation | None = None
        self._closed = False
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{self._name}-worker")
        self._watchdog: threading.Thread | None = None
        self._wake = threading.Event()

    @property
    def optimization_problem(self) -> QueueserverOptimizationProblem:
        """The optimization problem being solved."""
        return self._problem

    @property
    def name(self) -> str:
        """Owner name used for run claims."""
        return self._name

    @property
    def optimizer_lock(self) -> threading.RLock:
        """The lock held for every optimizer call."""
        return self._optimizer_lock

    @property
    def state(self) -> RunnerState:
        """Lifecycle of the most recent operation."""
        return self._state

    @property
    def current_iteration(self) -> int:
        """The number of iterations submitted by the current or last operation (0 if none)."""
        with self._lock:
            return self._operation.submitted if self._operation is not None else 0

    @property
    def in_flight(self) -> IterationRecord | None:
        """A snapshot of the iteration whose run is awaited, with ``finished_at=None``."""
        with self._lock:
            in_flight = self._operation.in_flight if self._operation is not None else None
            return in_flight.record(finished=False) if in_flight is not None else None

    @property
    def future(self) -> Future[OptimizationResult] | None:
        """The future of the current or last operation, ``None`` before the first."""
        with self._lock:
            return self._operation.future if self._operation is not None else None

    @property
    def is_contextual(self) -> bool:
        """Whether suggestions depend on the context of a run."""
        return self._problem.context_function is not None

    def run(
        self,
        iterations: int = 1,
        num_points: int = 1,
        checkpoint_interval: int | None = None,
        context_uid: str | None = None,
    ) -> Future[OptimizationResult]:
        """
        Run the optimization loop.

        Suggests and submits the first iteration before returning; later iterations are
        submitted as runs complete.

        Parameters
        ----------
        iterations : int
            Number of optimization iterations to run.
        num_points : int
            Number of points to suggest per iteration.
        checkpoint_interval : int | None
            The number of iterations between optimizer checkpoints. If None, checkpoints
            will not be saved. Optimizer must implement the
            :class:`bluesky_adaptive.protocols.Checkpointable` protocol.
        context_uid : str | None
            Run whose context the first suggestions are made in. Required for contextual problems,
            rejected otherwise. Later iterations use the context of the runner's own previous run.

        Returns
        -------
        concurrent.futures.Future[OptimizationResult]
            Resolves when all iterations complete or after :meth:`stop`. Holds the exception if
            the loop fails.

        Raises
        ------
        RuntimeError
            If an operation is active, the runner is closed, or the queueserver environment is not open.
        ValueError
            If the context UID is missing or unexpected, or the suggestions are invalid.
        """
        if iterations < 1 or num_points < 1:
            raise ValueError(f"iterations and num_points must be >= 1, got {iterations} and {num_points}")
        self._check_can_start()
        context = self._context_for(context_uid)
        operation = self._begin(iterations, num_points, checkpoint_interval)
        try:
            suggestions = self._suggest(context, num_points)
        except Exception as exc:
            self._finish(operation, "failed", exc)
            raise
        self._submit(operation, suggestions, context_uid, context)
        return operation.future

    def submit_suggestions(self, suggestions: Sequence[Mapping]) -> Future[OptimizationResult]:
        """
        Acquire the given points in one iteration and ingest their outcomes.

        Parameters
        ----------
        suggestions : Sequence[Mapping]
            Points to acquire. Points without an ``"_id"`` are registered with the optimizer,
            which must then implement :class:`~bluesky_adaptive.protocols.CanRegisterSuggestions`;
            points with an ``"_id"`` (for example from ``suggest``) keep it.

        Returns
        -------
        concurrent.futures.Future[OptimizationResult]
            Resolves when the acquisition is ingested.

        Raises
        ------
        RuntimeError
            If an operation is active, the runner is closed, or the queueserver environment is not open.
        ValueError
            If no suggestions are given, or some lack an ``"_id"`` and the optimizer cannot register them.
        """
        points = [dict(suggestion) for suggestion in suggestions]
        if not points:
            raise ValueError("submit_suggestions needs at least one suggestion")
        unregistered = [index for index, point in enumerate(points) if ID_KEY not in point]
        optimizer = self._problem.optimizer
        if unregistered and not isinstance(optimizer, CanRegisterSuggestions):
            raise ValueError(
                f"All suggestions must contain an '{ID_KEY}' key to later match with the outcomes or your optimizer must "
                "implement the `bluesky_adaptive.protocols.CanRegisterSuggestions` protocol. Please review your optimizer "
                f"implementation. Got suggestions: {list(suggestions)}"
            )
        self._check_can_start()
        operation = self._begin(1, len(points), None)
        try:
            if unregistered and isinstance(optimizer, CanRegisterSuggestions):
                with self._optimizer_lock:
                    registered = optimizer.register_suggestions([points[index] for index in unregistered])
                for index, point in zip(unregistered, registered, strict=True):
                    points[index] = dict(point)
            _validate_suggestions(points)
        except Exception as exc:
            self._finish(operation, "failed", exc)
            raise
        self._submit(operation, points, None, None)
        return operation.future

    def stop(self) -> RunnerState:
        """
        Stop the optimization loop: submit nothing more.

        If the in-flight plan is still queued it is removed and the operation stops at once.
        Otherwise the state becomes ``"stopping"`` and the operation stops after the in-flight
        run is ingested. The future resolves with a partial :class:`OptimizationResult`.

        Returns
        -------
        RunnerState
            ``"stopped"`` or ``"stopping"``, or the current state if nothing is running.
        """
        with self._lock:
            operation = self._operation
            if operation is None or self._state not in ("running", "stopping"):
                return self._state
            operation.stop_requested = True
            self._state = "stopping"
            in_flight = operation.in_flight
            item_uid = in_flight.item_uid if in_flight is not None else None
        logger.info("%s: stop requested", self._name)
        if in_flight is None or item_uid is None or not self._client.remove_queued_item(item_uid):
            return self.state
        with self._lock:
            if operation.in_flight is not in_flight:
                return self._state
            operation.in_flight = None
        self._router.release(in_flight.correlation_uid)
        self._register_failures(in_flight.suggestions)
        self._emit(in_flight.record(error="removed from queue by stop()"))
        self._finish(operation, "stopped")
        return self.state

    def close(self) -> None:
        """Release run claims, fail any active operation and stop the runner's threads."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            operation = self._operation
            in_flight = operation.in_flight if operation is not None else None
            if operation is not None:
                operation.in_flight = None
        if in_flight is not None:
            self._router.release(in_flight.correlation_uid)
        if operation is not None:
            self._finish(operation, "failed", RuntimeError("runner closed"))
        self._wake.set()
        self._worker.shutdown(wait=False, cancel_futures=True)

    def _check_can_start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("runner closed")
            if self._state in ("running", "stopping"):
                raise RuntimeError("Optimization loop is already running.")

    def _context_for(self, context_uid: str | None) -> Mapping[str, Any] | None:
        context_function = self._problem.context_function
        if context_function is None:
            if context_uid is not None:
                raise ValueError(f"This problem has no context function; got context uid {context_uid!r}")
            return None
        if context_uid is None:
            raise ValueError("This problem is contextual; give the uid of the run that sets the context")
        return dict(context_function(context_uid))

    def _begin(self, iterations: int, num_points: int, checkpoint_interval: int | None) -> _Operation:
        self._client.check_environment()
        with self._lock:
            self._check_can_start()
            operation = _Operation(iterations=iterations, num_points=num_points, checkpoint_interval=checkpoint_interval)
            self._operation = operation
            self._state = "running"
        return operation

    def _suggest(self, context: Mapping[str, Any] | None, num_points: int) -> list[dict[str, Any]]:
        optimizer = self._problem.optimizer
        with self._optimizer_lock:
            if context is None:
                suggestions = optimizer.suggest(num_points)
            else:
                if not isinstance(optimizer, ContextAware):
                    raise TypeError("A contextual problem needs a ContextAware optimizer")
                suggestions = optimizer.suggest_in_context(context, num_points)
        _validate_suggestions(suggestions)
        return [dict(suggestion) for suggestion in suggestions]

    def _submit(
        self,
        operation: _Operation,
        suggestions: Sequence[Mapping[str, Any]],
        context_uid: str | None,
        context: Mapping[str, Any] | None,
    ) -> None:
        """Claim the run, then submit its plan. Re-raises submission errors after failing the operation."""
        correlation_uid = str(uuid.uuid4())
        in_flight: _InFlight | None = None
        with self._lock:
            if self._operation is not operation or operation.future.done():
                return
            if not operation.stop_requested:
                in_flight = _InFlight(
                    iteration=operation.submitted,
                    correlation_uid=correlation_uid,
                    suggestions=tuple(suggestions),
                    context_uid=context_uid,
                    context=dict(context) if context is not None else None,
                    submitted_at=time.time(),
                )
                operation.submitted += 1
                operation.in_flight = in_flight
                self._router.claim(correlation_uid, self._name, self._on_run)
        if in_flight is None:
            # Stopped while the suggestions were computed: abandon them.
            self._register_failures(suggestions)
            self._finish(operation, "stopped")
            return
        plan = BPlan(
            self._plan_name,
            list(suggestions),
            list(self._problem.actuators),
            list(self._problem.sensors),
            md={CORRELATION_UID_KEY: correlation_uid, "blop_suggestions": list(suggestions)},
            **(self._problem.acquisition_plan_kwargs or {}),
        )
        logger.info(
            "%s: submitting iteration %d/%d (correlation uid %s)",
            self._name,
            in_flight.iteration + 1,
            operation.iterations,
            correlation_uid,
        )
        try:
            item_uid = self._client.submit_plan(plan)
        except Exception as exc:
            with self._lock:
                if operation.in_flight is in_flight:
                    operation.in_flight = None
            self._router.release(correlation_uid)
            self._fail_iteration(operation, in_flight, None, exc)
            raise
        with self._lock:
            in_flight.item_uid = item_uid
            self._ensure_watchdog()

    def _on_run(self, run: CompletedRun) -> None:
        """Router callback: hand the run to the worker thread."""
        try:
            self._worker.submit(self._process_run, run)
        except RuntimeError:
            logger.debug("%s: closed; ignoring run %s", self._name, run.uid)

    def _process_run(self, run: CompletedRun) -> None:
        with self._lock:
            operation = self._operation
            in_flight = operation.in_flight if operation is not None else None
            if operation is None or in_flight is None or in_flight.correlation_uid != run.correlation_uid:
                logger.info("%s: ignoring run %s that is not in flight", self._name, run.uid)
                return
            operation.in_flight = None
        self._router.release(in_flight.correlation_uid)
        try:
            if run.exit_status != "success":
                reason = run.stop.get("reason") or "(no reason given)"
                raise RuntimeError(f"Acquisition run {run.uid!r} ended with status {run.exit_status!r}: {reason}")
            outcomes = self._problem.evaluation_function(run.uid, in_flight.suggestions)
            _validate_outcomes(outcomes, in_flight.suggestions)
            points: list[Mapping[str, Any]] = [dict(outcome) for outcome in outcomes]
            run_context: dict[str, Any] | None = None
            if self._problem.context_function is not None:
                run_context = dict(self._problem.context_function(run.uid))
                points = [{**point, **run_context} for point in points]
            with self._optimizer_lock:
                self._problem.optimizer.ingest(points)
                _maybe_checkpoint(self._problem.optimizer, operation.checkpoint_interval, iteration=in_flight.iteration)
        except Exception as exc:
            self._fail_iteration(operation, in_flight, run.uid, exc)
            return
        logger.info("%s: ingested %d points from run %s", self._name, len(points), run.uid)
        with self._lock:
            operation.uids.append(run.uid)
            done = operation.stop_requested or operation.submitted >= operation.iterations
        self._emit(in_flight.record(run_uid=run.uid, points=points))
        if done:
            self._finish(operation, "stopped" if operation.stop_requested else "completed")
            return
        try:
            suggestions = self._suggest(run_context, operation.num_points)
        except Exception as exc:
            logger.exception("%s: failed to suggest the next iteration", self._name)
            self._finish(operation, "failed", exc)
            return
        try:
            self._submit(operation, suggestions, run.uid if run_context is not None else None, run_context)
        except Exception:
            logger.exception("%s: failed to submit the next iteration", self._name)

    def _fail_iteration(self, operation: _Operation, in_flight: _InFlight, run_uid: str | None, exc: BaseException) -> None:
        logger.error("%s: iteration %d failed: %s", self._name, in_flight.iteration, _describe(exc))
        self._register_failures(in_flight.suggestions)
        self._emit(in_flight.record(run_uid=run_uid, error=_describe(exc)))
        self._finish(operation, "failed", exc)

    def _finish(self, operation: _Operation, state: RunnerState, exc: BaseException | None = None) -> None:
        with self._lock:
            if operation.future.done():
                return
            if self._operation is operation:
                self._state = state
        logger.info("%s: operation %s after %d iterations", self._name, state, len(operation.uids))
        if exc is not None:
            operation.future.set_exception(exc)
        else:
            operation.future.set_result(operation.result())

    def _register_failures(self, suggestions: Sequence[Mapping]) -> None:
        optimizer = self._problem.optimizer
        if suggestions and isinstance(optimizer, TrialFaultAware):
            try:
                with self._optimizer_lock:
                    optimizer.register_failures(suggestions)
            except Exception:
                # The iteration has already failed; its error is what the caller needs to see.
                logger.exception("%s: failed to register trial failures with the optimizer", self._name)

    def _emit(self, record: IterationRecord) -> None:
        if self._on_iteration is None:
            return
        try:
            self._on_iteration(record)
        except Exception:
            # A broken history hook must not break the optimization loop.
            logger.exception("%s: on_iteration hook failed", self._name)

    def _ensure_watchdog(self) -> None:
        """LOCKED: start the history watchdog if it is not running."""
        if self._watchdog is None and not self._closed:
            self._watchdog = threading.Thread(target=self._watch, name=f"{self._name}-watchdog", daemon=True)
            self._watchdog.start()

    def _watch(self) -> None:
        """Fail the in-flight iteration if the queueserver says its plan ended without a usable run."""
        while True:
            with self._lock:
                operation = self._operation
                if operation is None or operation.future.done() or self._closed:
                    self._watchdog = None
                    return
                in_flight = operation.in_flight
                item_uid = in_flight.item_uid if in_flight is not None else None
            if in_flight is not None and item_uid is not None:
                try:
                    error = self._check_item(in_flight, item_uid)
                except Exception:
                    logger.warning("%s: could not read the queueserver history", self._name, exc_info=True)
                    error = None
                if error is not None:
                    try:
                        self._worker.submit(self._fail_from_watchdog, operation, in_flight, error)
                    except RuntimeError:
                        return
            self._wake.wait(self._poll_interval)

    def _check_item(self, in_flight: _InFlight, item_uid: str) -> RuntimeError | None:
        result = self._client.finished_item(item_uid)
        if result is None:
            return None
        exit_status = result.get("exit_status")
        if exit_status not in _FINISHED_NORMALLY:
            message = result.get("msg") or "(no message)"
            return RuntimeError(f"Queue item {item_uid} finished with status {exit_status!r}: {message}")
        run_uids = list(result.get("run_uids") or [])
        if not run_uids:
            return RuntimeError(f"Queue item {item_uid} finished without opening a run")
        now = time.monotonic()
        if in_flight.completed_seen_at is None:
            in_flight.completed_seen_at = now
        elif now - in_flight.completed_seen_at > self._run_feed_timeout:
            return RuntimeError(
                f"Queue item {item_uid} finished, but the run feed delivered none of its runs {run_uids} "
                f"within {self._run_feed_timeout} s"
            )
        return None

    def _fail_from_watchdog(self, operation: _Operation, in_flight: _InFlight, error: RuntimeError) -> None:
        with self._lock:
            if operation.in_flight is not in_flight:
                return
            operation.in_flight = None
        self._router.release(in_flight.correlation_uid)
        self._fail_iteration(operation, in_flight, None, error)
