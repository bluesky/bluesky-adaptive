"""
Backend-neutral optimization problem: the agent spec clients send, and the definition entries build from.

.. warning::

    This module is **experimental**. The API is not yet stable and may change
    in future releases without a deprecation period.

Constraints and scalarized objectives are linear expressions over DOF or outcome names. Names may
contain ``.`` and ``-``, so binary operators must be surrounded by whitespace, as in Ax::

    dcm_c2-pitch - 2 * m1-pitch <= 1e-4
    diag2_total - 0.5 * diag2_x_sigma_px
"""

import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, model_validator

NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.\-]*$"
"""Names usable in expressions: DOFs, outcomes and context features."""

_NAME = re.compile(NAME_PATTERN)
_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_COMPARISONS = ("<=", ">=")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RangeDOFSpec(_Frozen):
    """
    A continuous or integer degree of freedom.

    Parameters
    ----------
    name : str
        Suggestion key, which is also the data key the acquisition records (for an ophyd-async
        motor, its name, e.g. ``"dcm_c2-pitch"``).
    actuator : str | None
        Device path the queueserver moves (e.g. ``"dcm_c2.pitch"``). A DOF without an actuator
        reaches the plan only inside the suggestions.
    bounds : tuple[float, float]
        Lower and upper bound, inclusive.
    value_type : {"float", "int"}
        Type of the suggested values.
    """

    type: Literal["range"] = "range"
    name: str = Field(pattern=NAME_PATTERN)
    actuator: str | None = None
    bounds: tuple[float, float]
    value_type: Literal["float", "int"] = "float"

    @model_validator(mode="after")
    def _check_bounds(self) -> "RangeDOFSpec":
        lower, upper = self.bounds
        if not (math.isfinite(lower) and math.isfinite(upper) and lower < upper):
            raise ValueError(f"bounds of {self.name!r} must be finite with lower < upper, got {self.bounds}")
        return self


class ChoiceDOFSpec(_Frozen):
    """
    A degree of freedom that takes one of a set of values.

    Parameters
    ----------
    name : str
        Suggestion key and data key, as for :class:`RangeDOFSpec`.
    actuator : str | None
        Device path the queueserver moves.
    values : tuple
        Allowed values, all of ``value_type``.
    value_type : {"float", "int", "str", "bool"}
        Type of the values.
    is_ordered : bool
        Whether the values have a meaningful order.
    """

    type: Literal["choice"] = "choice"
    name: str = Field(pattern=NAME_PATTERN)
    actuator: str | None = None
    values: tuple[bool | int | float | str, ...] = Field(min_length=2)
    value_type: Literal["float", "int", "str", "bool"] = "str"
    is_ordered: bool = False

    @model_validator(mode="after")
    def _check_values(self) -> "ChoiceDOFSpec":
        expected: dict[str, tuple[type, ...]] = {
            "float": (int, float),
            "int": (int,),
            "str": (str,),
            "bool": (bool,),
        }
        for value in self.values:
            is_bool = isinstance(value, bool)
            if not isinstance(value, expected[self.value_type]) or (is_bool and self.value_type != "bool"):
                raise ValueError(f"value {value!r} of {self.name!r} is not of value_type {self.value_type!r}")
        if len(set(self.values)) != len(self.values):
            raise ValueError(f"values of {self.name!r} must be unique, got {self.values}")
        return self


def _dof_type(value: Any) -> str:
    if isinstance(value, Mapping):
        return value.get("type", "range")
    return getattr(value, "type", "range")


DOFSpec = Annotated[
    Annotated[RangeDOFSpec, Tag("range")] | Annotated[ChoiceDOFSpec, Tag("choice")], Discriminator(_dof_type)
]
"""A DOF, discriminated by ``type``; ``"range"`` when omitted."""


class ObjectiveSpec(_Frozen):
    """An outcome to optimize."""

    name: str
    minimize: bool = False


class ScalarizedObjectiveSpec(_Frozen):
    """A linear combination of outcomes to optimize, e.g. ``"diag2_total - 0.5 * diag2_x_sigma_px"``."""

    expression: str
    minimize: bool = False


