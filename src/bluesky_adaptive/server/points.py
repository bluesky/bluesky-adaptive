"""Checks and conversions for points and other values that cross the server's API."""

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from ..protocols import ID_KEY
from .problem import ProblemDefinition, RangeDOFSpec


def json_native(value: Any) -> Any:
    """
    Convert a value to JSON-native types.

    Numpy scalars and arrays become Python numbers and lists, tuples become lists, and non-finite
    floats become ``None``.

    Parameters
    ----------
    value : Any
        The value to convert, possibly nested in mappings and sequences.

    Returns
    -------
    Any
        The converted value.
    """
    if isinstance(value, Mapping):
        return {str(key): json_native(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_native(item) for item in value]
    if isinstance(value, np.ndarray):
        return json_native(value.tolist())
    if isinstance(value, np.generic):
        return json_native(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _check_value(key: str, value: Any) -> Any:
    value = json_native(value)
    if isinstance(value, bool | str):
        return value
    if isinstance(value, int | float):
        if not math.isfinite(value):
            raise ValueError(f"value of {key!r} must be finite, got {value!r}")
        return value
    raise ValueError(f"value of {key!r} must be a number, string or boolean, got {type(value).__name__}: {value!r}")


def validate_points(
    points: Sequence[Mapping[str, Any]], problem: ProblemDefinition, *, allow_ids: bool
) -> list[dict[str, Any]]:
    """
    Check points before they reach :meth:`~bluesky_adaptive.protocols.Optimizer.ingest`.

    Parameters
    ----------
    points : Sequence[Mapping[str, Any]]
        Points to ingest.
    problem : ProblemDefinition
        The agent's problem.
    allow_ids : bool
        Whether points may carry an ``"_id"``, completing a suggestion. Points without one must
        contain every DOF.

    Returns
    -------
    list[dict[str, Any]]
        The points with JSON-native values.

    Raises
    ------
    ValueError
        If a point has an unknown key, misses a DOF or context feature, has no output, or holds a
        value that is not a finite number, string or boolean.
    """
    dofs = set(problem.dof_names)
    outputs = set(problem.outputs)
    features = set(problem.context_features)
    allowed = dofs | outputs | features
    checked: list[dict[str, Any]] = []
    for index, point in enumerate(points):
        label = f"point {index}"
        unknown = sorted(set(point) - allowed - ({ID_KEY} if allow_ids else set()))
        if unknown:
            raise ValueError(
                f"{label} has unknown keys {unknown}; allowed: DOFs {sorted(dofs)}, outputs {sorted(outputs)}"
                + (f", context features {sorted(features)}" if features else "")
            )
        if ID_KEY not in point:
            missing_dofs = sorted(dofs - set(point))
            if missing_dofs:
                raise ValueError(f"{label} has no '{ID_KEY}' and misses DOFs {missing_dofs}")
        missing_features = sorted(features - set(point))
        if missing_features:
            raise ValueError(f"{label} misses context features {missing_features}")
        if not outputs & set(point):
            raise ValueError(f"{label} has no outputs; declared outputs: {sorted(outputs)}")
        checked.append({key: value if key == ID_KEY else _check_value(key, value) for key, value in point.items()})
    return checked


def validate_suggestions(suggestions: Sequence[Mapping[str, Any]], problem: ProblemDefinition) -> list[dict[str, Any]]:
    """
    Check points a client asks the agent to acquire: the plan moves actuators to these values.

    Parameters
    ----------
    suggestions : Sequence[Mapping[str, Any]]
        Points with a value for every DOF, and optionally the ``"_id"`` of a suggestion.
    problem : ProblemDefinition
        The agent's problem.

    Returns
    -------
    list[dict[str, Any]]
        The suggestions with JSON-native values.

    Raises
    ------
    ValueError
        If a suggestion has an unknown key, misses a DOF, or has a value outside the DOF's bounds or choices.
    """
    dofs = {dof.name: dof for dof in problem.dofs}
    checked: list[dict[str, Any]] = []
    for index, suggestion in enumerate(suggestions):
        label = f"suggestion {index}"
        unknown = sorted(set(suggestion) - set(dofs) - {ID_KEY})
        if unknown:
            raise ValueError(f"{label} has unknown keys {unknown}; DOFs: {sorted(dofs)}")
        missing = sorted(set(dofs) - set(suggestion))
        if missing:
            raise ValueError(f"{label} misses DOFs {missing}")
        point: dict[str, Any] = {ID_KEY: suggestion[ID_KEY]} if ID_KEY in suggestion else {}
        for name, dof in dofs.items():
            value = _check_value(name, suggestion[name])
            if isinstance(dof, RangeDOFSpec):
                lower, upper = dof.bounds
                if isinstance(value, bool | str) or not lower <= value <= upper:
                    raise ValueError(f"{label}: {name}={value!r} is outside the bounds [{lower}, {upper}]")
            elif value not in dof.values:
                raise ValueError(f"{label}: {name}={value!r} is not one of {list(dof.values)}")
            point[name] = value
        checked.append(point)
    return checked
