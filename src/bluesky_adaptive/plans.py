"""Bluesky plans for optimization."""

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from itertools import count
from typing import Any, Literal, cast

import bluesky.plans as bp
import bluesky.preprocessors as bpp
from bluesky.protocols import Readable
from bluesky.utils import Msg, MsgGenerator, plan

from .plan_stubs import list_scan_in_run, seq_read
from .protocols import (
    ID_KEY,
    AcquisitionPlan,
    Actuator,
    CanRegisterSuggestions,
    DocumentCallback,
    OptimizationProblem,
    Sensor,
    SupportsStoppingCriteria,
    TrialFaultAware,
    TUid,
)
from .records import RecordRun, _with_optimization_uid
from .utils import (
    _maybe_checkpoint,
    _reject_child_run_messages,
    _unpack_for_list_scan,
    _validate_outcomes,
    _validate_suggestions,
    collect_optimization_metadata,
    route_suggestions,
)

logger = logging.getLogger(__name__)

_DEFAULT_ACQUIRE_RUN_KEY: Literal["default_acquire"] = "default_acquire"
OPTIMIZE_IN_RUN_KEY: Literal["optimize_in_run"] = "optimize_in_run"


@plan
def default_acquire(
    suggestions: Sequence[Mapping],
    actuators: Sequence[Actuator],
    sensors: Sequence[Sensor] | None = None,
    md: Mapping[str, Any] | None = None,
    *,
    per_step: bp.PerStep | None = None,
    **kwargs: Any,
) -> MsgGenerator[str]:
    """
    Acquire data for optimization. Simply a list scan.

    Includes ``"blop_suggestions"`` metadata containing the routed suggestions for
    backwards compatibility and ``"blop_acquisition_order"`` containing IDs in actual scan order.
    Use those IDs, rather than positions in ``suggestions``, to associate acquired rows.

    Parameters
    ----------
    suggestions: Sequence[Mapping]
        A sequence of mappings, each containing the parameterization of a point to evaluate.
        Each mapping must contain a unique ``"_id"`` key used to associate the acquired
        data with its suggestion.
    actuators: Sequence[Actuator]
        The actuators to move and the inputs to move them to.
    sensors: Sequence[Sensor]
        The sensors that produce data to evaluate.
    md : Mapping[str, Any] | None, optional
        Metadata to attach to the start document.
    per_step: bp.PerStep | None, optional
        The plan to execute for each step of the scan.
    **kwargs: Any
        Additional keyword arguments to pass to the list_scan plan.

    Returns
    -------
    str
        The UID of the Bluesky run.

    See Also
    --------
    bluesky.plans.list_scan : The Bluesky plan to acquire data.
    """
    if sensors is None:
        sensors = []
    readables = [s for s in sensors if isinstance(s, Readable)]
    if len(readables) != len(sensors):
        logger.warning(f"Some sensors are not readable and will be ignored. Using only the readable sensors: {readables}")

    if len(suggestions) > 1:
        if all(isinstance(actuator, Readable) for actuator in actuators):
            current_position = yield from seq_read(cast(Sequence[Readable], actuators))
        else:
            current_position = None
        suggestions = route_suggestions(suggestions, starting_position=current_position)

    run_md = dict(md or {})
    run_md.update(
        {
            "blop_suggestions": suggestions,
            "blop_acquisition_order": [suggestion[ID_KEY] for suggestion in suggestions],
            "run_key": _DEFAULT_ACQUIRE_RUN_KEY,
        }
    )
    plan_args = _unpack_for_list_scan(suggestions, actuators)
    return (
        # TODO: fix argument type in bluesky.plans.list_scan
        yield from bpp.msg_mutator(
            bpp.set_run_key_wrapper(
                bp.list_scan(
                    readables,
                    *plan_args,  # type: ignore[arg-type]
                    per_step=per_step,
                    md=run_md,
                    **kwargs,
                ),
                _DEFAULT_ACQUIRE_RUN_KEY,
            ),
            lambda msg: None if msg.command == "checkpoint" else msg,
        )
    )