class PlanSpec(_Frozen):
    """The acquisition plan and its extra keyword arguments."""

    name: str
    kwargs: dict[str, Any] = Field(default_factory=dict)


class EntrySelection(_Frozen):
    """A catalog entry and its parameters."""

    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class OptimizerSelection(_Frozen):
    """An optimizer catalog entry and its options."""

    name: str
    options: dict[str, Any] = Field(default_factory=dict)


class ProblemSpec(_Frozen):
    """
    The agent spec: a complete optimization problem composed from catalog entries.

    Parameters
    ----------
    queueserver : str
        Name of the queueserver in the server settings.
    dofs : list[DOFSpec]
        Degrees of freedom.
    objectives : list[ObjectiveSpec]
        Outcomes to optimize. Exclusive with ``scalarized_objective``.
    scalarized_objective : ScalarizedObjectiveSpec | None
        One linear combination of outcomes to optimize. Exclusive with ``objectives``.
    dof_constraints : list[str]
        Linear constraints over DOF names, e.g. ``"x1 + x2 <= 1"``.
    outcome_constraints : list[str]
        Linear constraints over declared outputs, e.g. ``"diag2_y_centroid >= 120"``.
    sensors : list[str]
        Device paths the plan reads.
    acquisition_plan : PlanSpec
        The plan the queueserver runs once per iteration.
    evaluation, optimizer, context, ingestion : EntrySelection
        Catalog entries. ``context`` and ``ingestion`` are optional.
    """

    queueserver: str
    dofs: list[DOFSpec] = Field(min_length=1)
    objectives: list[ObjectiveSpec] = Field(default_factory=list)
    scalarized_objective: ScalarizedObjectiveSpec | None = None
    dof_constraints: list[str] = Field(default_factory=list)
    outcome_constraints: list[str] = Field(default_factory=list)
    sensors: list[str] = Field(default_factory=list)
    acquisition_plan: PlanSpec
    evaluation: EntrySelection
    optimizer: OptimizerSelection
    context: EntrySelection | None = None
    ingestion: EntrySelection | None = None

    @model_validator(mode="after")
    def _check(self) -> "ProblemSpec":
        if bool(self.objectives) == (self.scalarized_objective is not None):
            raise ValueError("give exactly one of 'objectives' and 'scalarized_objective'")
        _require_unique([dof.name for dof in self.dofs], "DOF names")
        _require_unique([dof.actuator for dof in self.dofs if dof.actuator is not None], "DOF actuators")
        _require_unique([objective.name for objective in self.objectives], "objective names")
        return self


def _require_unique(values: list[str], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"{label} must be unique, got duplicates {duplicates}")


@dataclass(frozen=True)
class LinearConstraint:
    """
    A parsed linear constraint ``sum(coefficient * name) <op> bound``.

    Parameters
    ----------
    terms : tuple[tuple[str, float], ...]
        ``(name, coefficient)`` pairs, one per name.
    op : {"<=", ">="}
        Comparison.
    bound : float
        Right-hand side.
    expression : str
        The expression as the client wrote it.
    """

    terms: tuple[tuple[str, float], ...]
    op: Literal["<=", ">="]
    bound: float
    expression: str

    @property
    def names(self) -> tuple[str, ...]:
        """Names the constraint refers to."""
        return tuple(name for name, _ in self.terms)

    def lhs(self, values: Mapping[str, float]) -> float:
        """Evaluate the left-hand side at ``values``."""
        return sum(coefficient * float(values[name]) for name, coefficient in self.terms)

    def violation(self, values: Mapping[str, float]) -> float:
        """How far ``values`` are outside the constraint; 0 when satisfied."""
        lhs = self.lhs(values)
        return max(0.0, lhs - self.bound) if self.op == "<=" else max(0.0, self.bound - lhs)

    def canonical(self) -> str:
        """Render as ``c * name + ... <op> bound``, with whitespace around every operator."""
        return f"{_render_terms(self.terms)} {self.op} {_number(self.bound)}"


