"""
Ax backend for the bluesky-adaptive server: a client-configurable optimizer specification and analyses.

Requires the ``server`` extra. The server core is backend-neutral; Ax-specific choices live here.
"""

import json
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Literal, Self, TypeVar

from ax.analysis.analysis import Analysis
from ax.analysis.plotly.plotly_analysis import PlotlyAnalysisCard
from ax.analysis.plotly.surface.contour import ContourPlot
from ax.analysis.plotly.surface.slice import SlicePlot
from ax.analysis.summary import Summary
from ax.exceptions.core import AxError
from botorch.acquisition import (
    AcquisitionFunction,
    qExpectedImprovement,
    qLogExpectedImprovement,
    qLogNoisyExpectedImprovement,
    qNoisyExpectedImprovement,
    qUpperConfidenceBound,
)
from botorch.acquisition.multi_objective.logei import qLogNoisyExpectedHypervolumeImprovement
from pydantic import BaseModel, ConfigDict, Discriminator, Field, JsonValue, Tag, create_model, model_validator

from ..protocols import Optimizer
from ..server.catalog import AnalysisMethod, NoParams
from .dof import ChoiceDOF, RangeDOF
from .objective import Objective, to_ax_objective_str
from .optimizer import AxOptimizer

ACQUISITION_FUNCTIONS: dict[str, type[AcquisitionFunction]] = {
    cls.__name__: cls
    for cls in (
        qLogNoisyExpectedImprovement,
        qLogExpectedImprovement,
        qNoisyExpectedImprovement,
        qExpectedImprovement,
        qUpperConfidenceBound,
        qLogNoisyExpectedHypervolumeImprovement,
    )
}
"""The BoTorch acquisition functions a client may choose, by class name."""

AcquisitionFunctionName = Literal[
    "qLogNoisyExpectedImprovement",
    "qLogExpectedImprovement",
    "qNoisyExpectedImprovement",
    "qExpectedImprovement",
    "qUpperConfidenceBound",
    "qLogNoisyExpectedHypervolumeImprovement",
]


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")


_SpecT = TypeVar("_SpecT", bound=_Spec)


def _with_names(model: type[_SpecT], names: Sequence[str]) -> type[_SpecT]:
    """Return a subclass of a spec whose ``name`` accepts only the given values."""
    allowed: Any = Literal[tuple(names)]
    # Pydantic describes a single allowed value as "const"; list "enum" too so clients read one key.
    name = Field(json_schema_extra={"enum": list(names)})
    return create_model(model.__name__, __base__=model, __doc__=model.__doc__, name=(allowed, name))


class RangeDOFSpec(_Spec):
    """A continuous or integer DOF. See :class:`~bluesky_adaptive.ax.dof.RangeDOF`."""

    kind: Literal["range"] = "range"
    name: str
    bounds: tuple[float, float]
    parameter_type: Literal["float", "int"] = "float"
    step_size: float | None = Field(default=None, gt=0)
    scaling: Literal["linear", "log"] | None = None

    @model_validator(mode="after")
    def _ordered_bounds(self) -> Self:
        if self.bounds[0] >= self.bounds[1]:
            raise ValueError(f"The lower bound must be below the upper bound, got {self.bounds}")
        return self

    def to_dof(self) -> RangeDOF:
        """Convert to a :class:`~bluesky_adaptive.ax.dof.RangeDOF`."""
        return RangeDOF(
            name=self.name,
            bounds=self.bounds,
            parameter_type=self.parameter_type,
            step_size=self.step_size,
            scaling=self.scaling,
        )


class ChoiceDOFSpec(_Spec):
    """A discrete DOF. See :class:`~bluesky_adaptive.ax.dof.ChoiceDOF`."""

    kind: Literal["choice"] = "choice"
    name: str
    values: list[bool] | list[int] | list[float] | list[str] = Field(min_length=2)
    parameter_type: Literal["float", "int", "str", "bool"]
    is_ordered: bool | None = None

    def to_dof(self) -> ChoiceDOF:
        """Convert to a :class:`~bluesky_adaptive.ax.dof.ChoiceDOF`."""
        return ChoiceDOF(name=self.name, values=self.values, parameter_type=self.parameter_type, is_ordered=self.is_ordered)


