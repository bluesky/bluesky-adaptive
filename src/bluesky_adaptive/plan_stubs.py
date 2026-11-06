"""Bluesky plan stubs for optimization."""

import logging
from collections.abc import Hashable, Mapping, Sequence
from typing import Any, cast

import bluesky.plan_stubs as bps
import bluesky.plans as bp
from bluesky.protocols import Readable
from bluesky.utils import MsgGenerator, plan

from .protocols import Actuator, Optimizer, Sensor
from .utils import (
    _drop_run_control_messages,
    _suggestion_ids,
    _unpack_for_list_scan,
    _validate_suggestions,
    route_suggestions,
)

logger = logging.getLogger(__name__)


@plan
def seq_read(readables: Sequence[Readable], **kwargs: Any) -> MsgGenerator[dict[str, Any]]:
    """
    Read the current values of the given readables.

    Parameters
    ----------
    readables : Sequence[Readable]
        The readables to read.

    Returns
    -------
    dict[str, Any]
        A dictionary of the readable names and their current values.
    """
    results = {}
    for readable in readables:
        results[readable.name] = yield from bps.rd(readable, **kwargs)
    return results


@plan
def navigate_to_best(
    actuators: Sequence[Actuator],
    optimizer: Optimizer | None = None,
    parameterization: Mapping | None = None,
) -> MsgGenerator[None]:
    """
    Move actuators to the best point found during optimization.

    If no explicit parameterization is provided, queries the optimizer for its
    best point(s). For multi-objective optimizers that return multiple Pareto-optimal
    points, an explicit parameterization must be provided.

    Parameters
    ----------
    actuators : Sequence[Actuator]
        The actuators to move to the best parameterization.
    optimizer : Optimizer | None, optional
        The optimizer to query for the best point.
    parameterization : Mapping | None, optional
        Explicit parameterization to navigate to. If None, queries the optimizer's
        best point. For multi-objective problems, call ``optimizer.get_best_points()``
        to inspect the Pareto set and select one.

    Raises
    ------
    TypeError
        If both ``parameterization`` and ``optimizer`` arguments are ``None``.
    ValueError
        If the optimizer returns multiple Pareto-optimal points and no
        explicit ``parameterization`` is provided.
    """
    if parameterization is None:
        if optimizer is None:
            raise TypeError("Either pass an explicit parameterization or use an optimizer.")
        best_points = optimizer.get_best_points()
        if len(best_points) > 1:
            raise ValueError(
                f"The optimizer returned {len(best_points)} Pareto-optimal points. "
                "Please call optimizer.get_best_points() to inspect them and pass your "
                "chosen parameterization explicitly via the 'parameterization' argument."
            )
        _, parameterization, _ = best_points[0]

    actuator_by_name = {actuator.name: actuator for actuator in actuators}
    moves = []
    for name, value in parameterization.items():
        if name in actuator_by_name:
            moves.append(actuator_by_name[name])
            moves.append(value)

    if moves:
        yield from bps.mv(*moves)


@plan
def list_scan_in_run(
    suggestions: Sequence[Mapping],
    actuators: Sequence[Actuator],
    sensors: Sequence[Sensor] | None = None,
    *,
    per_step: bp.PerStep | None = None,
    **kwargs: Any,
) -> MsgGenerator[tuple[Hashable, ...]]:
    """Acquire suggestions inside an already-open Bluesky run.

    This plan moves through the suggestions, optionally reordering them for efficient motion,
    and executes a Bluesky list scan without opening a child run. The list scan's stage and
    unstage messages are preserved.

    .. warning::

        The single-run optimization API is **experimental**. This plan may change in
        future releases without a deprecation period.

    Parameters
    ----------
    suggestions : Sequence[Mapping]
        Suggested parameterizations to execute. Each suggestion must contain a hashable ``_id``.
    actuators : Sequence[Actuator]
        Actuators to move to the suggested positions.
    sensors : Sequence[Sensor] | None, optional
        Sensors that produce data to evaluate. Non-readable sensors are ignored.
    per_step : bp.PerStep | None, optional
        Bluesky list-scan step hook. Custom hooks may emit any number of events into any streams.
    **kwargs : Any
        Additional keyword arguments to pass to :func:`bluesky.plans.list_scan`.

    Returns
    -------
    tuple[Hashable, ...]
        Suggestion IDs in the order the suggestions were executed.

        This identifier intentionally does not encode stream names, event UIDs, event counts, or
        per-stream offsets. Custom ``per_step`` hooks may emit any number of events into any number
        of streams. The matching evaluation function is responsible for interpreting those documents
        and correlating them with these ordered suggestion IDs.

    Yields
    ------
    Msg
        Bluesky messages.
    """
    _validate_suggestions(suggestions)
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

    suggestion_ids = _suggestion_ids(suggestions)
    plan_args = _unpack_for_list_scan(suggestions, actuators)
    # TODO: fix argument type in bluesky.plans.list_scan
    yield from _drop_run_control_messages(
        bp.list_scan(
            readables,
            *plan_args,  # type: ignore[arg-type]
            per_step=per_step,
            **kwargs,
        )
    )

    return suggestion_ids