@dataclass(frozen=True)
class LinearObjective:
    """
    A parsed linear combination of outcomes to optimize.

    Parameters
    ----------
    terms : tuple[tuple[str, float], ...]
        ``(name, coefficient)`` pairs, one per name.
    minimize : bool
        Whether to minimize the combination.
    expression : str
        The expression as the client wrote it.
    """

    terms: tuple[tuple[str, float], ...]
    minimize: bool
    expression: str

    @property
    def names(self) -> tuple[str, ...]:
        """Outcomes the objective combines."""
        return tuple(name for name, _ in self.terms)

    def canonical(self) -> str:
        """Render as ``c * name + ...``, with whitespace around every operator."""
        return _render_terms(self.terms)


def _number(value: float) -> str:
    return f"{value:.17g}"


def _render_terms(terms: tuple[tuple[str, float], ...]) -> str:
    rendered = ""
    for index, (name, coefficient) in enumerate(terms):
        if index == 0:
            rendered = f"{_number(coefficient)} * {name}"
        elif coefficient < 0:
            rendered += f" - {_number(-coefficient)} * {name}"
        else:
            rendered += f" + {_number(coefficient)} * {name}"
    return rendered


_Token = tuple[Literal["num", "name", "op", "cmp"], Any]


def _tokenize(expression: str, names: Collection[str]) -> list[_Token]:
    tokens: list[_Token] = []
    for chunk in expression.split():
        if chunk in ("+", "-", "*"):
            tokens.append(("op", chunk))
            continue
        if chunk in _COMPARISONS:
            tokens.append(("cmp", chunk))
            continue
        if any(char in chunk for char in "<>=!"):
            if chunk in ("<", ">", "==", "=", "!="):
                raise ValueError(f"unsupported comparison {chunk!r}; use '<=' or '>='")
            raise ValueError(f"comparison in {chunk!r} must be surrounded by whitespace")
        for index, part in enumerate(chunk.split("*")):
            if index:
                tokens.append(("op", "*"))
            if part:
                tokens.extend(_classify(part, names))
    return tokens


def _classify(part: str, names: Collection[str]) -> list[_Token]:
    if _NUMBER.match(part):
        return [("num", float(part))]
    if part.startswith("-") and _NAME.match(part[1:]):
        return [("op", "-"), ("name", part[1:])]
    if _NAME.match(part):
        return [("name", part)]
    if "+" in part:
        raise ValueError(f"'+' in {part!r} must be surrounded by whitespace")
    raise ValueError(f"unexpected token {part!r}; allowed names: {_allowed(names)}")


def _allowed(names: Collection[str]) -> str:
    return ", ".join(sorted(names)) or "(none)"


class _Parser:
    def __init__(self, expression: str, names: Collection[str]):
        self.expression = expression
        self.names = names
        self.tokens = _tokenize(expression, names)
        self.position = 0

    def peek(self) -> _Token | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self) -> _Token | None:
        token = self.peek()
        self.position += 1
        return token

    def terms(self) -> tuple[tuple[str, float], ...]:
        coefficients: dict[str, float] = {}
        sign = 1.0
        while True:
            name, coefficient = self.term()
            coefficients[name] = coefficients.get(name, 0.0) + sign * coefficient
            token = self.peek()
            if token is None or token[0] != "op" or token[1] not in ("+", "-"):
                break
            self.take()
            sign = 1.0 if token[1] == "+" else -1.0
        return tuple(coefficients.items())

    def term(self) -> tuple[str, float]:
        coefficient = 1.0
        token = self.take()
        while token == ("op", "-"):
            coefficient = -coefficient
            token = self.take()
        if token is not None and token[0] == "num":
            coefficient *= token[1]
            if self.take() != ("op", "*"):
                raise ValueError(f"expected '*' and a name after coefficient {_number(token[1])}")
            token = self.take()
        if token is None or token[0] != "name":
            found = "end of expression" if token is None else repr(token[1])
            raise ValueError(f"expected a name, found {found}; allowed names: {_allowed(self.names)}")
        return self.name(token[1]), coefficient

    def name(self, name: str) -> str:
        if name in self.names:
            return name
        splits = [index for index, char in enumerate(name) if char == "-"]
        hint = ""
        if any(name[:index] in self.names and name[index + 1 :] in self.names for index in splits):
            hint = " (binary '-' must be surrounded by whitespace)"
        raise ValueError(f"unknown name {name!r}{hint}; allowed names: {_allowed(self.names)}")

    def bound(self) -> float:
        sign = 1.0
        token = self.take()
        while token == ("op", "-"):
            sign = -sign
            token = self.take()
        if token is None or token[0] != "num":
            found = "end of expression" if token is None else repr(token[1])
            raise ValueError(f"expected a number after the comparison, found {found}")
        return sign * token[1]

    def finish(self) -> None:
        token = self.peek()
        if token is not None:
            raise ValueError(f"unexpected {token[1]!r} after the expression")


