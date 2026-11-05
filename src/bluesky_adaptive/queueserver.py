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
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Literal

from bluesky.callbacks import CallbackBase
from bluesky.callbacks.zmq import RemoteDispatcher

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
from event_model import RunStart, RunStop

from .plans import default_acquire
from .protocols import ID_KEY, CanRegisterSuggestions, QueueserverOptimizationProblem, TrialFaultAware
from .utils import _maybe_checkpoint

logger = logging.getLogger(__name__)


DEFAULT_ACQUIRE_PLAN_NAME: str = default_acquire.__name__
CORRELATION_UID_KEY: Literal["blop_correlation_uid"] = "blop_correlation_uid"

RunCallback = Callable[[RunStart, RunStop], None]
"""Signature of a completed-run subscriber: ``callback(start_doc, stop_doc)``."""


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
class AcquisitionRequest:
    """
    An acquisition the runner is about to submit to the queueserver.

    Parameters
    ----------
    iteration : int
        The 1-based iteration number within the current run.
    correlation_uid : str
        The UID injected into the start document to route the acquisition's run back to the runner.
    suggestions : Sequence[Mapping]
        The suggestions to acquire, each with an ``"_id"``.
    """

    iteration: int
    correlation_uid: str
    suggestions: Sequence[Mapping]


@dataclass(frozen=True)
class IterationRecord:
    """
    The evaluated outcomes of one acquisition.

    Parameters
    ----------
    iteration : int
        The 1-based iteration number within the current run.
    run_uid : str
        The Bluesky run UID of the acquisition.
    suggestions : Sequence[Mapping]
        The acquired suggestions, each with an ``"_id"``.
    outcomes : Sequence[Mapping]
        The outcomes returned by the evaluation function, each with an ``"_id"``.
    """

    iteration: int
    run_uid: str
    suggestions: Sequence[Mapping]
    outcomes: Sequence[Mapping]


@dataclass(frozen=True)
class QueueItemResult:
    """
    How a finished queue item ended, as recorded in the queueserver history.

    Parameters
    ----------
    item_uid : str
        The queue item UID.
    exit_status : str
        The queueserver exit status, for example ``"completed"`` or ``"failed"``.
    run_uids : tuple[str, ...]
        The Bluesky runs the item opened.
    message : str
        The queueserver's message, for example the error that ended the plan.
    """

    item_uid: str
    exit_status: str
    run_uids: tuple[str, ...]
    message: str


class ConsumerCallback(CallbackBase):
    """
    A callback that caches Bluesky Adaptive start documents and passes completed runs to subscribers.

    Only runs whose start document carries a correlation UID are cached. Each subscriber
    receives every such run and decides itself whether the run is its own.
    """

    def __init__(self) -> None:
        super().__init__()
        self._start_doc_cache: dict[str, RunStart] = {}
        self._subscribers: tuple[RunCallback, ...] = ()
        self._lock = threading.Lock()

    def add(self, callback: RunCallback) -> None:
        """
        Add a subscriber.

        Raises
        ------
        ValueError
            If the callback is already subscribed.
        """
        with self._lock:
            if callback in self._subscribers:
                raise ValueError(f"Callback {callback!r} is already subscribed.")
            self._subscribers = (*self._subscribers, callback)

    def remove(self, callback: RunCallback) -> None:
        """Remove a subscriber. Removing an unknown subscriber does nothing."""
        with self._lock:
            self._subscribers = tuple(subscriber for subscriber in self._subscribers if subscriber != callback)

    def start(self, doc: RunStart) -> None:
        """
        Process the start document.

        Caches the start document if it has the correlation UID injected by Bluesky Adaptive.
        """
        if doc.get(CORRELATION_UID_KEY):
            self._start_doc_cache[doc["uid"]] = doc

    def stop(self, doc: RunStop) -> None:
        """
        Process the stop document.

        Passes the cached start document and stop document pair to every subscriber.
        """
        start_doc = self._start_doc_cache.pop(doc["run_start"], None)
        if start_doc is None:
            return
        for subscriber in self._subscribers:
            try:
                subscriber(start_doc, doc)
            except Exception:
                # One broken subscriber must not stop document delivery to the others.
                logger.exception("Run subscriber %r failed", subscriber)


