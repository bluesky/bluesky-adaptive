"""
An example optimization server built from ``bluesky_adaptive.server``.

The application declares two problem templates for the Himmelblau function:

- ``himmelblau-fixed`` fixes everything except the half-width of the search box.
- ``himmelblau`` lets clients compose the Ax optimizer: DOFs, objective, constraints, and
  generation strategy.

The evaluation function is simulated: it computes the Himmelblau function from the suggested
parameters instead of loading acquired data, so the example runs without data services. A real
evaluation function loads the data identified by ``uid``.

Serve without a queueserver (manual suggest/evaluate/ingest only)::

    pixi run -e dev-cpu uvicorn --app-dir examples server_app:app

Serve against the queueserver stack of the queueserver tutorial (``docs/source/tutorials/queueserver``),
whose startup namespace defines ``motor1``, ``motor2``, ``himmel_det``, and ``default_acquire``::

    BLUESKY_ADAPTIVE_QSERVER=tcp://localhost:60615 BLUESKY_ADAPTIVE_DOCUMENTS=localhost:5578 \\
        pixi run -e dev-cpu uvicorn --app-dir examples server_app:app

Connecting enables queue autostart on that queueserver.
"""

import os
from collections.abc import Mapping, Sequence
from typing import Any

from bluesky.callbacks.zmq import RemoteDispatcher
from bluesky_queueserver_api.zmq import REManagerAPI
from fastapi import FastAPI
from pydantic import BaseModel, Field

from bluesky_adaptive.ax.server import AxOptimizerSpec, ObjectiveSpec, RangeDOFSpec, ax_analysis_methods
from bluesky_adaptive.protocols import ID_KEY, QueueserverOptimizationProblem
from bluesky_adaptive.queueserver import QueueserverClient
from bluesky_adaptive.server import (
    Catalog,
    InMemoryOptimizationRepository,
    OptimizationManager,
    ProblemTemplate,
    create_app,
)

ACTUATORS = ("motor1", "motor2")
SENSORS = ("himmel_det",)
OUTCOMES = ("himmelblau",)


def simulated_himmelblau(uid: Any, suggestions: Sequence[Mapping]) -> Sequence[Mapping]:
    """Compute the Himmelblau function of each suggestion (simulated acquisition)."""
    return [
        {
            ID_KEY: suggestion[ID_KEY],
            "himmelblau": (suggestion["motor1"] ** 2 + suggestion["motor2"] - 11) ** 2
            + (suggestion["motor1"] + suggestion["motor2"] ** 2 - 7) ** 2,
        }
        for suggestion in suggestions
    ]


def _problem(spec: AxOptimizerSpec) -> QueueserverOptimizationProblem:
    return QueueserverOptimizationProblem(
        optimizer=spec.build(),
        actuators=[dof.name for dof in spec.dofs],
        sensors=list(SENSORS),
        evaluation_function=simulated_himmelblau,
        acquisition_plan="default_acquire",
    )


class FixedParams(BaseModel):
    """The only configurable part of the fixed problem."""

    half_width: float = Field(default=5.0, gt=0, le=6)


def build_fixed(params: FixedParams) -> QueueserverOptimizationProblem:
    """Minimize the Himmelblau function over a square box centered at the origin."""
    bounds = (-params.half_width, params.half_width)
    return _problem(
        AxOptimizerSpec(
            dofs=[RangeDOFSpec(name=name, bounds=bounds) for name in ACTUATORS],
            objectives=[ObjectiveSpec(name="himmelblau", minimize=True)],
        )
    )


class HimmelblauOptimizerSpec(AxOptimizerSpec.restricted(dofs=ACTUATORS, objectives=OUTCOMES)):
    """An Ax optimizer over the beamline's actuators and the outcomes the evaluation function returns."""


class ComposableParams(BaseModel):
    """A client-composed Ax optimizer."""

    optimizer: HimmelblauOptimizerSpec


def build_composable(params: ComposableParams) -> QueueserverOptimizationProblem:
    """Optimize with a client-composed Ax optimizer over the beamline's actuators."""
    return _problem(params.optimizer)


def build_app() -> FastAPI:
    """Create the example application, connecting to a queueserver if one is configured."""
    queueserver = None
    if control_address := os.environ.get("BLUESKY_ADAPTIVE_QSERVER"):
        host, port = os.environ.get("BLUESKY_ADAPTIVE_DOCUMENTS", "localhost:5578").rsplit(":", 1)
        queueserver = QueueserverClient(REManagerAPI(zmq_control_addr=control_address), RemoteDispatcher((host, int(port))))
    manager = OptimizationManager(
        problems=Catalog(
            [
                ProblemTemplate(
                    name="himmelblau-fixed",
                    params=FixedParams,
                    build=build_fixed,
                    description="Minimize the Himmelblau function; only the search half-width is configurable.",
                ),
                ProblemTemplate(
                    name="himmelblau",
                    params=ComposableParams,
                    build=build_composable,
                    description="Optimize the Himmelblau function with a client-composed Ax optimizer.",
                ),
            ]
        ),
        repository=InMemoryOptimizationRepository(),
        queueserver=queueserver,
        analyses=Catalog(ax_analysis_methods()),
    )
    health_checks = {"queueserver": queueserver.check_environment} if queueserver is not None else {}
    return create_app(manager, health_checks=health_checks, capabilities={"simulated_evaluation": True})


app = build_app()