@plan
def optimize_step(
    optimization_problem: OptimizationProblem[TUid],
    n_points: int = 1,
    *args: Any,
    **kwargs: Any,
) -> MsgGenerator[tuple[TUid, Sequence[Mapping], Sequence[Mapping]]]:
    """
    Single step of the optimization loop.

    Parameters
    ----------
    optimization_problem : OptimizationProblem
        The optimization problem to solve.
    n_points : int, optional
        The number of points to suggest.

    Returns
    -------
    tuple[TUid, Sequence[Mapping], Sequence[Mapping]]
        The acquisition UID, suggestions, and outcomes of the step.
    """
    if optimization_problem.acquisition_plan is None:
        acquisition_plan = cast(AcquisitionPlan[TUid], default_acquire)
    else:
        acquisition_plan = optimization_problem.acquisition_plan
    optimizer = optimization_problem.optimizer
    actuators = optimization_problem.actuators
    suggestions = optimizer.suggest(n_points)
    _validate_suggestions(suggestions)
    try:
        uid = yield from acquisition_plan(suggestions, actuators, optimization_problem.sensors, *args, **kwargs)
        outcomes = optimization_problem.evaluation_function(uid, suggestions)
    except Exception:
        if isinstance(optimizer, TrialFaultAware):
            optimizer.register_failures(suggestions)
        raise

    _validate_outcomes(outcomes, suggestions)
    optimizer.ingest(outcomes)

    return uid, suggestions, outcomes


@plan
def optimize(
    optimization_problem: OptimizationProblem[TUid],
    iterations: int | None = 1,
    n_points: int = 1,
    checkpoint_interval: int | None = None,
    *,
    callbacks: Iterable[DocumentCallback] = (),
    optimization_uid: str | None = None,
    **kwargs: Any,
) -> MsgGenerator[None]:
    """
    Solve the optimization problem.

    Each call composes one record run (see :class:`bluesky_adaptive.records.RecordRun`) with one
    ``primary`` event per evaluated point, emitted after the outcomes are validated and before the
    optimizer ingests them. The record run is sent to ``callbacks``, not to the RunEngine.

    Parameters
    ----------
    optimization_problem : OptimizationProblem
        The optimization problem to solve.
    iterations : int | None, optional
        The maximum number of optimization iterations to run. If None, run until
        the optimizer's stopping criterion is met. An optimizer implementing
        :class:`bluesky_adaptive.protocols.SupportsStoppingCriteria` is required when None.
    n_points : int, optional
        The number of points to suggest per iteration.
    checkpoint_interval : int | None, optional
        The number of iterations between optimizer checkpoints. If None, checkpoints
        will not be saved. Optimizer must implement the
        :class:`bluesky_adaptive.protocols.Checkpointable` protocol.
    callbacks : Iterable[DocumentCallback], optional
        Bluesky callbacks that receive the record run's documents.
    optimization_uid : str | None, optional
        Identifier stored as ``start["adaptive"]["optimization_uid"]`` in the record run and passed
        to the acquisition plan in ``md``. If None, a new UUID4 is generated.
    **kwargs : Any
        Additional keyword arguments to pass to the acquisition plan.

    Raises
    ------
    ValueError
        If ``iterations`` is None and the optimizer does not implement
        :class:`bluesky_adaptive.protocols.SupportsStoppingCriteria`.

    See Also
    --------
    bluesky_adaptive.protocols.OptimizationProblem : The problem to solve.
    bluesky_adaptive.protocols.Checkpointable : The protocol for checkpointable objects.
    optimize_step : A single optimization step, without a record run.
    """
    optimizer = optimization_problem.optimizer
    if iterations is None and not isinstance(optimizer, SupportsStoppingCriteria):
        raise ValueError("iterations=None requires an optimizer that implements SupportsStoppingCriteria")
    if optimization_problem.acquisition_plan is None:
        acquisition_plan = cast(AcquisitionPlan[TUid], default_acquire)
    else:
        acquisition_plan = optimization_problem.acquisition_plan
    actuators = optimization_problem.actuators
    sensors = optimization_problem.sensors

    metadata = {
        **collect_optimization_metadata(optimization_problem),
        "plan_name": "optimize",
        "iterations": iterations,
        "n_points": n_points,
        "checkpoint_interval": checkpoint_interval,
    }
    record = RecordRun(optimizer, callbacks=callbacks, metadata=metadata, optimization_uid=optimization_uid)
    md = _with_optimization_uid(kwargs.pop("md", None), record.optimization_uid)
    iteration = 0
    suggestions: Sequence[Mapping] = []
    try:
        for iteration in count() if iterations is None else range(iterations):
            suggestions = []  # a failing suggest() must not report the previous iteration's IDs
            suggestions = optimizer.suggest(n_points)
            _validate_suggestions(suggestions)
            try:
                uid = yield from acquisition_plan(suggestions, actuators, sensors, md=md, **kwargs)
                outcomes = optimization_problem.evaluation_function(uid, suggestions)
            except Exception:
                if isinstance(optimizer, TrialFaultAware):
                    optimizer.register_failures(suggestions)
                raise
            _validate_outcomes(outcomes, suggestions)
            record.record(iteration, uid, suggestions, outcomes)
            optimizer.ingest(outcomes)

            if isinstance(optimizer, SupportsStoppingCriteria):
                stop_now, stop_reason = optimizer.should_stop()
                if stop_now:
                    reason = f"Stopping criterion met at iteration {iteration}: {stop_reason or 'No reason provided'}"
                    logger.info(reason)
                    record.stop("success", reason)
                    return

            # Possibly take a checkpoint of the optimizer state
            _maybe_checkpoint(optimizer, checkpoint_interval, iteration)

            # Bluesky checkpoint for mid-optimization pauses
            yield Msg("checkpoint")
    except BaseException as error:
        record.stop_for(error, iteration, suggestions)
        raise
    record.stop()