class QueueserverClient:
    """
    Handles communication with a Bluesky queueserver.

    This class encapsulates queueserver communication, including plan submission
    and event listening. One client, and its document listener, can be shared by
    several runners: each runner subscribes to completed runs and ignores runs that
    are not its own.

    .. warning::

        This class is part of the **experimental** queueserver integration.
        The API may change in future releases without a deprecation period.

    Parameters
    ----------
    re_manager_api : bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI
        Manager instance for communication with Bluesky Queueserver
    document_dispatcher : bluesky.callbacks.zmq.RemoteDispatcher
        Dispatcher for the Bluesky document stream. The client starts it on the first
        subscription and stops it in :meth:`close`; a stopped dispatcher cannot be restarted.
    autostart : bool, optional
        Whether to enable queue autostart on the queueserver. Default is True.
    """

    def __init__(
        self,
        re_manager_api: bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI,
        document_dispatcher: RemoteDispatcher,
        *,
        autostart: bool = True,
    ):
        self._rm = re_manager_api
        self._dispatcher = document_dispatcher
        self._consumer_callback = ConsumerCallback()
        self._listener_lock = threading.Lock()
        self._listener_thread: threading.Thread | None = None

        response = self._rm.queue_autostart(autostart)
        logger.debug(f"Set queue autostart to {autostart}. Response: {response}")

    def status(self) -> Mapping[str, Any] | None:
        """Return the queueserver status, or ``None`` if the manager API has none."""
        return self._rm.status()

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
        RuntimeError
            If the manager API is configured not to raise and the queueserver rejects the plan.
        """
        response = self._rm.item_add(plan)
        logger.debug(f"Submitted plan to queue. Response: {response}")
        item = response.get("item") or {}
        if not response.get("success", True) or "item_uid" not in item:
            raise RuntimeError(f"The queueserver rejected plan {plan.name!r}: {response.get('msg') or '(no message)'}")
        return item["item_uid"]

    def finished_item(self, item_uid: str) -> QueueItemResult | None:
        """
        Look up a finished queue item in the queueserver history.

        Parameters
        ----------
        item_uid : str
            The queue item UID.

        Returns
        -------
        QueueItemResult | None
            How the item ended, or ``None`` if it is not in the history yet.
        """
        for item in self._rm.history_get().get("items") or []:
            if item.get("item_uid") == item_uid:
                result = item.get("result") or {}
                return QueueItemResult(
                    item_uid=item_uid,
                    exit_status=str(result.get("exit_status")),
                    run_uids=tuple(result.get("run_uids") or ()),
                    message=result.get("msg") or "",
                )
        return None

    def remove_item(self, item_uid: str) -> bool:
        """
        Remove an item that is still waiting in the queue.

        Parameters
        ----------
        item_uid : str
            The queue item UID.

        Returns
        -------
        bool
            ``True`` if the item was removed, ``False`` if the queueserver refused,
            for example because the item already started.
        """
        try:
            response = self._rm.item_remove(uid=item_uid)
        except RequestFailedError as err:
            logger.info(f"Queue item {item_uid} was not removed: {err}")
            return False
        return bool(response.get("success", True))

    def subscribe(self, on_stop: RunCallback) -> None:
        """
        Subscribe to completed Bluesky Adaptive runs, starting the document listener if needed.

        Parameters
        ----------
        on_stop : callable
            Called with ``(start_doc, stop_doc)`` on the listener thread for every run whose
            start document carries a correlation UID.
        """
        self._consumer_callback.add(on_stop)
        with self._listener_lock:
            if self._listener_thread is not None:
                return
            self._dispatcher.subscribe(self._consumer_callback)
            logger.info("Starting document listener thread")
            self._listener_thread = threading.Thread(
                target=self._dispatcher.start,
                name="qserver-document-consumer",
                daemon=True,
            )
            self._listener_thread.start()

    def unsubscribe(self, on_stop: RunCallback) -> None:
        """Stop passing completed runs to a subscriber. The document listener keeps running."""
        self._consumer_callback.remove(on_stop)

    def close(self) -> None:
        """Stop the document listener thread. The client cannot listen for documents afterwards."""
        with self._listener_lock:
            if self._listener_thread is None:
                return
            self._dispatcher.stop()
            self._listener_thread = None
        logger.info("Stopped document listener")


@dataclass
class _InFlight:
    """An acquisition submitted to the queueserver whose outcomes are not ingested yet."""

    iteration: int
    correlation_uid: str
    suggestions: Sequence[Mapping]
    item_uid: str | None = None
    claimed: bool = False
    """Set when the acquisition's documents arrive; only the claimant may resolve it."""
    finished: QueueItemResult | None = None
    finished_at: float | None = None


