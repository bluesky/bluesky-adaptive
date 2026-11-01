"""
Beamline alignment server: Bayesian optimization (Ax) of beam metrics measured on screens.

Clients compose problems from two catalog entries: the ``ax-bo`` optimizer defined here and the
shared ``image-metrics`` evaluation. Run from the repository root, with the deployment in
``prototypes/deploy`` up::

    pixi run -e integration uvicorn prototypes.alignment_server.app:app --port 8100

Settings come from environment variables; see :mod:`prototypes.common.settings`.
"""

import dataclasses
from pathlib import Path
from typing import Literal

from ax import ChoiceParameterConfig, RangeParameterConfig
from ax.exceptions.core import AxError, DataRequiredError, UnsupportedError
from pydantic import BaseModel, ConfigDict, Field

from bluesky_adaptive.ax.optimizer import AxOptimizer
from bluesky_adaptive.server import ChoiceDOFSpec, OptimizerEntry, ProblemDefinition, RangeDOFSpec, create_app
from prototypes.common.image_metrics import IMAGE_METRICS
from prototypes.common.settings import configure_logging, prototype_settings


class BOOptions(BaseModel):
    """
    Options of the ``ax-bo`` optimizer.

    Parameters
    ----------
    method : {"fast", "quality", "random_search"}
        Ax generation strategy preset.
    initialization_budget : int | None
        Quasi-random trials before the model takes over; Ax's default when ``None``.
    initialize_with_center : bool
        Whether the first trial is the center of the search space.
    seed : int | None
        Seed of Ax's random number generators.
    """

    model_config = ConfigDict(extra="forbid")

    method: Literal["fast", "quality", "random_search"] = "fast"
    initialization_budget: int | None = Field(default=None, ge=1)
    initialize_with_center: bool = True
    seed: int | None = None


def ax_objective(problem: ProblemDefinition) -> str:
    """
    Render the problem's objective in Ax's syntax.

    Parameters
    ----------
    problem : ProblemDefinition
        The validated problem.

    Returns
    -------
    str
        ``"a, -b"`` for objectives, where ``-`` marks a minimized one, or the canonical linear form of
        the scalarized objective. Ax maximizes a linear form, so a minimized one has its coefficients negated.
    """
    if problem.scalarized_objective is None:
        return ", ".join(f"-{objective.name}" if objective.minimize else objective.name for objective in problem.objectives)
    maximized = tuple(problem.objective_weights().items())
    return dataclasses.replace(problem.scalarized_objective, terms=maximized, minimize=False).canonical()


def ax_parameter(dof: RangeDOFSpec | ChoiceDOFSpec) -> RangeParameterConfig | ChoiceParameterConfig:
    """
    Describe a DOF as an Ax parameter.

    Parameters
    ----------
    dof : RangeDOFSpec | ChoiceDOFSpec
        The DOF; its name is the parameter name.

    Returns
    -------
    RangeParameterConfig | ChoiceParameterConfig
        The parameter.

    Raises
    ------
    ValueError
        If an integer DOF has fractional bounds.
    """
    if isinstance(dof, ChoiceDOFSpec):
        values: list = list(dof.values)
        return ChoiceParameterConfig(name=dof.name, values=values, parameter_type=dof.value_type, is_ordered=dof.is_ordered)
    lower, upper = dof.bounds
    if dof.value_type == "int":
        if not (lower.is_integer() and upper.is_integer()):
            raise ValueError(f"bounds of integer DOF {dof.name!r} must be whole numbers, got {dof.bounds}")
        return RangeParameterConfig(name=dof.name, bounds=(int(lower), int(upper)), parameter_type="int")
    return RangeParameterConfig(name=dof.name, bounds=(lower, upper), parameter_type="float")


def build_bo(problem: ProblemDefinition, options: BOOptions, checkpoint_path: Path) -> AxOptimizer:
    """
    Build an Ax optimizer for a problem.

    Parameters
    ----------
    problem : ProblemDefinition
        The validated problem.
    options : BOOptions
        Generation strategy and seed.
    checkpoint_path : Path
        Checkpoint location; written with the ``.json`` suffix.

    Returns
    -------
    AxOptimizer
        The optimizer, with its generation strategy configured.

    Raises
    ------
    ValueError
        If the problem has a context or Ax rejects it.
    """
    if problem.context_features:
        raise ValueError("ax-bo does not take a context; remove 'context' from the spec")
    parameters = [ax_parameter(dof) for dof in problem.dofs]
    try:
        optimizer = AxOptimizer(
            parameters=parameters,
            objective=ax_objective(problem),
            parameter_constraints=[constraint.canonical() for constraint in problem.dof_constraints],
            outcome_constraints=[constraint.canonical() for constraint in problem.outcome_constraints],
            checkpoint_path=str(checkpoint_path.with_suffix(".json")),
            client_kwargs={"random_seed": options.seed},
        )
        optimizer.ax_client.configure_generation_strategy(
            method=options.method,
            initialization_budget=options.initialization_budget,
            initialization_random_seed=options.seed,
            initialize_with_center=options.initialize_with_center,
        )
    except AxError as err:
        raise ValueError(f"Ax rejected the problem: {err}") from err
    return optimizer


BO: OptimizerEntry[BOOptions] = OptimizerEntry(
    name="ax-bo",
    version="1",
    options=BOOptions,
    build=build_bo,
    description="Bayesian optimization with Ax: objectives or a scalarized objective, linear constraints",
    # Ax raises UnsupportedError before the first trial, AssertionError from best points until a completed
    # trial is feasible, and DataRequiredError while a generation step waits for completed trials.
    no_data_errors=(AssertionError, DataRequiredError, UnsupportedError),
)
"""Catalog entry for :class:`~bluesky_adaptive.ax.optimizer.AxOptimizer`."""

configure_logging()
app = create_app(prototype_settings("alignment"), optimizers=[BO], evaluations=[IMAGE_METRICS])