@plan
def optimize_in_run(
    optimization_problem: OptimizationProblem[TUid],
    iterations: int = 1,
    n_points: int = 1,
    checkpoint_interval: int | None = None,
    *,
    callbacks: Iterable[DocumentCallback] = (),
    optimization_uid: str | None = None,
    **kwargs: Any,
) -> MsgGenerator[None]:
    """Solve an optimization problem by evaluating acquisition documents inside one run.

    .. warning::

        This plan is **experimental**. Its API is not yet stable and may change in
        future releases without a deprecation period.

    The RunEngine run holds the raw data. Each call also composes one record run (see
    :class:`bluesky_adaptive.records.RecordRun`) with one ``primary`` event per evaluated point,
    sent to ``callbacks``. Both runs carry the same ``start["adaptive"]["optimization_uid"]``.

    Parameters
    ----------
    optimization_problem : OptimizationProblem
        The optimization problem to solve.
    iterations : int, optional
        The number of optimization iterations to run.
    n_points : int, optional
        The number of points to suggest per iteration.
    checkpoint_interval : int | None, optional
        The number of iterations between optimizer checkpoints. If None, checkpoints
        will not be saved. Optimizer must implement the
        :class:`bluesky_adaptive.protocols.Checkpointable` protocol.
    callbacks : Iterable[DocumentCallback], optional
        Bluesky callbacks that receive the record run's documents.
    optimization_uid : str | None, optional
        Identifier stored as ``start["adaptive"]["optimization_uid"]`` in the record run and the
        RunEngine run. If None, a new UUID4 is generated.
    **kwargs : Any
        Additional keyword arguments to pass to the acquisition plan.
    """
    optimization_uid = optimization_uid if optimization_uid is not None else str(uuid.uuid4())
    metadata = {
        **collect_optimization_metadata(optimization_problem),
        "plan_name": "optimize_in_run",
        "iterations": iterations,
        "n_points": n_points,
        "checkpoint_interval": checkpoint_interval,
    }

    optimizer = optimization_problem.optimizer
    actuators = optimization_problem.actuators
    acquisition_plan = (
        cast(AcquisitionPlan[TUid], list_scan_in_run)
        if optimization_problem.acquisition_plan is None
        else optimization_problem.acquisition_plan
    )

    @bpp.set_run_key_decorator(OPTIMIZE_IN_RUN_KEY)
    @bpp.run_decorator(md=_with_optimization_uid({**metadata, "run_key": OPTIMIZE_IN_RUN_KEY}, optimization_uid))
    def _optimize_in_run() -> MsgGenerator[None]:
        # Started inside the RunEngine run, so a failure to open that run leaves no unstopped record.
        record = RecordRun(optimizer, callbacks=callbacks, metadata=metadata, optimization_uid=optimization_uid)
        iteration = 0
        suggestions: Sequence[Mapping] = []
        try:
            for iteration in range(iterations):
                suggestions = []  # a failing suggest() must not report the previous iteration's IDs
                suggestions = optimizer.suggest(n_points)
                _validate_suggestions(suggestions)
                try:
                    uid = yield from _reject_child_run_messages(
                        acquisition_plan(suggestions, actuators, optimization_problem.sensors, **kwargs)
                    )
                    outcomes = optimization_problem.evaluation_function(uid, suggestions)
                    _validate_outcomes(outcomes, suggestions)
                except Exception:
                    if isinstance(optimizer, TrialFaultAware):
                        # TODO: Is it possible to be more fine-grained than this?
                        # Some suggestions may have been acquired/evaluated without issue.
                        optimizer.register_failures(suggestions)
                    raise

                record.record(iteration, uid, suggestions, outcomes)
                optimizer.ingest(outcomes)

                if isinstance(optimizer, SupportsStoppingCriteria):
                    stop_now, stop_reason = optimizer.should_stop()
                    if stop_now:
                        reason = f"Stopping criterion met at iteration {iteration}: {stop_reason or 'No reason provided'}"
                        logger.info(reason)
                        record.stop("success", reason)
                        return

                _maybe_checkpoint(optimizer, checkpoint_interval, iteration)
        except BaseException as error:
            record.stop_for(error, iteration, suggestions)
            raise
        record.stop()

    return (yield from _optimize_in_run())