def _dof_kind(value: Any) -> str:
    """Return the DOF kind of a spec or its JSON; ``"range"`` if not given."""
    if isinstance(value, dict):
        return value.get("kind", "range")
    return getattr(value, "kind", "range")


def _dof_spec(range_spec: type[RangeDOFSpec], choice_spec: type[ChoiceDOFSpec]) -> Any:
    """Return the union of a range and a choice DOF spec, told apart by ``kind`` (default ``"range"``)."""
    return Annotated[
        Annotated[range_spec, Tag("range")] | Annotated[choice_spec, Tag("choice")],
        Discriminator(_dof_kind),
    ]


DOFSpec = Annotated[
    Annotated[RangeDOFSpec, Tag("range")] | Annotated[ChoiceDOFSpec, Tag("choice")],
    Discriminator(_dof_kind),
]
"""A range or choice DOF, told apart by ``kind`` (default ``"range"``)."""


class ObjectiveSpec(_Spec):
    """An objective. See :class:`~bluesky_adaptive.ax.objective.Objective`."""

    name: str
    minimize: bool


class GenerationSpec(_Spec):
    """How Ax generates suggestions. See :meth:`~bluesky_adaptive.ax.optimizer.AxOptimizer.configure_generation_strategy`."""

    method: Literal["fast", "quality", "random_search"] = "fast"
    acquisition_function: AcquisitionFunctionName | None = None
    initialization_budget: int | None = Field(default=None, ge=1)
    initialization_random_seed: int | None = None

    @model_validator(mode="after")
    def _no_acquisition_function_for_random_search(self) -> Self:
        if self.method == "random_search" and self.acquisition_function is not None:
            raise ValueError("Random search does not use an acquisition function")
        return self


class AxOptimizerSpec(_Spec):
    """
    A client-configurable Ax optimizer: DOFs, objectives, constraints, and generation strategy.

    Include it in a :class:`~bluesky_adaptive.server.ProblemTemplate`'s parameter model to let
    clients compose the search space and objectives, and call :meth:`build` in the template.
    Use :meth:`restricted` to fix the DOF and objective names clients may choose from.

    Parameters
    ----------
    dofs : list[RangeDOFSpec | ChoiceDOFSpec]
        The search space. DOF names are the parameter names in suggestions.
    objectives : list[ObjectiveSpec]
        The objectives; outcome names returned by the evaluation function.
    dof_constraints : list[str]
        Ax parameter constraints over DOF names, for example ``"x1 + x2 <= 1"``.
    outcome_constraints : list[str]
        Ax outcome constraints, for example ``"intensity >= 1e4"``.
    generation : GenerationSpec
        How Ax generates suggestions.
    """

    dofs: list[DOFSpec] = Field(min_length=1)
    objectives: list[ObjectiveSpec] = Field(min_length=1)
    dof_constraints: list[str] = Field(default_factory=list)
    outcome_constraints: list[str] = Field(default_factory=list)
    generation: GenerationSpec = Field(default_factory=GenerationSpec)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [dof.name for dof in self.dofs] + [objective.name for objective in self.objectives]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"DOF and objective names must be unique, got duplicates {duplicates}")
        return self

    @classmethod
    def restricted(cls, *, dofs: Sequence[str] | None = None, objectives: Sequence[str] | None = None) -> type[Self]:
        """
        Return a variant of the spec that accepts only the given DOF and objective names.

        The names appear as ``enum`` values in the variant's JSON schema, so clients can discover
        them, for example from ``GET /problems``, and validation rejects any other name. Use it for
        names the application fixes: the actuators of a beamline and the outcomes its evaluation
        function returns.

        Parameters
        ----------
        dofs : Sequence[str] | None, optional
            The allowed DOF names. Default: any name.
        objectives : Sequence[str] | None, optional
            The allowed objective names. Default: any name.

        Returns
        -------
        type[AxOptimizerSpec]
            A subclass of the spec. Subclass it again to give it a name of its own, as in
            ``class BeamlineOptimizerSpec(AxOptimizerSpec.restricted(dofs=["m1", "m2"])): ...``.

        Examples
        --------
        >>> Spec = AxOptimizerSpec.restricted(dofs=["motor1", "motor2"], objectives=["flux", "fwhm"])
        >>> Spec.model_json_schema()["$defs"]["RangeDOFSpec"]["properties"]["name"]["enum"]
        ['motor1', 'motor2']
        """
        fields: dict[str, Any] = {}
        if dofs is not None:
            dof_spec = _dof_spec(_with_names(RangeDOFSpec, dofs), _with_names(ChoiceDOFSpec, dofs))
            fields["dofs"] = (list[dof_spec], Field(min_length=1))
        if objectives is not None:
            fields["objectives"] = (list[_with_names(ObjectiveSpec, objectives)], Field(min_length=1))
        return create_model(cls.__name__, __base__=cls, __doc__=cls.__doc__, **fields)

    def build(self, checkpoint_path: str | None = None) -> AxOptimizer:
        """
        Build a fresh optimizer.

        Raises
        ------
        ValueError
            If Ax rejects the configuration, for example a malformed constraint.
        """
        try:
            optimizer = AxOptimizer(
                parameters=[dof.to_dof().to_ax_parameter_config() for dof in self.dofs],
                objective=to_ax_objective_str(
                    [Objective(name=objective.name, minimize=objective.minimize) for objective in self.objectives]
                ),
                parameter_constraints=self.dof_constraints or None,
                outcome_constraints=self.outcome_constraints or None,
                checkpoint_path=checkpoint_path,
            )
            generation = self.generation
            optimizer.configure_generation_strategy(
                generation.method,
                acquisition_function=ACQUISITION_FUNCTIONS[generation.acquisition_function]
                if generation.acquisition_function is not None
                else None,
                initialization_budget=generation.initialization_budget,
                initialization_random_seed=generation.initialization_random_seed,
            )
        except AxError as err:
            raise ValueError(f"Ax rejected the optimizer configuration: {err}") from err
        return optimizer