@dataclass
class _OptimizationState:
    """Internal mutable state for an optimization run."""

    future: Future[OptimizationResult]
    max_iterations: int = 1
    num_points: int = 1
    checkpoint_interval: int | None = None
    current_iteration: int = 0
    in_flight: _InFlight | None = None
    uids: list[str] = field(default_factory=list)
    stop_requested: bool = False
    finished: bool = False

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
    results when plans complete.

    Only runs carrying the correlation UID of the runner's in-flight acquisition are
    processed; other runs on the shared document stream are ignored. Suggestions that
    are never acquired, or whose acquisition fails, are reported to optimizers that
    implement :class:`~bluesky_adaptive.protocols.TrialFaultAware`.

    .. warning::

        This class is part of the **experimental** queueserver integration.
        The API may change in future releases without a deprecation period.

    Parameters
    ----------
    optimization_problem : QueueserverOptimizationProblem
        The optimization problem to solve, containing the optimizer, actuators,
        sensors, and evaluation function.
    queueserver_client : QueueserverClient
        Client for communicating with the queueserver. The runner subscribes to its
        completed runs on construction, so it is ready before any submitted plan can emit
        documents, and unsubscribes in :meth:`close`.
    optimizer_lock : threading.RLock | None, optional
        Lock held around every optimizer call. Share it with other code that uses the same
        optimizer, so that access is serialized. A private lock is created if not given.
    on_submit : callable, optional
        Called with an :class:`AcquisitionRequest` before each plan is submitted.
    on_evaluation : callable, optional
        Called with an :class:`IterationRecord` after evaluation and before ingestion, for
        example to save observations. An exception fails the acquisition and nothing is ingested.
    on_iteration : callable, optional
        Called with an :class:`IterationRecord` after the outcomes are ingested, before the optimizer
        is checkpointed. An exception stops the optimization; the outcomes stay ingested.
    history_poll_interval : float | None, optional
        Seconds between checks of the queueserver history for an in-flight plan that ended
        without delivering a stop document, for example a plan that failed before opening a
        run. ``None`` disables the checks. Default is 2 seconds.
    document_timeout : float, optional
        Seconds to wait for the stop document after the queueserver reports that the plan
        finished with runs. Default is 30 seconds.
    """

    def __init__(
        self,
        optimization_problem: QueueserverOptimizationProblem,
        queueserver_client: QueueserverClient,
        *,
        optimizer_lock: "threading.RLock | None" = None,
        on_submit: Callable[[AcquisitionRequest], None] | None = None,
        on_evaluation: Callable[[IterationRecord], None] | None = None,
        on_iteration: Callable[[IterationRecord], None] | None = None,
        history_poll_interval: float | None = 2.0,
        document_timeout: float = 30.0,
    ):
        self._problem = optimization_problem
        self._client = queueserver_client
        self._plan_name = optimization_problem.acquisition_plan or DEFAULT_ACQUIRE_PLAN_NAME
        self._optimizer_lock = optimizer_lock if optimizer_lock is not None else threading.RLock()
        self._on_submit = on_submit
        self._on_evaluation = on_evaluation
        self._on_iteration = on_iteration
        self._history_poll_interval = history_poll_interval
        self._document_timeout = document_timeout
        self._state: _OptimizationState | None = None
        self._state_lock = threading.RLock()
        self._watchdog: threading.Thread | None = None
        self._seen_history_uid: Any = None
        self._closed = threading.Event()
        self._client.subscribe(self._on_acquisition_complete)

    @property
    def optimization_problem(self) -> QueueserverOptimizationProblem:
        """The optimization problem being solved."""
        return self._problem

    @property
    def optimizer_lock(self) -> "threading.RLock":
        """The lock held around every optimizer call."""
        return self._optimizer_lock

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
        cycle. This method returns immediately; the optimization runs asynchronously
        via callbacks on the Bluesky document stream.

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
            iterations complete, or when :meth:`stop` takes effect. If the
            optimization loop raises an unhandled exception the future will
            hold that exception and re-raise it on ``.result()``.

        Raises
        ------
        RuntimeError
            If the runner is already running, the queueserver environment is not ready,
            or the queueserver rejects the plan.
        """
        state = self._begin(max_iterations=iterations, num_points=num_points, checkpoint_interval=checkpoint_interval)
        try:
            with self._optimizer_lock:
                suggestions = self._problem.optimizer.suggest(num_points)
        except Exception as exc:
            self._finish(state, exc)
            raise
        self._submit(state, suggestions, raise_errors=True)
        return state.future

    def submit_suggestions(self, suggestions: Sequence[Mapping]) -> Future[OptimizationResult]:
        """
        Manually submit suggestions to the queue.

        This method returns immediately; the optimization runs asynchronously via callbacks on the Bluesky document stream.

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

        Raises
        ------
        ValueError
            If a suggestion has no ``"_id"`` and the optimizer cannot register suggestions.
        RuntimeError
            If the runner is already running, the queueserver environment is not ready,
            or the queueserver rejects the plan.
        """
        optimizer = self._problem.optimizer
        registrar = optimizer if isinstance(optimizer, CanRegisterSuggestions) else None
        unregistered = [suggestion for suggestion in suggestions if ID_KEY not in suggestion]
        if unregistered and registrar is None:
            raise ValueError(
                f"All suggestions must contain an '{ID_KEY}' key to later match with the outcomes or your optimizer must "
                "implement the `bluesky_adaptive.protocols.CanRegisterSuggestions` protocol. Please review your optimizer "
                f"implementation. Got suggestions: {suggestions}"
            )
        state = self._begin(max_iterations=1, num_points=len(suggestions), checkpoint_interval=None)
        if registrar is not None and unregistered:
            # Suggestions that carry an "_id" are already registered; registering them again would duplicate trials.
            try:
                with self._optimizer_lock:
                    registered = iter(registrar.register_suggestions(unregistered))
            except Exception as exc:
                self._finish(state, exc)
                raise
            suggestions = [suggestion if ID_KEY in suggestion else next(registered) for suggestion in suggestions]
        self._submit(state, suggestions, raise_errors=True)
        return state.future

    def stop(self) -> None:
        """
        Stop the optimization loop gracefully.

        No further plans are submitted. If the in-flight plan is still waiting in the queue,
        it is removed and its suggestions are registered as failures; otherwise its outcomes
        are evaluated and ingested first. The future returned by :meth:`run` or
        :meth:`submit_suggestions` then resolves with an :class:`OptimizationResult`
        containing however many iterations completed.
        """
        with self._state_lock:
            state = self._state
            if state is None or state.finished:
                return
            state.stop_requested = True
            in_flight = state.in_flight
            item_uid = in_flight.item_uid if in_flight is not None and not in_flight.claimed else None
        if in_flight is None:
            self._finish(state)
        elif item_uid is not None and self._client.remove_item(item_uid):
            with self._state_lock:
                removed = state.in_flight is in_flight and not in_flight.claimed
                if removed:
                    state.in_flight = None
            if removed:
                logger.info(f"Removed queue item {item_uid} before it started")
                self._register_failures(in_flight.suggestions)
                self._finish(state)
        else:
            logger.info("Stop requested; waiting for the in-flight acquisition to be ingested")

    def close(self) -> None:
        """
        Stop listening for completed runs and stop checking the queueserver history.

        Call :meth:`stop` first and wait for the future if an acquisition is in flight; its
        outcomes are not ingested after the runner is closed.
        """
        self._closed.set()
        self._client.unsubscribe(self._on_acquisition_complete)

    def _begin(self, max_iterations: int, num_points: int, checkpoint_interval: int | None) -> _OptimizationState:
        """Validate that a new run can start and install its state."""
        with self._state_lock:
            if self._closed.is_set():
                raise RuntimeError("The runner is closed.")
            if self._state is not None and not self._state.finished:
                raise RuntimeError("Optimization loop is already running.")
            self._client.check_environment()
            self._state = _OptimizationState(
                future=Future(),
                max_iterations=max_iterations,
                num_points=num_points,
                checkpoint_interval=checkpoint_interval,
            )
            return self._state

    def _submit(self, state: _OptimizationState, suggestions: Sequence[Mapping], *, raise_errors: bool) -> None:
        """Submit an acquisition plan for the suggestions, or fail the run if that is impossible."""
        with self._state_lock:
            if state.finished or state.stop_requested:
                in_flight = None
            else:
                state.current_iteration += 1
                in_flight = _InFlight(
                    iteration=state.current_iteration, correlation_uid=str(uuid.uuid4()), suggestions=suggestions
                )
                state.in_flight = in_flight
        if in_flight is None:
            # Stopped while the suggestions were generated: they will never be acquired.
            self._register_failures(suggestions)
            self._finish(state)
            return

        logger.info(
            f"Submitting iteration {in_flight.iteration}/{state.max_iterations} "
            f"with correlation uid: {in_flight.correlation_uid}"
        )
        try:
            if self._on_submit is not None:
                self._on_submit(
                    AcquisitionRequest(
                        iteration=in_flight.iteration, correlation_uid=in_flight.correlation_uid, suggestions=suggestions
                    )
                )
            item_uid = self._client.submit_plan(self._build_plan(in_flight))
        except Exception as exc:
            self._release(state, in_flight)
            self._register_failures(suggestions, exc)
            self._finish(state, exc)
            if raise_errors:
                raise
            return

        with self._state_lock:
            in_flight.item_uid = item_uid
        self._ensure_watchdog()

    def _build_plan(self, in_flight: _InFlight) -> BPlan:
        """Build the acquisition plan for an in-flight acquisition."""
        md: dict[str, Any] = {
            CORRELATION_UID_KEY: in_flight.correlation_uid,
            "blop_suggestions": in_flight.suggestions,
        }
        return BPlan(
            self._plan_name,
            in_flight.suggestions,
            list(self._problem.actuators),
            list(self._problem.sensors),
            md=md,
            **(self._problem.acquisition_plan_kwargs or {}),
        )

    def _finish(self, state: _OptimizationState, exc: BaseException | None = None) -> None:
        """Resolve the run's future once, outside the state lock so done-callbacks may call back in."""
        with self._state_lock:
            if state.finished:
                return
            state.finished = True
            result = state.build_result()
        if exc is None:
            logger.info(f"Optimization finished after {result.iterations_completed} iterations")
            state.future.set_result(result)
        else:
            logger.error(f"Optimization failed: {exc!r}")
            state.future.set_exception(exc)

    def _register_failures(self, suggestions: Sequence[Mapping], error: BaseException | None = None) -> None:
        """Notify a TrialFaultAware optimizer of suggestions that will not be ingested."""
        optimizer = self._problem.optimizer
        if not suggestions or not isinstance(optimizer, TrialFaultAware):
            return
        try:
            with self._optimizer_lock:
                optimizer.register_failures(suggestions)
        except Exception as register_error:
            logger.exception("Failed to register trial failures with the optimizer")
            if error is not None:
                error.add_note(f"Registering the failed trials with the optimizer also failed: {register_error!r}")

    def _on_acquisition_complete(self, start_doc: RunStart, stop_doc: RunStop) -> None:
        """Process a completed run if it is the in-flight acquisition; ignore any other run."""
        with self._state_lock:
            state = self._state
            in_flight = state.in_flight if state is not None else None
            if (
                state is None
                or in_flight is None
                or in_flight.claimed
                or in_flight.correlation_uid != start_doc.get(CORRELATION_UID_KEY)
            ):
                logger.debug(f"Ignoring run {start_doc.get('uid')!r}: not this runner's in-flight acquisition")
                return
            in_flight.claimed = True
        try:
            self._process_acquisition(state, in_flight, start_doc, stop_doc)
        except Exception as exc:
            # Never let an error escape into the shared document listener thread.
            logger.exception("Unhandled exception in optimization background thread")
            self._finish(state, exc)

    def _process_acquisition(
        self, state: _OptimizationState, in_flight: _InFlight, start_doc: RunStart, stop_doc: RunStop
    ) -> None:
        """Evaluate and ingest a claimed acquisition, then submit the next one or finish."""
        run_uid = start_doc["uid"]
        iteration = in_flight.iteration
        try:
            exit_status = stop_doc.get("exit_status")
            if exit_status != "success":
                reason = stop_doc.get("reason") or "(no reason given)"
                raise RuntimeError(f"Acquisition run {run_uid!r} ended with status {exit_status!r}: {reason}")
            logger.info(f"Acquisition complete for run {run_uid}")
            outcomes = self._problem.evaluation_function(uid=run_uid, suggestions=in_flight.suggestions)
            logger.info(f"Evaluated {len(outcomes)} outcomes")
            record = IterationRecord(
                iteration=iteration, run_uid=run_uid, suggestions=in_flight.suggestions, outcomes=outcomes
            )
            if self._on_evaluation is not None:
                self._on_evaluation(record)
            with self._optimizer_lock:
                self._problem.optimizer.ingest(outcomes)
        except Exception as exc:
            self._release(state, in_flight)
            self._register_failures(in_flight.suggestions, exc)
            self._finish(state, exc)
            return

        # The outcomes are ingested; later errors must not register these suggestions as failures.
        with self._state_lock:
            state.uids.append(run_uid)
        try:
            if self._on_iteration is not None:
                self._on_iteration(record)
            with self._optimizer_lock:
                _maybe_checkpoint(self._problem.optimizer, state.checkpoint_interval, iteration=iteration)
        except Exception as exc:
            self._release(state, in_flight)
            self._finish(state, exc)
            return

        self._release(state, in_flight)
        with self._state_lock:
            done = state.finished or state.stop_requested or state.current_iteration >= state.max_iterations
        if done:
            self._finish(state)
            return
        try:
            with self._optimizer_lock:
                suggestions = self._problem.optimizer.suggest(state.num_points)
        except Exception as exc:
            self._finish(state, exc)
            return
        self._submit(state, suggestions, raise_errors=False)

    def _release(self, state: _OptimizationState, in_flight: _InFlight) -> None:
        """Clear the in-flight acquisition if it is still current."""
        with self._state_lock:
            if state.in_flight is in_flight:
                state.in_flight = None

    def _ensure_watchdog(self) -> None:
        """Start the history watchdog if it is enabled and not running."""
        if self._history_poll_interval is None:
            return
        with self._state_lock:
            if self._watchdog is not None:
                return
            self._watchdog = threading.Thread(target=self._watch, name="qserver-runner-watchdog", daemon=True)
            self._watchdog.start()

    def _watch(self) -> None:
        """Periodically check whether the in-flight plan ended without a stop document."""
        while True:
            with self._state_lock:
                state = self._state
                if self._closed.is_set() or state is None or state.finished:
                    # Cleared under the lock so _ensure_watchdog never sees a thread that is about to exit.
                    self._watchdog = None
                    return
            try:
                self._check_in_flight(state)
            except Exception:
                logger.warning("Could not check the queueserver history", exc_info=True)
            self._closed.wait(self._history_poll_interval)

    def _check_in_flight(self, state: _OptimizationState) -> None:
        """Fail the in-flight acquisition if the queueserver finished it but no stop document can arrive."""
        with self._state_lock:
            in_flight = state.in_flight
            if in_flight is None or in_flight.claimed or in_flight.item_uid is None:
                return
            item_uid = in_flight.item_uid
            finished = in_flight.finished
            finished_at = in_flight.finished_at

        if finished is None:
            # Fetch the history only when it changed.
            status = self._client.status()
            history_uid = status.get("plan_history_uid") if status is not None else None
            if history_uid is not None and history_uid == self._seen_history_uid:
                return
            self._seen_history_uid = history_uid
            finished = self._client.finished_item(item_uid)
            if finished is None:
                return
            finished_at = time.monotonic()
            with self._state_lock:
                in_flight.finished = finished
                in_flight.finished_at = finished_at

        if not finished.run_uids:
            error = RuntimeError(
                f"Queue item {item_uid} finished with status {finished.exit_status!r} without opening a run: "
                f"{finished.message or '(no message)'}"
            )
        elif finished_at is not None and time.monotonic() - finished_at > self._document_timeout:
            error = RuntimeError(
                f"Queue item {item_uid} finished with status {finished.exit_status!r}, but no stop document "
                f"for runs {list(finished.run_uids)} arrived within {self._document_timeout} s"
            )
        else:
            return

        with self._state_lock:
            if state.in_flight is not in_flight or in_flight.claimed:
                return
            state.in_flight = None
        self._register_failures(in_flight.suggestions, error)
        self._finish(state, error)
