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
except ImportError as e:
    raise ImportError(
        "The queueserver integration requires additional dependencies. "
        "Install them with: pip install bluesky-adaptive[queueserver]"
    ) from e
from event_model import RunStart, RunStop

from .plans import default_acquire
from .protocols import (
    ID_KEY,
    CanRegisterSuggestions,
    DocumentCallback,
    QueueserverOptimizationProblem,
    TrialFaultAware,
)
from .records import RecordRun, _with_optimization_uid
from .utils import _maybe_checkpoint, _validate_outcomes, _validate_suggestions, collect_optimization_metadata

logger = logging.getLogger(__name__)


DEFAULT_ACQUIRE_PLAN_NAME: str = default_acquire.__name__
CORRELATION_UID_KEY: Literal["blop_correlation_uid"] = "blop_correlation_uid"


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


class ConsumerCallback(CallbackBase):
    """
    A callback that caches the start document and invokes a callback on stop.

    Parameters
    ----------
    callback : callable
        Function to call when a stop document is received.
        Signature: callback(start_doc, stop_doc)
    """

    def __init__(self, callback: Callable[[RunStart, RunStop], None] | None = None):
        super().__init__()
        self._start_doc_cache: dict[str, RunStart] = {}
        self._callback = callback

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

        Calls the callback with the cached start document and stop document pair.
        """
        start_doc = self._start_doc_cache.pop(doc["run_start"], None)
        if self._callback is not None and start_doc is not None:
            self._callback(start_doc, doc)


class QueueserverClient:
    """
    Handles communication with a Bluesky queueserver.

    This class encapsulates queueserver communication, including plan submission
    and event listening.

    .. warning::

        This class is part of the **experimental** queueserver integration.
        The API may change in future releases without a deprecation period.

    Parameters
    ----------
    re_manager_api : bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI
        Manager instance for communication with Bluesky Queueserver
    document_dispatcher : bluesky.callbacks.zmq.RemoteDispatcher
        Dispatcher for the Bluesky document stream.
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
        self._consumer_callback: ConsumerCallback | None = None
        self._listener_thread: threading.Thread | None = None

        response = self._rm.queue_autostart(autostart)
        logger.debug(f"Set queue autostart to {autostart}. Response: {response}")

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

    def submit_plan(self, plan: BPlan) -> None:
        """
        Submit a plan to the queueserver queue.

        Parameters
        ----------
        plan : BPlan
            The plan to submit.
        """
        response = self._rm.item_add(plan)
        logger.debug(f"Submitted plan to queue. Response: {response}")

    def start_listener(
        self,
        on_stop: Callable[[RunStart, RunStop], None],
    ) -> None:
        """
        Start listening for document events from the queueserver.

        Parameters
        ----------
        on_stop : callable
            Callback invoked when a stop document is received.
            Signature: on_stop(start_doc, stop_doc)
        """
        if self._listener_thread is not None:
            logger.warning("Listener already running")
            return

        self._consumer_callback = ConsumerCallback(callback=on_stop)
        self._dispatcher.subscribe(self._consumer_callback)

        logger.info("Starting document listener thread")
        self._listener_thread = threading.Thread(
            target=self._dispatcher.start,
            name="qserver-document-consumer",
            daemon=True,
        )
        self._listener_thread.start()

    def stop_listener(self) -> None:
        """Stop the document listener thread."""
        if self._listener_thread is not None:
            self._dispatcher.stop()
        self._consumer_callback = None
        self._listener_thread = None
        logger.info("Stopped document listener")