@plan
def sample_suggestions(
    optimization_problem: OptimizationProblem[TUid],
    suggestions: Sequence[Mapping],
    *,
    callbacks: Iterable[DocumentCallback] = (),
    optimization_uid: str | None = None,
    **kwargs: Any,
) -> MsgGenerator[tuple[TUid, Sequence[Mapping], Sequence[Mapping]]]:
    """
    Evaluate specific parameter combinations.

    This plan acquires data for given suggestions and ingests results into the optimizer.
    Supports both optimizer-generated suggestions (with "_id") and manual points
    (without "_id", if optimizer implements CanRegisterSuggestions).

    Each call composes one record run (see :class:`bluesky_adaptive.records.RecordRun`) with one
    ``primary`` event per suggestion, recorded with ``adaptive_iteration = -1``.

    Parameters
    ----------
    optimization_problem : OptimizationProblem
        The optimization problem.
    suggestions : Sequence[Mapping]
        Parameter combinations to evaluate. Can be:

        - Optimizer suggestions (with "_id" keys from suggest())
        - Manual points (without "_id", requires CanRegisterSuggestions protocol)

    callbacks : Iterable[DocumentCallback], optional
        Bluesky callbacks that receive the record run's documents.
    optimization_uid : str | None, optional
        Identifier stored as ``start["adaptive"]["optimization_uid"]`` in the record run and passed
        to the acquisition plan in ``md``. If None, a new UUID4 is generated.
    **kwargs : Any
        Additional arguments for acquisition plan.

    Returns
    -------
    uid : TUid
        The acquisition UID returned by the acquisition plan.
    suggestions : Sequence[Mapping]
        Suggestions with "_id" keys.
    outcomes : Sequence[Mapping]
        Evaluated outcomes.

    Raises
    ------
    ValueError
        If suggestions lack "_id" and optimizer doesn't implement CanRegisterSuggestions.

    See Also
    --------
    optimize_step : Standard optimizer-driven step.
    bluesky_adaptive.protocols.CanRegisterSuggestions : Protocol for manual suggestions.
    """
    optimizer = optimization_problem.optimizer
    # Ensure the suggestions have an ID_KEY or register them with the optimizer
    if not isinstance(optimizer, CanRegisterSuggestions) and any(ID_KEY not in suggestion for suggestion in suggestions):
        raise ValueError(
            f"All suggestions must contain an '{ID_KEY}' key to later match with the outcomes or your optimizer must "
            "implement the `bluesky_adaptive.protocols.CanRegisterSuggestions` protocol. Please review your optimizer "
            f"implementation. Got suggestions: {suggestions}"
        )
    elif isinstance(optimizer, CanRegisterSuggestions):
        suggestions = optimizer.register_suggestions(suggestions)
    _validate_suggestions(suggestions)

    if optimization_problem.acquisition_plan is None:
        acquisition_plan = cast(AcquisitionPlan[TUid], default_acquire)
    else:
        acquisition_plan = optimization_problem.acquisition_plan
    metadata = {
        **collect_optimization_metadata(optimization_problem),
        "plan_name": "sample_suggestions",
        "suggestions": suggestions,
    }
    record = RecordRun(optimizer, callbacks=callbacks, metadata=metadata, optimization_uid=optimization_uid)
    md = _with_optimization_uid(kwargs.pop("md", None), record.optimization_uid)
    try:
        uid = yield from acquisition_plan(
            suggestions, optimization_problem.actuators, optimization_problem.sensors, md=md, **kwargs
        )
        outcomes = optimization_problem.evaluation_function(uid, suggestions)
        _validate_outcomes(outcomes, suggestions)
        record.record(-1, uid, suggestions, outcomes)
        optimizer.ingest(outcomes)
    except BaseException as error:
        record.stop_for(error, -1, suggestions)
        raise
    record.stop()
    return uid, suggestions, outcomes


