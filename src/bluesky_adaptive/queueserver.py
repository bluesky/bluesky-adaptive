"""
Queueserver integration for running optimization through a Bluesky queueserver.

.. warning::

    This module is **experimental**. The API is not yet stable and may change
    in future releases without a deprecation period. It is not recommended for
    production use.

This module provides components for running optimization loops remotely through
a queueserver, rather than directly through a RunEngine.
"""

import logging
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

try:
    import bluesky_queueserver_api.http
    import bluesky_queueserver_api.zmq
    from bluesky_queueserver_api import BPlan
except ImportError as e:
    raise ImportError(
        "The queueserver integration requires additional dependencies. "
        "Install them with: pip install bluesky-adaptive[queueserver]"
    ) from e

from .plans import default_acquire
from .protocols import ID_KEY, CanRegisterSuggestions, QueueserverOptimizationProblem, TrialFaultAware
from .utils import _maybe_checkpoint, _validate_outcomes, _validate_suggestions

logger = logging.getLogger(__name__)


DEFAULT_ACQUIRE_PLAN_NAME: str = default_acquire.__name__


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
        The queue item UIDs of the completed acquisitions, in order. Each item's ``history_get()`` entry
        lists the UIDs of the runs it opened in ``result["run_uids"]``.
    """

    iterations_completed: int
    num_points: int
    uids: tuple[str, ...]


class QueueserverClient:
    """
    Handles communication with a Bluesky queueserver.

    This class encapsulates queueserver communication: plan submission and
    environment checks. The queue must be running or have autostart enabled.

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
            The queue item UID assigned by the RE Manager.
        """
        response = self._rm.item_add(plan)
        logger.debug(f"Submitted plan to queue. Response: {response}")
        return response["item"]["item_uid"]


@dataclass
class _OptimizationState:
    """Internal mutable state for an optimization run."""

    max_iterations: int = 1
    num_points: int = 1
    checkpoint_interval: int | None = None
    current_iteration: int = 0
    current_suggestions: Sequence[Mapping] = field(default_factory=list)
    uids: list[str] = field(default_factory=list)

    def build_result(self) -> OptimizationResult:
        """Build an :class:`OptimizationResult` from the current state."""
        return OptimizationResult(
            iterations_completed=len(self.uids),
            num_points=self.num_points,
            uids=tuple(self.uids),
        )


class QueueserverOptimizationRunner:
    """
    Runs optimization loops through a Bluesky queueserver.

    This class coordinates the optimization workflow by getting suggestions from
    the optimizer, submitting acquisition plans to the queueserver, and ingesting
    the outcomes the evaluation function returns for each submitted plan.

    .. warning::

        This class is part of the **experimental** queueserver integration.
        The API may change in future releases without a deprecation period.

    Parameters
    ----------
    optimization_problem : QueueserverOptimizationProblem
        The optimization problem to solve, containing the optimizer, actuators,
        sensors, and evaluation function.
    queueserver_client : QueueserverClient
        Client for communicating with the queueserver.
    """

    def __init__(
        self,
        optimization_problem: QueueserverOptimizationProblem,
        queueserver_client: QueueserverClient,
    ):
        self._problem = optimization_problem
        self._client = queueserver_client
        self._plan_name = optimization_problem.acquisition_plan or DEFAULT_ACQUIRE_PLAN_NAME
        self._state: _OptimizationState | None = None
        self._continuous = True
        self._state_lock = threading.RLock()
        self._current_future: Future[OptimizationResult] | None = None

    @property
    def optimization_problem(self) -> QueueserverOptimizationProblem:
        """The optimization problem being solved."""
        return self._problem

    @property
    def current_iteration(self) -> int:
        """The current iteration number (0 if not running)."""
        with self._state_lock:
            return self._state.current_iteration if self._state else 0

    def run(
        self, iterations: int = 1, num_points: int = 1, checkpoint_interval: int | None = None
    ) -> Future[OptimizationResult]:
        """
        Run the optimization loop.

        Validates the queueserver state, then begins the suggest -> acquire -> ingest
        cycle. This method returns immediately; the optimization runs in a background
        thread that calls the evaluation function with each submitted plan's queue item UID.

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

        Returns
        -------
        concurrent.futures.Future[OptimizationResult]
            A future that resolves to an :class:`OptimizationResult` when all
            iterations complete, or after :meth:`stop` once the in-flight evaluation
            returns. If the optimization loop raises an unhandled exception the future
            will hold that exception and re-raise it on ``.result()``.

        Raises
        ------
        RuntimeError
            If the queueserver environment is not ready.
        ValueError
            If required devices or plans are not available, or the suggestions are invalid.
        """
        with self._state_lock:
            self._validate()
            self._state = _OptimizationState(
                max_iterations=iterations, num_points=num_points, checkpoint_interval=checkpoint_interval
            )
            self._continuous = True
        suggestions = self._problem.optimizer.suggest(num_points)
        with self._state_lock:
            plan = self._build_plan(suggestions)
            logger.info(f"Submitting iteration {self._state.current_iteration}/{self._state.max_iterations}")
            future: Future[OptimizationResult] = Future()
            self._current_future = future
        self._submit_and_start_worker(plan)
        return future

    def submit_suggestions(self, suggestions: Sequence[Mapping]) -> Future[OptimizationResult]:
        """
        Manually submit suggestions to the queue.

        This method returns immediately; the optimization runs in a background thread that calls the evaluation
        function with each submitted plan's queue item UID.

        Parameters
        ----------
        suggestions : Sequence[Mapping]
            Parameter combinations to evaluate. Can be:

            - Optimizer suggestions (with "_id" keys from suggest())
            - Manual points (without "_id", requires CanRegisterSuggestions protocol)

        Returns
        -------
        concurrent.futures.Future[OptimizationResult]
            A future that resolves to an :class:`OptimizationResult` when the
            acquisition completes. If an unhandled exception occurs the future
            will hold it and re-raise on ``.result()``.
        """
        with self._state_lock:
            self._validate()
            self._state = _OptimizationState(max_iterations=1, num_points=len(suggestions))
            self._continuous = False
        # Ensure all suggestions have an ID_KEY or register them with the optimizer
        if not isinstance(self.optimization_problem.optimizer, CanRegisterSuggestions) and any(
            ID_KEY not in suggestion for suggestion in suggestions
        ):
            raise ValueError(
                f"All suggestions must contain an '{ID_KEY}' key to later match with the outcomes or your optimizer must "
                "implement the `bluesky_adaptive.protocols.CanRegisterSuggestions` protocol. Please review your optimizer "
                f"implementation. Got suggestions: {suggestions}"
            )
        elif isinstance(self.optimization_problem.optimizer, CanRegisterSuggestions):
            suggestions = self.optimization_problem.optimizer.register_suggestions(suggestions)

        with self._state_lock:
            plan = self._build_plan(suggestions)
            logger.info("Submitting manually specified suggestion(s)")
            future: Future[OptimizationResult] = Future()
            self._current_future = future
        self._submit_and_start_worker(plan)
        return future

    def stop(self) -> None:
        """
        Stop the optimization loop gracefully.

        No further plans are submitted, and the in-flight evaluation is not interrupted. The future
        returned by :meth:`run` or :meth:`submit_suggestions` resolves with a partial
        :class:`OptimizationResult` after that evaluation returns and is ingested, or holds its
        exception if it raises. Plans already in the queue are not removed. Evaluation functions
        should bound their waits so that a stop takes effect.
        """
        with self._state_lock:
            self._continuous = False
        logger.info("Optimization will stop after the in-flight acquisition is evaluated")

    def _resolve_future(self, result: OptimizationResult) -> None:
        """LOCKED: Resolve the current future with a result if it is not already done."""
        if self._current_future is not None and not self._current_future.done():
            self._current_future.set_result(result)

    def _fail_future(self, exc: Exception) -> None:
        """LOCKED: Resolve the current future with an exception if it is not already done."""
        if self._current_future is not None and not self._current_future.done():
            self._current_future.set_exception(exc)

    def _try_register_failures(self, suggestions: Sequence[Mapping]) -> None:
        """Notify a TrialFaultAware optimizer of failed suggestions, if supported."""
        if suggestions and isinstance(self._problem.optimizer, TrialFaultAware):
            try:
                self._problem.optimizer.register_failures(suggestions)
            except Exception:
                # Logged, not raised: this runs at the top of the worker thread and the original error
                # must still fail the future.
                logger.exception("Failed to register trial failures with the optimizer")

    def _validate(self) -> None:
        """LOCKED: Validate not already running, queueserver environment, devices, and plan availability."""
        if self._current_future is not None and not self._current_future.done():
            raise RuntimeError("Optimization loop is already running.")
        self._client.check_environment()

    def _build_plan(self, suggestions: Sequence[Mapping]) -> BPlan:
        """LOCKED: Build the plan to submit and update current state."""
        if self._state is None:
            raise RuntimeError("_build_plan() called before run() or submit_suggestions()")
        _validate_suggestions(suggestions)

        self._state.current_iteration += 1
        self._state.current_suggestions = suggestions

        # Build metadata
        md: dict[str, Any] = {"blop_suggestions": self._state.current_suggestions}

        return BPlan(
            self._plan_name,
            self._state.current_suggestions,
            list(self._problem.actuators),
            list(self._problem.sensors),
            md=md,
            **(self._problem.acquisition_plan_kwargs or {}),
        )

    def _submit_and_start_worker(self, plan: BPlan) -> None:
        """Submit the first plan and start the worker thread that evaluates it, failing the future on error."""
        try:
            item_uid = self._client.submit_plan(plan)
        except Exception as exc:
            with self._state_lock:
                self._fail_future(exc)
            raise
        threading.Thread(target=self._run_loop, args=(item_uid,), name="qserver-optimization-runner", daemon=True).start()

    def _run_loop(self, item_uid: str) -> None:
        """Worker thread: evaluate, ingest, and resubmit until done, failing the future on error."""
        try:
            next_item_uid: str | None = item_uid
            while next_item_uid is not None:
                next_item_uid = self._process_acquisition(next_item_uid)
        except Exception as exc:
            logger.exception(
                "Unhandled exception in optimization background thread. "
                "Optimization has been stopped. Inspect the future's exception for details."
            )
            with self._state_lock:
                suggestions = self._state.current_suggestions if self._state is not None else []
            # Register failures before failing the future so a caller that observes the failure sees the trials failed.
            self._try_register_failures(suggestions)
            with self._state_lock:
                self._fail_future(exc)

    def _process_acquisition(self, item_uid: str) -> str | None:
        """
        Evaluate and ingest one acquisition.

        Returns the next submitted queue item UID, or ``None`` after resolving the future.
        """
        with self._state_lock:
            if self._state is None:
                raise RuntimeError("_process_acquisition called before run()")
            suggestions = self._state.current_suggestions

        # Evaluate the results; the evaluation function waits for its data.
        logger.info(f"Evaluating queue item {item_uid}")
        outcomes = self._problem.evaluation_function(uid=item_uid, suggestions=suggestions)
        _validate_outcomes(outcomes, suggestions)
        logger.info(f"Evaluated {len(outcomes)} outcomes")

        # Ingest into optimizer
        self._problem.optimizer.ingest(outcomes)

        with self._state_lock:
            self._state.uids.append(item_uid)
            # Ingested suggestions are no longer in flight.
            self._state.current_suggestions = []
            # Checkpoint optimizer, if applicable
            _maybe_checkpoint(
                self._problem.optimizer, self._state.checkpoint_interval, iteration=self._state.current_iteration
            )
            # Check if done
            if not self._continuous or self._state.current_iteration >= self._state.max_iterations:
                logger.info(f"Optimization complete after {self._state.current_iteration} iterations")
                self._resolve_future(self._state.build_result())
                return None

            # Continue: get next suggestions and submit
            num_points = self._state.num_points

        suggestions = self._problem.optimizer.suggest(num_points)
        with self._state_lock:
            plan = self._build_plan(suggestions)
            logger.info(f"Submitting iteration {self._state.current_iteration}/{self._state.max_iterations}")
        return self._client.submit_plan(plan)