@dataclass
class _OptimizationState:
    """Internal mutable state for an optimization run."""

    max_iterations: int = 1
    num_points: int = 1
    checkpoint_interval: int | None = None
    current_iteration: int = 0
    current_suggestions: Sequence[Mapping] = field(default_factory=list)
    current_uid: str | None = None
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
    results when plans complete.

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
        The document listener is started once during runner construction so it
        is ready before any submitted plan can emit documents.
    callbacks : Sequence[DocumentCallback], optional
        Bluesky callbacks that receive the documents of each record run: one per :meth:`run` or
        :meth:`submit_suggestions` call, with one ``primary`` event per evaluated point (see
        :class:`bluesky_adaptive.records.RecordRun`). Held by reference; a change takes effect at
        the next call. Documents are emitted from the caller's thread and the document listener thread.
    optimization_uid : str | None, optional
        Identifier stored as ``start["adaptive"]["optimization_uid"]`` in the record runs and the
        submitted plans' metadata. If None, a new UUID4 is generated.
    """

    def __init__(
        self,
        optimization_problem: QueueserverOptimizationProblem,
        queueserver_client: QueueserverClient,
        *,
        callbacks: Sequence[DocumentCallback] = (),
        optimization_uid: str | None = None,
    ):
        self._problem = optimization_problem
        self._client = queueserver_client
        self._plan_name = optimization_problem.acquisition_plan or DEFAULT_ACQUIRE_PLAN_NAME
        self._state: _OptimizationState | None = None
        self._callbacks = callbacks
        self._optimization_uid = optimization_uid if optimization_uid is not None else str(uuid.uuid4())
        self._record: RecordRun | None = None
        self._continuous = True
        self._autostart = True
        self._state_lock = threading.RLock()
        self._current_future: Future[OptimizationResult] | None = None
        self._client.start_listener(
            on_stop=self._on_acquisition_complete,
        )

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
            iterations complete, or when :meth:`stop` is called. If the
            optimization loop raises an unhandled exception the future will
            hold that exception and re-raise it on ``.result()``.

        Raises
        ------
        RuntimeError
            If the queueserver environment is not ready.
        ValueError
            If required devices or plans are not available.
        """
        with self._state_lock:
            self._validate()
            self._state = _OptimizationState(
                max_iterations=iterations, num_points=num_points, checkpoint_interval=checkpoint_interval
            )
            self._continuous = True
            self._start_record(
                {
                    "plan_name": "QueueserverOptimizationRunner.run",
                    "iterations": iterations,
                    "n_points": num_points,
                    "checkpoint_interval": checkpoint_interval,
                }
            )
            self._state.current_iteration += 1
            self._state.current_suggestions = []
        try:
            suggestions = self._problem.optimizer.suggest(num_points)
            _validate_suggestions(suggestions)
            with self._state_lock:
                plan = self._build_plan(suggestions)
                logger.info(
                    f"Submitting iteration {self._state.current_iteration}/{self._state.max_iterations} "
                    f"with correlation uid: {self._state.current_uid}"
                )
                future: Future[OptimizationResult] = Future()
                self._current_future = future
            self._client.submit_plan(plan)
        except Exception as exc:
            with self._state_lock:
                self._fail_future(exc)
                self._stop_record_for(exc)
            raise
        return future

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
        _validate_suggestions(suggestions)

        with self._state_lock:
            self._start_record({"plan_name": "QueueserverOptimizationRunner.submit_suggestions", "suggestions": suggestions})
            self._state.current_iteration += 1
            self._state.current_suggestions = []
            plan = self._build_plan(suggestions)
            logger.info(f"Submitting manually specified suggestion(s) with correlation uid: {self._state.current_uid}")
            future: Future[OptimizationResult] = Future()
            self._current_future = future
        try:
            self._client.submit_plan(plan)
        except Exception as exc:
            with self._state_lock:
                self._fail_future(exc)
                self._stop_record_for(exc)
            raise
        return future

    def stop(self) -> None:
        """
        Stop the optimization loop gracefully.

        The future returned by :meth:`run` or :meth:`submit_suggestions` will
        be resolved with a partial :class:`OptimizationResult` containing however
        many iterations completed before the stop. The record run stops immediately,
        with reason ``"Stop requested"``. An acquisition that completes after the stop
        is ignored: it is not evaluated, ingested, or recorded.
        """
        with self._state_lock:
            self._continuous = False
            result = (
                self._state.build_result()
                if self._state is not None
                else OptimizationResult(iterations_completed=0, num_points=0, uids=())
            )
            # Resolved first, so a raising callback cannot leave the future pending.
            self._resolve_future(result)
            if self._record is not None:
                self._record.stop("success", "Stop requested")
        logger.info("Optimization stopped")

    def _start_record(self, metadata: Mapping[str, Any]) -> None:
        """LOCKED: Start the record run of one run() or submit_suggestions() call."""
        self._record = RecordRun(
            self._problem.optimizer,
            callbacks=self._callbacks,
            metadata={**collect_optimization_metadata(self._problem), "acquisition_plan": self._plan_name, **metadata},
            optimization_uid=self._optimization_uid,
        )

    def _stop_record_for(self, exc: BaseException) -> None:
        """LOCKED: Stop the record run for the exception that ended the optimization, if it is still running."""
        if self._record is not None and self._state is not None:
            self._record.stop_for(
                exc,
                self._state.current_iteration - 1 if self._continuous else -1,
                self._state.current_suggestions,
            )

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
                logger.exception("Failed to register trial failures with the optimizer")
                raise

    def _validate(self) -> None:
        """LOCKED: Validate not already running, queueserver environment, devices, and plan availability."""
        if self._current_future is not None and not self._current_future.done():
            raise RuntimeError("Optimization loop is already running.")
        self._client.check_environment()

    def _build_plan(self, suggestions: Sequence[Mapping]) -> BPlan:
        """LOCKED: Build the plan to submit and update current state."""
        if self._state is None:
            raise RuntimeError("_build_plan() called before run() or submit_suggestions()")

        self._state.current_suggestions = suggestions
        self._state.current_uid = str(uuid.uuid4())

        # Build metadata
        md = _with_optimization_uid(
            {CORRELATION_UID_KEY: self._state.current_uid, "blop_suggestions": self._state.current_suggestions},
            self._optimization_uid,
        )

        return BPlan(
            self._plan_name,
            self._state.current_suggestions,
            list(self._problem.actuators),
            list(self._problem.sensors),
            md=md,
            **(self._problem.acquisition_plan_kwargs or {}),
        )

    def _on_acquisition_complete(self, start_doc: RunStart, stop_doc: RunStop) -> None:
        """When acquisition finishes, ingest results, and maybe continue."""
        try:
            self._process_acquisition(start_doc, stop_doc)
        except Exception as exc:
            logger.exception(
                "Unhandled exception in optimization background thread. "
                "Optimization has been stopped. Inspect the future's exception for details."
            )
            suggestions: Sequence[Mapping] = []
            try:
                with self._state_lock:
                    suggestions = self._state.current_suggestions if self._state is not None else []
                    self._fail_future(exc)
                    # Stopped together with the future: once the future fails, run() may replace the record.
                    self._stop_record_for(exc)
            finally:
                self._try_register_failures(suggestions)

    def _process_acquisition(self, start_doc: RunStart, stop_doc: RunStop) -> None:
        """Core acquisition-complete logic (called from _on_acquisition_complete)."""
        run_filter = self._problem.acquisition_run_filter
        if run_filter is not None and not run_filter(start_doc):
            logger.info(f"Ignoring run {start_doc['uid']!r}: excluded by acquisition_run_filter")
            return
        with self._state_lock:
            if self._state is None or self._record is None:
                raise RuntimeError("_on_acquisition_complete called before run()")
            # Until this record stops, the runner's state belongs to this acquisition's optimization:
            # a new run() requires a done future, and the future completes together with the record.
            record = self._record
            if record.stopped:
                logger.info(f"Ignoring acquisition run {start_doc['uid']!r}: the optimization was stopped")
                return
            if self._state.current_uid is None:
                raise RuntimeError("current_uid not set")
            exit_status = stop_doc.get("exit_status")
            if self._state.current_uid != start_doc.get(CORRELATION_UID_KEY):
                raise RuntimeError(
                    "current_uid did not match start document. "
                    f"Got: {start_doc.get(CORRELATION_UID_KEY)}, Expected: {self._state.current_uid}. "
                    "If the acquisition plan opens more than one run with this metadata (for example, a "
                    "dark-frame run), set acquisition_run_filter to select the run to evaluate."
                )
            if exit_status != "success":
                reason = stop_doc.get("reason") or "(no reason given)"
                raise RuntimeError(f"Acquisition run {start_doc['uid']!r} ended with status {exit_status!r}: {reason}")
            logger.info(f"Acquisition complete for uid: {self._state.current_uid}")
            suggestions = self._state.current_suggestions
            self._state.uids.append(start_doc["uid"])

        # Evaluate the results
        outcomes = self._problem.evaluation_function(uid=start_doc["uid"], suggestions=suggestions)
        logger.info(f"Evaluated {len(outcomes)} outcomes")
        _validate_outcomes(outcomes, suggestions)

        with self._state_lock:
            if record.stopped:
                logger.info(f"Ignoring acquisition run {start_doc['uid']!r}: the optimization was stopped")
                return
            record.record(
                self._state.current_iteration - 1 if self._continuous else -1, start_doc["uid"], suggestions, outcomes
            )

        # Ingest into optimizer
        self._problem.optimizer.ingest(outcomes)

        with self._state_lock:
            if record.stopped:
                return
            # Checkpoint optimizer, if applicable
            _maybe_checkpoint(
                self._problem.optimizer, self._state.checkpoint_interval, iteration=self._state.current_iteration
            )
            # Check if done
            if not self._continuous or self._state.current_iteration >= self._state.max_iterations:
                logger.info(f"Optimization complete after {self._state.current_iteration} iterations")
                record.stop()
                self._resolve_future(self._state.build_result())
                return

            # Continue: start the next iteration, get suggestions, and submit
            self._state.current_iteration += 1
            self._state.current_suggestions = []
            num_points = self._state.num_points

        suggestions = self._problem.optimizer.suggest(num_points)
        _validate_suggestions(suggestions)
        with self._state_lock:
            if record.stopped:
                return
            plan = self._build_plan(suggestions)
            logger.info(
                f"Submitting iteration {self._state.current_iteration}/{self._state.max_iterations} "
                f"with correlation uid: {self._state.current_uid}"
            )
        self._client.submit_plan(plan)