class ContourParams(_Spec):
    """Parameters of the ``ax-contour`` analysis."""

    x_parameter: str
    y_parameter: str
    metric: str | None = None


class SliceParams(_Spec):
    """Parameters of the ``ax-slice`` analysis."""

    parameter: str
    metric: str | None = None


def _is_ax(optimizer: Optimizer) -> bool:
    return isinstance(optimizer, AxOptimizer)


def _computer(make: Callable[[Any], Analysis]) -> Callable[[Optimizer, Any], JsonValue]:
    """Compute an Ax analysis and convert its cards to JSON: Plotly figures or tables."""

    def compute(optimizer: Optimizer, params: Any) -> JsonValue:
        if not isinstance(optimizer, AxOptimizer):
            raise TypeError(f"Expected an AxOptimizer, got {type(optimizer).__name__}")
        cards = optimizer.ax_client.compute_analyses([make(params)], display=False)
        results: list[JsonValue] = []
        for card in (leaf for group in cards for leaf in group.flatten()):
            result: dict[str, JsonValue] = {"name": card.name, "title": card.title, "subtitle": card.subtitle}
            if isinstance(card, PlotlyAnalysisCard):
                result["figure"] = json.loads(card.blob)
            else:
                result["table"] = json.loads(card.df.to_json(orient="records") or "[]")
            results.append(result)
        return results

    return compute


def ax_analysis_methods() -> list[AnalysisMethod[Any]]:
    """
    Analyses of :class:`~bluesky_adaptive.ax.optimizer.AxOptimizer` for the server's analysis catalog.

    Returns
    -------
    list[AnalysisMethod]
        ``ax-summary`` (a table of trials), ``ax-contour`` (predicted metric over two parameters),
        and ``ax-slice`` (predicted metric along one parameter).
    """
    return [
        AnalysisMethod(
            name="ax-summary",
            params=NoParams,
            compute=_computer(lambda _: Summary()),
            supports=_is_ax,
            description="A table of the experiment's trials, parameters, and metrics.",
        ),
        AnalysisMethod(
            name="ax-contour",
            params=ContourParams,
            compute=_computer(
                lambda p: ContourPlot(x_parameter_name=p.x_parameter, y_parameter_name=p.y_parameter, metric_name=p.metric)
            ),
            supports=_is_ax,
            description="The model's predicted metric over two parameters, as a Plotly figure.",
        ),
        AnalysisMethod(
            name="ax-slice",
            params=SliceParams,
            compute=_computer(lambda p: SlicePlot(parameter_name=p.parameter, metric_name=p.metric)),
            supports=_is_ax,
            description="The model's predicted metric along one parameter, as a Plotly figure.",
        ),
    ]
