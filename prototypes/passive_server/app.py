"""
Passive learning server: an MLP surrogate learns beam metrics from every run that fits its problem.

Clients compose problems from four catalog entries: the ``mlp-sgd`` optimizer, the shared ``image-metrics``
evaluation, the ``baseline-signals`` context and the ``bluesky-scans`` ingestion. Run from the repository root,
with the deployment in ``prototypes/deploy`` up::

    pixi run -e integration uvicorn prototypes.passive_server.app:app --port 8200

Settings come from environment variables; see :mod:`prototypes.common.settings`.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from bluesky_adaptive.server import ContextEntry, IngestionEntry, ProblemDefinition, create_app
from prototypes.common.image_metrics import IMAGE_METRICS, ImageMetricsParams, event_outcomes_from_run, image_metric_outputs
from prototypes.common.settings import configure_logging, prototype_settings
from prototypes.common.tiled_data import run_client, stream_column
from prototypes.passive_server.mlp import MLP_SGD


class BaselineSignalsParams(BaseModel):
    """
    Parameters of the ``baseline-signals`` context.

    Parameters
    ----------
    signals : list[str]
        Data keys whose readings are the context features, e.g. ``"dcm_c1-pitch"``.
    stream : str
        Stream holding the readings.
    reduce : {"first", "mean"}
        How the stream's readings become one value: the first one, taken when the run started, or their mean.
    """

    model_config = ConfigDict(extra="forbid")

    signals: list[str] = Field(min_length=1)
    stream: str = "baseline"
    reduce: Literal["first", "mean"] = "first"


class BaselineSignals:
    """
    Context function: a run's readings of the configured signals.

    Parameters
    ----------
    params : BaselineSignalsParams
        Signals, stream and reduction.
    run_loader : Callable[[str], Any]
        Returns the run container for a UID; defaults to Tiled.
    """

    def __init__(self, params: BaselineSignalsParams, run_loader: Callable[[str], Any] = run_client):
        self.params = params
        self._run_loader = run_loader

    def __call__(self, uid: str) -> dict[str, float]:
        """
        Return ``{signal: value}`` for run ``uid``.

        Raises
        ------
        KeyError
            If the run, the stream or a signal is missing, or the stream has no readings.
        """
        run = self._run_loader(uid)
        features = {}
        for signal in self.params.signals:
            readings = stream_column(run, self.params.stream, signal)
            if readings.size == 0:
                raise KeyError(f"run {uid!r} has no {signal!r} readings in stream {self.params.stream!r}")
            features[signal] = float(readings[0] if self.params.reduce == "first" else np.mean(readings))
        return features


def build_baseline_signals(problem: ProblemDefinition, params: BaselineSignalsParams) -> BaselineSignals:
    """Build the ``baseline-signals`` context function; it needs nothing from the problem."""
    return BaselineSignals(params)


BASELINE_SIGNALS: ContextEntry[BaselineSignalsParams] = ContextEntry(
    name="baseline-signals",
    version="1",
    params=BaselineSignalsParams,
    build=build_baseline_signals,
    features=lambda params: params.signals,
    description="Context features read from a run's baseline stream, such as offsets other users set",
)
"""Catalog entry for :class:`BaselineSignals`."""

DEFAULT_PLAN_NAMES = ("scan", "list_scan", "rel_scan", "adaptive_acquire")
"""Step plans whose events pair DOF readbacks with detector images."""


class BlueskyScansParams(BaseModel):
    """
    Parameters of the ``bluesky-scans`` ingestion.

    Parameters
    ----------
    plan_names : list[str]
        Plans whose runs may hold points, matched against the start document's ``plan_name``.
    metrics : ImageMetricsParams | None
        Metrics to compute per event; ``None`` reuses the params of the agent's ``image-metrics`` evaluation.
    """

    model_config = ConfigDict(extra="forbid")

    plan_names: list[str] = Field(default_factory=lambda: list(DEFAULT_PLAN_NAMES), min_length=1)
    metrics: ImageMetricsParams | None = None


class BlueskyScansIngestor:
    """
    Points from step scans: per event, the DOF readbacks and the image metrics.

    A run fits when one of the plans made it, it moved exactly the agent's DOFs, so no axis the model
    ignores changed during the run, and it read every sensor of the agent.

    Parameters
    ----------
    dofs : Sequence[str]
        DOF names, which are also the motors' data keys.
    sensors : Sequence[str]
        Sensor device paths; their ophyd names must be among the run's detectors.
    plan_names : Sequence[str]
        Accepted plans.
    metrics : ImageMetricsParams
        Metrics to compute per event.
    run_loader : Callable[[str], Any]
        Returns the run container for a UID; defaults to Tiled.
    """

    def __init__(
        self,
        dofs: Sequence[str],
        sensors: Sequence[str],
        plan_names: Sequence[str],
        metrics: ImageMetricsParams,
        run_loader: Callable[[str], Any] = run_client,
    ):
        self._dofs = tuple(dofs)
        self._detectors = {sensor.replace(".", "-") for sensor in sensors}
        self._plan_names = frozenset(plan_names)
        self.metrics = metrics
        self._run_loader = run_loader

    def accepts(self, start: Mapping[str, Any]) -> bool:
        """Return whether the run's plan, motors and detectors fit the agent."""
        return (
            start.get("plan_name") in self._plan_names
            and set(start.get("motors") or ()) == set(self._dofs)
            and self._detectors <= set(start.get("detectors") or ())
        )

    def __call__(self, uid: str) -> list[dict[str, Any]]:
        """
        Return one point per event of run ``uid``: DOF readbacks and metrics.

        Raises
        ------
        KeyError
            If the run, a DOF column or an image is missing.
        ValueError
            If the DOF columns and the images have different numbers of events.
        """
        run = self._run_loader(uid)
        columns = {name: stream_column(run, self.metrics.stream, name) for name in self._dofs}
        rows = event_outcomes_from_run(run, self.metrics)
        lengths = {name: len(column) for name, column in columns.items()}
        if any(length != len(rows) for length in lengths.values()):
            raise ValueError(f"run {uid!r} has {len(rows)} image events but DOF readings {lengths}")
        return [{**{name: columns[name][index].item() for name in self._dofs}, **row} for index, row in enumerate(rows)]