def parse_linear_constraint(expression: str, names: Collection[str]) -> LinearConstraint:
    """
    Parse ``term (('+'|'-') term)* ('<='|'>=') number``, where ``term`` is ``[number '*'] name``.

    Parameters
    ----------
    expression : str
        The constraint, e.g. ``"dcm_c2-pitch - 2 * m1-pitch <= 1e-4"``.
    names : Collection[str]
        Names the constraint may refer to.

    Returns
    -------
    LinearConstraint
        The parsed constraint.

    Raises
    ------
    ValueError
        If the expression does not parse, naming the offending token and the allowed names.
    """
    try:
        parser = _Parser(expression, names)
        terms = parser.terms()
        comparison = parser.take()
        if comparison is None or comparison[0] != "cmp":
            found = "end of expression" if comparison is None else repr(comparison[1])
            raise ValueError(f"expected '<=' or '>=', found {found}")
        bound = parser.bound()
        parser.finish()
    except ValueError as err:
        raise ValueError(f"invalid constraint {expression!r}: {err}") from err
    return LinearConstraint(terms=terms, op=comparison[1], bound=bound, expression=expression)


def parse_linear_objective(expression: str, names: Collection[str], *, minimize: bool) -> LinearObjective:
    """
    Parse ``term (('+'|'-') term)*``, where ``term`` is ``[number '*'] name``.

    Parameters
    ----------
    expression : str
        The objective, e.g. ``"diag2_total - 0.5 * diag2_x_sigma_px"``.
    names : Collection[str]
        Outcome names the objective may combine.
    minimize : bool
        Whether to minimize the combination.

    Returns
    -------
    LinearObjective
        The parsed objective.

    Raises
    ------
    ValueError
        If the expression does not parse, naming the offending token and the allowed names.
    """
    try:
        parser = _Parser(expression, names)
        terms = parser.terms()
        parser.finish()
    except ValueError as err:
        raise ValueError(f"invalid objective {expression!r}: {err}") from err
    return LinearObjective(terms=terms, minimize=minimize, expression=expression)


@dataclass(frozen=True)
class EntryConfig:
    """
    A catalog entry chosen for an agent, with its validated params (or optimizer options).

    Parameters
    ----------
    name : str
        Catalog entry name.
    params : pydantic.BaseModel
        The entry's validated params or options.
    """

    name: str
    params: BaseModel