def acquire_baseline(
    optimization_problem: OptimizationProblem[TUid],
    parameterization: Mapping[str, Any] | None = None,
    *,
    callbacks: Iterable[DocumentCallback] = (),
    optimization_uid: str | None = None,
    **kwargs: Any,
) -> MsgGenerator[None]:
    """
    Acquire a baseline reading. Useful for relative outcome constraints.

    Each call composes one record run (see :class:`bluesky_adaptive.records.RecordRun`) with one
    ``primary`` event for the baseline point, recorded with ``adaptive_iteration = -1``.

    Parameters
    ----------
    optimization_problem : OptimizationProblem
        The optimization problem to solve.
    parameterization : dict[str, Any] | None = None
        Move the DOFs to the given parameterization, if provided.
    callbacks : Iterable[DocumentCallback], optional
        Bluesky callbacks that receive the record run's documents.
    optimization_uid : str | None, optional
        Identifier stored as ``start["adaptive"]["optimization_uid"]`` in the record run and passed
        to the acquisition plan in ``md``. If None, a new UUID4 is generated.
    **kwargs : Any
        Additional keyword arguments to pass to the acquisition plan.

    See Also
    --------
    default_acquire : The default plan to acquire data.
    """
    actuators = optimization_problem.actuators
    if parameterization is None:
        if all(isinstance(actuator, Readable) for actuator in actuators):
            parameterization = yield from seq_read(cast(Sequence[Readable], actuators))
        else:
            raise ValueError(
                "All actuators must also implement the Readable protocol to acquire a baseline from current positions."
            )
    parameterization_copy = dict(parameterization)
    if ID_KEY not in parameterization:
        parameterization_copy[ID_KEY] = "baseline"
    optimizer = optimization_problem.optimizer
    if optimization_problem.acquisition_plan is None:
        acquisition_plan = cast(AcquisitionPlan[TUid], default_acquire)
    else:
        acquisition_plan = optimization_problem.acquisition_plan
    points = [parameterization_copy]
    metadata = {
        **collect_optimization_metadata(optimization_problem),
        "plan_name": "acquire_baseline",
        "suggestions": points,
    }
    record = RecordRun(optimizer, callbacks=callbacks, metadata=metadata, optimization_uid=optimization_uid)
    md = _with_optimization_uid(kwargs.pop("md", None), record.optimization_uid)
    try:
        uid = yield from acquisition_plan(points, actuators, optimization_problem.sensors, md=md, **kwargs)
        outcomes = optimization_problem.evaluation_function(uid, points)
        _validate_outcomes(outcomes, points)
        record.record(-1, uid, points, outcomes)
        optimizer.ingest([{**outcomes[0], **parameterization_copy}])
    except BaseException as error:
        record.stop_for(error, -1, points)
        raise
    record.stop()
