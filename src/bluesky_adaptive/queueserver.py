"""
Queueserver integration for running optimization through a Bluesky queueserver.

.. warning::

    This module is **experimental**. The API is not yet stable and may change
    in future releases without a deprecation period. It is not recommended for
    production use.

This module provides components for running optimization loops remotely through
a queueserver, rather than directly through a RunEngine.
"""

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
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

_background_loop_lock = threading.Lock()
_background_loop: asyncio.AbstractEventLoop | None = None


def _get_background_loop() -> asyncio.AbstractEventLoop:
    """Return the event loop that runs optimizations started from synchronous code, starting it on first use."""
    global _background_loop
    with _background_loop_lock:
        if _background_loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="qserver-optimization-runner", daemon=True).start()
            _background_loop = loop
        return _background_loop


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

    The loop is an asyncio coroutine. :meth:`run_async` and :meth:`submit_suggestions_async` run it on the
    caller's event loop; :meth:`run` and :meth:`submit_suggestions` start it on a shared background event loop
    and return a :class:`concurrent.futures.Future`. Queueserver requests, optimizer calls, and the evaluation
    function are synchronous, so they run in worker threads via :func:`asyncio.to_thread` and never block the
    event loop. A runner executes one loop at a time.

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
        self._running = False

    @property
    def optimization_problem(self) -> QueueserverOptimizationProblem:
        """The optimization problem being solved."""
        return self._problem

    @property
    def current_iteration(self) -> int:
        """The current iteration number (0 if not running)."""
        return self._state.current_iteration if self._state else 0

    def run(
        self, iterations: int = 1, num_points: int = 1, checkpoint_interval: int | None = None
    ) -> Future[OptimizationResult]:
        """
        Run the optimization loop.

        Validates the queueserver state and submits the first plan, then returns while the
        suggest -> acquire -> ingest cycle continues on a background event loop that calls the evaluation
        function with each submitted plan's queue item UID. :meth:`run_async` is the coroutine version.

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
            will hold that exception and re-raise it on ``.result()``. Cancelling the
            future cancels the loop like cancelling :meth:`run_async`.

        Raises
        ------
        RuntimeError
            If the queueserver environment is not ready.
        ValueError
            If required devices or plans are not available, or the suggestions are invalid.
        """
        return self._run_in_background(lambda: self._start_run(iterations, num_points, checkpoint_interval))

    async def run_async(
        self, iterations: int = 1, num_points: int = 1, checkpoint_interval: int | None = None
    ) -> OptimizationResult:
        """
        Run the optimization loop on the running event loop and return its result.

        Coroutine version of :meth:`run` for callers with a running event loop, such as Jupyter or a
        service; parameters and errors are the same, and loop errors are raised instead of held in a
        future. Cancelling it abandons the in-flight acquisition: its plan stays queued, its evaluation
        runs to completion in its worker thread, and its outcomes are not ingested.
        """
        return await self._run(lambda: self._start_run(iterations, num_points, checkpoint_interval))

    def submit_suggestions(self, suggestions: Sequence[Mapping]) -> Future[OptimizationResult]:
        """
        Manually submit suggestions to the queue.

        This method returns once the plan is submitted; a background event loop calls the evaluation function
        with the plan's queue item UID and ingests the outcomes. :meth:`submit_suggestions_async` is the coroutine
        version.

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
        return self._run_in_background(lambda: self._start_submit(suggestions))

    async def submit_suggestions_async(self, suggestions: Sequence[Mapping]) -> OptimizationResult:
        """
        Manually submit suggestions to the queue on the running event loop and return the result.

        Coroutine version of :meth:`submit_suggestions` for callers with a running event loop, such as
        Jupyter or a service; parameters and errors are the same.
        """
        return await self._run(lambda: self._start_submit(suggestions))

    def stop(self) -> None:
        """
        Stop the optimization loop gracefully.

        No further plans are submitted, and the in-flight evaluation is not interrupted. After that evaluation
        returns and is ingested, the future returned by :meth:`run` or :meth:`submit_suggestions` resolves (and
        :meth:`run_async` or :meth:`submit_suggestions_async` returns) with a partial :class:`OptimizationResult`,
        or with the evaluation's exception. Plans already in the queue are not removed. Evaluation functions should
        bound their waits so that a stop takes effect. This method only sets a flag, so it can be called from any
        thread.
        """
        self._continuous = False
        logger.info("Optimization will stop after the in-flight acquisition is evaluated")

    def _claim(self) -> None:
        """Mark the runner as running, raising if a loop is already running."""
        if self._running:
            raise RuntimeError("Optimization loop is already running.")
        self._running = True

    def _run_in_background(self, start: Callable[[], Awaitable[str]]) -> Future[OptimizationResult]:
        """Run a loop on the background event loop, raising here any error before its first plan is submitted."""
        started: Future[None] = Future()
        future = asyncio.run_coroutine_threadsafe(self._run(start, started), _get_background_loop())
        pending: list[Future[Any]] = [started, future]
        try:
            wait(pending, return_when=FIRST_COMPLETED)
        except KeyboardInterrupt:
            future.cancel()  # Interrupted before the first plan was submitted, so abandon the run.
            raise
        if not started.done():
            future.result()  # The run failed before submitting its first plan.
        return future

    async def _run(self, start: Callable[[], Awaitable[str]], started: Future[None] | None = None) -> OptimizationResult:
        """Claim the runner, submit the first plan with ``start``, and run the loop, releasing the runner at the end."""
        self._claim()
        try:
            item_uid = await start()
            if started is not None:
                started.set_result(None)
            return await self._run_loop(item_uid)
        finally:
            self._running = False

    async def _start_run(self, iterations: int, num_points: int, checkpoint_interval: int | None) -> str:
        """Check the environment and submit the first plan of a run, returning its queue item UID."""
        await asyncio.to_thread(self._client.check_environment)
        self._state = _OptimizationState(
            max_iterations=iterations, num_points=num_points, checkpoint_interval=checkpoint_interval
        )
        self._continuous = True
        suggestions = await asyncio.to_thread(self._problem.optimizer.suggest, num_points)
        plan = self._build_plan(suggestions)
        logger.info(f"Submitting iteration {self._state.current_iteration}/{self._state.max_iterations}")
        return await asyncio.to_thread(self._client.submit_plan, plan)

    async def _start_submit(self, suggestions: Sequence[Mapping]) -> str:
        """Check the environment and submit a plan for manual suggestions, returning its queue item UID."""
        await asyncio.to_thread(self._client.check_environment)
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
            suggestions = await asyncio.to_thread(self.optimization_problem.optimizer.register_suggestions, suggestions)

        plan = self._build_plan(suggestions)
        logger.info("Submitting manually specified suggestion(s)")
        return await asyncio.to_thread(self._client.submit_plan, plan)

    def _try_register_failures(self, suggestions: Sequence[Mapping]) -> None:
        """Notify a TrialFaultAware optimizer of failed suggestions, if supported."""
        if suggestions and isinstance(self._problem.optimizer, TrialFaultAware):
            try:
                self._problem.optimizer.register_failures(suggestions)
            except Exception:
                # Logged, not raised: the original error must still propagate to the caller.
                logger.exception("Failed to register trial failures with the optimizer")

    def _build_plan(self, suggestions: Sequence[Mapping]) -> BPlan:
        """Build the plan to submit and update current state."""
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

    async def _run_loop(self, item_uid: str) -> OptimizationResult:
        """Evaluate, ingest, and resubmit until done, registering the in-flight suggestions as failed on error."""
        state = self._state
        if state is None:
            raise RuntimeError("_run_loop() called before run() or submit_suggestions()")
        try:
            next_item_uid: str | None = item_uid
            while next_item_uid is not None:
                next_item_uid = await self._process_acquisition(next_item_uid)
        except Exception:
            logger.exception("Unhandled exception in the optimization loop. Optimization has been stopped.")
            # Register failures before re-raising so a caller that observes the failure sees the trials failed.
            await asyncio.to_thread(self._try_register_failures, state.current_suggestions)
            raise
        return state.build_result()

    async def _process_acquisition(self, item_uid: str) -> str | None:
        """
        Evaluate and ingest one acquisition.

        Returns the next submitted queue item UID, or ``None`` when the loop is done.
        """
        if self._state is None:
            raise RuntimeError("_process_acquisition called before run()")
        suggestions = self._state.current_suggestions

        # Evaluate the results; the evaluation function waits for its data.
        logger.info(f"Evaluating queue item {item_uid}")
        outcomes = await asyncio.to_thread(self._problem.evaluation_function, uid=item_uid, suggestions=suggestions)
        _validate_outcomes(outcomes, suggestions)
        logger.info(f"Evaluated {len(outcomes)} outcomes")

        # Ingest into optimizer
        await asyncio.to_thread(self._problem.optimizer.ingest, outcomes)

        self._state.uids.append(item_uid)
        # Ingested suggestions are no longer in flight.
        self._state.current_suggestions = []
        # Checkpoint optimizer, if applicable
        await asyncio.to_thread(
            _maybe_checkpoint,
            self._problem.optimizer,
            self._state.checkpoint_interval,
            iteration=self._state.current_iteration,
        )
        # Check if done
        if not self._continuous or self._state.current_iteration >= self._state.max_iterations:
            logger.info(f"Optimization complete after {self._state.current_iteration} iterations")
            return None

        # Continue: get next suggestions and submit
        suggestions = await asyncio.to_thread(self._problem.optimizer.suggest, self._state.num_points)
        plan = self._build_plan(suggestions)
        logger.info(f"Submitting iteration {self._state.current_iteration}/{self._state.max_iterations}")
        return await asyncio.to_thread(self._client.submit_plan, plan)