def build_bluesky_scans(problem: ProblemDefinition, params: BlueskyScansParams) -> BlueskyScansIngestor:
    """
    Build the ``bluesky-scans`` ingestor for a problem.

    Raises
    ------
    ValueError
        If ``metrics`` is omitted and the evaluation is not ``image-metrics``, or the metrics are not
        outputs of the evaluation.
    """
    metrics = params.metrics
    if metrics is None:
        metrics = problem.evaluation.params
        if problem.evaluation.name != IMAGE_METRICS.name or not isinstance(metrics, ImageMetricsParams):
            raise ValueError(f"bluesky-scans needs 'metrics' unless the evaluation is {IMAGE_METRICS.name!r}")
    unknown = sorted(set(image_metric_outputs(metrics)) - set(problem.outputs))
    if unknown:
        raise ValueError(f"metrics {unknown} are not outputs of the evaluation; outputs: {list(problem.outputs)}")
    return BlueskyScansIngestor(problem.dof_names, problem.sensors, params.plan_names, metrics)


BLUESKY_SCANS: IngestionEntry[BlueskyScansParams] = IngestionEntry(
    name="bluesky-scans",
    version="1",
    params=BlueskyScansParams,
    build=build_bluesky_scans,
    description="Points from step scans that moved exactly the agent's DOFs: readbacks plus image metrics",
)
"""Catalog entry for :class:`BlueskyScansIngestor`; its outputs depend on the evaluation, so none are declared."""

configure_logging()
app = create_app(
    prototype_settings("passive"),
    optimizers=[MLP_SGD],
    evaluations=[IMAGE_METRICS],
    contexts=[BASELINE_SIGNALS],
    ingestions=[BLUESKY_SCANS],
)