@dataclass(frozen=True)
class ProblemDefinition:
    """
    A validated optimization problem, passed to every catalog entry's ``build``.

    Parameters
    ----------
    dofs : tuple[RangeDOFSpec | ChoiceDOFSpec, ...]
        Degrees of freedom.
    objectives : tuple[ObjectiveSpec, ...]
        Outcomes to optimize; empty when ``scalarized_objective`` is set.
    scalarized_objective : LinearObjective | None
        One linear combination of outcomes to optimize.
    dof_constraints : tuple[LinearConstraint, ...]
        Constraints over DOF names.
    outcome_constraints : tuple[LinearConstraint, ...]
        Constraints over outputs.
    outputs : tuple[str, ...]
        Outcome names the evaluation entry declares.
    context_features : tuple[str, ...]
        Feature names the context entry declares; empty without a context entry.
    sensors : tuple[str, ...]
        Device paths the plan reads.
    plan_name : str
        The acquisition plan.
    plan_kwargs : Mapping[str, Any]
        Extra keyword arguments for the plan.
    evaluation, optimizer : EntryConfig
        The chosen entries with validated params, so sibling entries can share configuration.
    context, ingestion : EntryConfig | None
        Optional entries.
    """

    dofs: tuple[RangeDOFSpec | ChoiceDOFSpec, ...]
    objectives: tuple[ObjectiveSpec, ...]
    scalarized_objective: LinearObjective | None
    dof_constraints: tuple[LinearConstraint, ...]
    outcome_constraints: tuple[LinearConstraint, ...]
    outputs: tuple[str, ...]
    context_features: tuple[str, ...]
    sensors: tuple[str, ...]
    plan_name: str
    plan_kwargs: Mapping[str, Any]
    evaluation: EntryConfig
    optimizer: EntryConfig
    context: EntryConfig | None = None
    ingestion: EntryConfig | None = None

    @property
    def dof_names(self) -> tuple[str, ...]:
        """Suggestion keys, in DOF order."""
        return tuple(dof.name for dof in self.dofs)

    @property
    def actuators(self) -> tuple[str, ...]:
        """Device paths of the DOFs that have an actuator, in DOF order."""
        return tuple(dof.actuator for dof in self.dofs if dof.actuator is not None)

    def objective_weights(self) -> dict[str, float]:
        """
        Weights that turn the objectives into one score to maximize.

        Returns
        -------
        dict[str, float]
            ``{outcome: weight}``; minimized outcomes get negative weights. Several objectives are
            summed with unit weights.
        """
        if self.scalarized_objective is not None:
            sign = -1.0 if self.scalarized_objective.minimize else 1.0
            return {name: sign * coefficient for name, coefficient in self.scalarized_objective.terms}
        return {objective.name: -1.0 if objective.minimize else 1.0 for objective in self.objectives}

    @classmethod
    def from_spec(
        cls,
        spec: ProblemSpec,
        *,
        outputs: Collection[str],
        context_features: Collection[str],
        evaluation: EntryConfig,
        optimizer: EntryConfig,
        context: EntryConfig | None = None,
        ingestion: EntryConfig | None = None,
    ) -> "ProblemDefinition":
        """
        Validate names and parse expressions in a spec.

        Parameters
        ----------
        spec : ProblemSpec
            The client's spec.
        outputs : Collection[str]
            Outcome names the evaluation entry declares.
        context_features : Collection[str]
            Feature names the context entry declares.
        evaluation, optimizer, context, ingestion : EntryConfig
            The chosen entries with validated params.

        Returns
        -------
        ProblemDefinition
            The validated problem.

        Raises
        ------
        ValueError
            If an objective or constraint refers to an unknown name, or names collide.
        """
        dof_names = [dof.name for dof in spec.dofs]
        for label, names in (("outputs", outputs), ("context features", context_features)):
            invalid = sorted(name for name in names if not _NAME.match(name))
            if invalid:
                raise ValueError(f"{label} {invalid} do not match {NAME_PATTERN}")
        collisions = sorted((set(dof_names) & set(outputs)) | (set(dof_names) & set(context_features)))
        collisions += sorted(set(outputs) & set(context_features))
        if collisions:
            raise ValueError(f"DOF, output and context feature names must be distinct, got {collisions}")
        for objective in spec.objectives:
            if objective.name not in outputs:
                raise ValueError(f"objective {objective.name!r} is not a declared output; outputs: {_allowed(outputs)}")
        scalarized = None
        if spec.scalarized_objective is not None:
            scalarized = parse_linear_objective(
                spec.scalarized_objective.expression, outputs, minimize=spec.scalarized_objective.minimize
            )
        return cls(
            dofs=tuple(spec.dofs),
            objectives=tuple(spec.objectives),
            scalarized_objective=scalarized,
            dof_constraints=tuple(parse_linear_constraint(text, dof_names) for text in spec.dof_constraints),
            outcome_constraints=tuple(parse_linear_constraint(text, outputs) for text in spec.outcome_constraints),
            outputs=tuple(outputs),
            context_features=tuple(context_features),
            sensors=tuple(spec.sensors),
            plan_name=spec.acquisition_plan.name,
            plan_kwargs=dict(spec.acquisition_plan.kwargs),
            evaluation=evaluation,
            optimizer=optimizer,
            context=context,
            ingestion=ingestion,
        )
