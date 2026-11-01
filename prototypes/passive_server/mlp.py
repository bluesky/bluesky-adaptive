"""
A contextual MLP surrogate optimizer trained by online SGD.

The surrogate maps DOFs plus context features to the modeled outputs. Each :meth:`MLPSurrogateOptimizer.ingest`
trains it with mini-batch SGD over all data seen so far; suggestions score random candidates inside the DOF box
at a given context and return the best, with ε-exploration.
"""

import logging
import math
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import torch
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

from bluesky_adaptive.protocols import ID_KEY
from bluesky_adaptive.server.catalog import OptimizerEntry
from bluesky_adaptive.server.problem import ChoiceDOFSpec, LinearConstraint, ProblemDefinition

logger = logging.getLogger(__name__)

_RELATIVE_STD_FLOOR = 1e-12
"""A standard deviation below ``_RELATIVE_STD_FLOOR * max(1, |mean|)`` counts as constant and is replaced by 1."""


class MLPOptions(BaseModel):
    """
    Options of :class:`MLPSurrogateOptimizer`.

    Parameters
    ----------
    hidden_layers : list[int]
        Widths of the hidden layers.
    learning_rate : float
        SGD learning rate.
    momentum : float
        SGD momentum.
    epochs_per_ingest : int
        Passes over all data seen so far after each ingest.
    batch_size : int
        Mini-batch size.
    n_candidates : int
        Uniform candidates drawn in the DOF box per suggestion.
    exploration : float
        Fraction of suggestions replaced by uniform candidates (ε).
    min_points : int
        Ingested points needed before the model drives suggestions; until then all picks are uniform.
    min_separation : float
        Minimum pairwise distance between model-driven picks, in DOF units scaled to [0, 1].
    constraint_penalty : float
        Weight of predicted outcome-constraint violation in the candidate score.
    seed : int
        Seed of the weight initialization, the shuffling and the candidate draws.
    """

    model_config = ConfigDict(extra="forbid")

    hidden_layers: list[int] = Field(default_factory=lambda: [64, 64], min_length=1)
    learning_rate: float = Field(default=1e-2, gt=0)
    momentum: float = Field(default=0.9, ge=0, lt=1)
    epochs_per_ingest: int = Field(default=200, ge=0)
    batch_size: int = Field(default=64, ge=1)
    n_candidates: int = Field(default=4096, ge=1)
    exploration: float = Field(default=0.2, ge=0, le=1)
    min_points: int = Field(default=5, ge=1)
    min_separation: float = Field(default=0.05, ge=0)
    constraint_penalty: float = Field(default=10.0, ge=0)
    seed: int = 0


def _standardization(values: NDArray[np.float64]) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Column means and standard deviations ignoring NaN; empty or constant columns get mean 0 / std 1."""
    count = np.sum(np.isfinite(values), axis=0)
    filled = np.where(np.isfinite(values), values, 0.0)
    mean = np.divide(filled.sum(axis=0), count, out=np.zeros(values.shape[1]), where=count > 0)
    deviation = np.where(np.isfinite(values), values - mean, 0.0)
    std = np.sqrt(np.divide((deviation**2).sum(axis=0), count, out=np.zeros(values.shape[1]), where=count > 0))
    degenerate = std < _RELATIVE_STD_FLOOR * np.maximum(1.0, np.abs(mean))
    return mean, np.where(degenerate, 1.0, std)


def _finite_number(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | np.integer | np.floating):
        raise ValueError(f"{key!r} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{key!r} must be finite, got {value!r}")
    return number


class MLPSurrogateOptimizer:
    """
    Contextual optimizer: an MLP surrogate trained by SGD that scores random candidates.

    Implements :class:`~bluesky_adaptive.protocols.Optimizer`, :class:`~bluesky_adaptive.protocols.ContextAware`,
    :class:`~bluesky_adaptive.protocols.Checkpointable`, :class:`~bluesky_adaptive.protocols.CanRegisterSuggestions`
    and :class:`~bluesky_adaptive.protocols.TrialFaultAware`.

    Inputs are the DOFs, scaled to [0, 1] by their bounds, and the context features, standardized. Outputs are
    standardized. Means and standard deviations are recomputed over all data seen at every ingest; a standard
    deviation below ``1e-12 * max(1, |mean|)`` marks a constant column and is replaced by 1.

    The candidate score is ``sum_k w_k * (yhat_k - mean_k) / std_k`` (standardized, so weights are scale-free)
    minus ``constraint_penalty * sum_j violation_j / scale_j``, where ``violation_j`` is the outcome constraint's
    violation at the predicted raw outputs and ``scale_j = sum_i |c_ij| * std_i`` over the constraint's terms.

    Parameters
    ----------
    parameters : Sequence[tuple[str, float, float]]
        ``(name, lower, upper)`` of each DOF.
    context_features : Sequence[str]
        Context feature names; empty for a non-contextual optimizer.
    outputs : Sequence[str]
        Outputs to model.
    objective_weights : Mapping[str, float]
        The optimizer maximizes ``sum(w_k * y_k)``.
    outcome_constraints : Sequence[LinearConstraint]
        Constraints over outputs.
    dof_constraints : Sequence[LinearConstraint]
        Constraints over DOFs; every suggestion satisfies them.
    options : MLPOptions | None
        Model and search options; defaults when None.
    checkpoint_path : str | Path | None
        Where :meth:`checkpoint` writes; checkpointing raises without it.
    integer_parameters : Collection[str]
        DOFs whose suggested values are rounded to integers.

    Raises
    ------
    ValueError
        If names are duplicated or unknown, bounds are invalid, or an integer DOF has no integer in its bounds.
    """

    def __init__(
        self,
        parameters: Sequence[tuple[str, float, float]],
        context_features: Sequence[str],
        outputs: Sequence[str],
        objective_weights: Mapping[str, float],
        outcome_constraints: Sequence[LinearConstraint] = (),
        dof_constraints: Sequence[LinearConstraint] = (),
        options: MLPOptions | None = None,
        checkpoint_path: str | Path | None = None,
        integer_parameters: Collection[str] = (),
    ):
        if not parameters:
            raise ValueError("at least one parameter is required")
        if not outputs:
            raise ValueError("at least one output is required")
        self._dofs = tuple(name for name, _, _ in parameters)
        self._features = tuple(context_features)
        self._outputs = tuple(outputs)
        names = [*self._dofs, *self._features, *self._outputs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"parameter, context feature and output names must be unique, got duplicates {duplicates}")
        self._lower = np.array([float(lower) for _, lower, _ in parameters])
        self._upper = np.array([float(upper) for _, _, upper in parameters])
        for name, lower, upper in zip(self._dofs, self._lower, self._upper, strict=True):
            if not (math.isfinite(lower) and math.isfinite(upper) and lower < upper):
                raise ValueError(f"bounds of {name!r} must be finite with lower < upper, got ({lower}, {upper})")

        unknown = sorted(set(objective_weights) - set(self._outputs))
        if unknown:
            raise ValueError(f"objective names {unknown} are not modeled outputs {list(self._outputs)}")
        if not objective_weights:
            raise ValueError("objective_weights must name at least one output")
        for constraint in outcome_constraints:
            unknown = sorted(set(constraint.names) - set(self._outputs))
            if unknown:
                raise ValueError(f"outcome constraint {constraint.expression!r} names {unknown} that are not outputs")
        for constraint in dof_constraints:
            unknown = sorted(set(constraint.names) - set(self._dofs))
            if unknown:
                raise ValueError(f"DOF constraint {constraint.expression!r} names {unknown} that are not parameters")
        unknown = sorted(set(integer_parameters) - set(self._dofs))
        if unknown:
            raise ValueError(f"integer parameters {unknown} are not parameters {list(self._dofs)}")

        self._integer = np.array([name in integer_parameters for name in self._dofs])
        self._integer_lower = np.where(self._integer, np.ceil(self._lower), self._lower)
        self._integer_upper = np.where(self._integer, np.floor(self._upper), self._upper)
        empty = [name for name, lo, hi in zip(self._dofs, self._integer_lower, self._integer_upper, strict=True) if lo > hi]
        if empty:
            raise ValueError(f"integer parameters {empty} have no integer value inside their bounds")

        self._weights = np.array([float(objective_weights.get(name, 0.0)) for name in self._outputs])
        self._objective_outputs = tuple(name for name in self._outputs if name in objective_weights)
        self._outcome_constraints = tuple(outcome_constraints)
        self._dof_constraints = tuple(dof_constraints)
        self._dof_matrix, self._dof_bounds, self._dof_signs = self._constraint_arrays(self._dof_constraints, self._dofs)
        self._out_matrix, self._out_bounds, self._out_signs = self._constraint_arrays(
            self._outcome_constraints, self._outputs
        )

        self._options = options or MLPOptions()
        self._checkpoint_path = Path(checkpoint_path) if checkpoint_path is not None else None
        self._rng = np.random.default_rng(self._options.seed)
        self._torch_generator = torch.Generator().manual_seed(self._options.seed)
        self._model = self._build_model()
        self._sgd = torch.optim.SGD(
            self._model.parameters(), lr=self._options.learning_rate, momentum=self._options.momentum
        )

        n_inputs = len(self._dofs) + len(self._features)
        self._inputs = np.empty((0, n_inputs))
        self._targets = np.empty((0, len(self._outputs)))
        self._ids: list[int] = []
        self._feature_mean = np.zeros(len(self._features))
        self._feature_std = np.ones(len(self._features))
        self._output_mean = np.zeros(len(self._outputs))
        self._output_std = np.ones(len(self._outputs))
        self._pending: dict[int, dict[str, float]] = {}
        self._next_id = 0

    @staticmethod
    def _constraint_arrays(
        constraints: Sequence[LinearConstraint], names: Sequence[str]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
        """Matrix ``A``, bounds ``b`` and signs ``s`` such that the violation is ``max(0, s * (A @ x - b))``."""
        matrix = np.zeros((len(constraints), len(names)))
        for row, constraint in enumerate(constraints):
            for name, coefficient in constraint.terms:
                matrix[row, names.index(name)] += coefficient
        bounds = np.array([constraint.bound for constraint in constraints])
        signs = np.array([1.0 if constraint.op == "<=" else -1.0 for constraint in constraints])
        return matrix, bounds, signs

    def _build_model(self) -> torch.nn.Sequential:
        layers: list[torch.nn.Module] = []
        width = len(self._dofs) + len(self._features)
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(self._options.seed)
            for hidden in self._options.hidden_layers:
                layers += [torch.nn.Linear(width, hidden, dtype=torch.float64), torch.nn.Tanh()]
                width = hidden
            layers.append(torch.nn.Linear(width, len(self._outputs), dtype=torch.float64))
            return torch.nn.Sequential(*layers)

    @property
    def parameter_names(self) -> tuple[str, ...]:
        """DOF names, in order."""
        return self._dofs

    @property
    def context_features(self) -> tuple[str, ...]:
        """Context feature names, in order."""
        return self._features

    @property
    def outputs(self) -> tuple[str, ...]:
        """Modeled outputs, in order."""
        return self._outputs

    @property
    def n_points(self) -> int:
        """Number of points ingested so far."""
        return len(self._ids)

    @property
    def pending(self) -> Mapping[int, Mapping[str, float]]:
        """Suggestions awaiting outcomes, keyed by ``_id``."""
        return dict(self._pending)

    def _new_id(self) -> int:
        identifier = self._next_id
        self._next_id += 1
        return identifier

    def _dof_values(self, point: Mapping[str, Any], label: str) -> dict[str, float]:
        missing = [name for name in self._dofs if name not in point]
        if missing:
            raise ValueError(f"{label} lacks parameters {missing}; it has keys {sorted(map(str, point))}")
        return {name: _finite_number(point[name], name) for name in self._dofs}

    def _context_values(self, context: Mapping[str, Any], label: str) -> NDArray[np.float64]:
        missing = [name for name in self._features if name not in context]
        if missing:
            raise ValueError(f"{label} lacks context features {missing}; it has keys {sorted(map(str, context))}")
        return np.array([_finite_number(context[name], name) for name in self._features])

    # Optimizer

    def ingest(self, points: Sequence[Mapping]) -> None:
        """
        Ingest evaluated points and train the surrogate on all data seen so far.

        Parameters
        ----------
        points : Sequence[Mapping]
            Each point has outputs and, for a contextual optimizer, every context feature. A point whose ``_id``
            is a pending suggestion takes its DOF values from that suggestion; any other point must carry every
            DOF and gets a new ``_id``. Missing or non-finite outputs are left out of the loss.

        Raises
        ------
        ValueError
            If a point lacks DOFs or context features, or a DOF or feature value is not a finite number.
        """
        rows: list[NDArray[np.float64]] = []
        targets: list[NDArray[np.float64]] = []
        ids: list[int] = []
        claimed: set[int] = set()
        for index, point in enumerate(points):
            identifier = point.get(ID_KEY)
            if isinstance(identifier, int) and identifier in self._pending and identifier not in claimed:
                dofs = self._pending[identifier]
                claimed.add(identifier)
            else:
                dofs = self._dof_values(point, f"point {index} (_id={identifier!r})")
                identifier = None
            features = self._context_values(point, f"point {index}")
            ids.append(identifier if identifier is not None else -1)
            rows.append(np.concatenate([[dofs[name] for name in self._dofs], features]))
            targets.append(np.array([self._output_value(point.get(name)) for name in self._outputs]))
        if not rows:
            return
        for position, identifier in enumerate(ids):
            if identifier == -1:
                ids[position] = self._new_id()
            else:
                self._pending.pop(identifier)
        self._inputs = np.vstack([self._inputs, *rows])
        self._targets = np.vstack([self._targets, *targets])
        self._ids += ids
        self._train()

    @staticmethod
    def _output_value(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float | np.integer | np.floating):
            return math.nan
        number = float(value)
        return number if math.isfinite(number) else math.nan

    def _scaled_inputs(self, inputs: NDArray[np.float64]) -> NDArray[np.float64]:
        n_dofs = len(self._dofs)
        dofs = (inputs[:, :n_dofs] - self._lower) / (self._upper - self._lower)
        features = (inputs[:, n_dofs:] - self._feature_mean) / self._feature_std
        return np.hstack([dofs, features])

    def _train(self) -> None:
        n_dofs = len(self._dofs)
        self._feature_mean, self._feature_std = _standardization(self._inputs[:, n_dofs:])
        self._output_mean, self._output_std = _standardization(self._targets)
        inputs = torch.from_numpy(self._scaled_inputs(self._inputs))
        standardized = (self._targets - self._output_mean) / self._output_std
        mask = torch.from_numpy(np.isfinite(standardized))
        targets = torch.from_numpy(np.where(np.isfinite(standardized), standardized, 0.0))
        if not bool(mask.any()):
            logger.debug("no finite outputs yet; skipping training")
            return
        self._model.train()
        total = inputs.shape[0]
        loss = torch.tensor(math.nan)
        for _ in range(self._options.epochs_per_ingest):
            order = torch.randperm(total, generator=self._torch_generator)
            for start in range(0, total, self._options.batch_size):
                batch = order[start : start + self._options.batch_size]
                batch_mask = mask[batch]
                count = batch_mask.sum()
                if count == 0:
                    continue
                self._sgd.zero_grad()
                residual = (self._model(inputs[batch]) - targets[batch]) * batch_mask
                loss = (residual**2).sum() / count
                loss.backward()
                self._sgd.step()
        self._model.eval()
        logger.debug("trained on %d points; last batch loss %.4g", total, float(loss.detach()))

    def suggest(self, num_points: int | None = None) -> Sequence[Mapping]:
        """
        Suggest points; only for an optimizer without context features.

        Parameters
        ----------
        num_points : int | None
            Number of points; 1 when None.

        Returns
        -------
        Sequence[Mapping]
            ``{dof: value, ..., "_id": int}`` per point.

        Raises
        ------
        ValueError
            If the optimizer has context features.
        """
        if self._features:
            raise ValueError(f"this optimizer requires a context {list(self._features)}; use suggest_in_context")
        return self.suggest_in_context({}, num_points)

    def suggest_in_context(self, context: Mapping[str, Any], num_points: int | None = None) -> Sequence[Mapping]:
        """
        Suggest points at a context: the best-scoring random candidates, with ε-exploration.

        Parameters
        ----------
        context : Mapping[str, Any]
            Value of every context feature; extra keys are ignored.
        num_points : int | None
            Number of points; 1 when None.

        Returns
        -------
        Sequence[Mapping]
            ``{dof: value, ..., "_id": int}`` per point, without context keys. They stay pending until ingested.

        Raises
        ------
        ValueError
            If a context feature is missing, ``num_points < 1``, or no candidate satisfies the DOF constraints.
        """
        n = 1 if num_points is None else num_points
        if n < 1:
            raise ValueError(f"num_points must be at least 1, got {num_points}")
        features = self._context_values(context, "context")
        candidates = self._feasible_candidates()
        if self.n_points < self._options.min_points:
            picks = self._uniform_picks(len(candidates), n, exclude=np.array([], dtype=int))
        else:
            scores = self._scores(candidates, features)
            picks = self._greedy_picks(candidates, scores, n)
            n_explore = (
                int(round(self._options.exploration * n)) if n > 1 else int(self._rng.random() < self._options.exploration)
            )
            if n_explore:
                picks[n - n_explore :] = self._uniform_picks(len(candidates), n_explore, exclude=picks[: n - n_explore])
        suggestions = []
        for row in candidates[picks]:
            values = {
                name: int(value) if integer else float(value)
                for name, value, integer in zip(self._dofs, row, self._integer, strict=True)
            }
            identifier = self._new_id()
            self._pending[identifier] = {name: float(value) for name, value in values.items()}
            suggestions.append({**values, ID_KEY: identifier})
        return suggestions

    def _feasible_candidates(self) -> NDArray[np.float64]:
        unit = self._rng.random((self._options.n_candidates, len(self._dofs)))
        candidates = self._lower + unit * (self._upper - self._lower)
        candidates = np.where(
            self._integer, np.clip(np.round(candidates), self._integer_lower, self._integer_upper), candidates
        )
        if len(self._dof_constraints):
            violation = self._dof_signs * (candidates @ self._dof_matrix.T - self._dof_bounds)
            candidates = candidates[np.all(violation <= 0, axis=1)]
        if not len(candidates):
            expressions = [constraint.expression for constraint in self._dof_constraints]
            raise ValueError(f"none of {self._options.n_candidates} candidates satisfies the DOF constraints {expressions}")
        return candidates

    def _uniform_picks(self, n_candidates: int, n: int, exclude: NDArray[np.int_]) -> NDArray[np.int_]:
        available = np.setdiff1d(np.arange(n_candidates), exclude)
        if len(available) == 0:
            available = np.arange(n_candidates)
        return self._rng.choice(available, size=n, replace=len(available) < n)

    def _predict(self, inputs: NDArray[np.float64]) -> NDArray[np.float64]:
        """Predicted standardized outputs at raw inputs."""
        with torch.no_grad():
            return self._model(torch.from_numpy(self._scaled_inputs(inputs))).numpy()

    def _scores(self, candidates: NDArray[np.float64], features: NDArray[np.float64]) -> NDArray[np.float64]:
        inputs = np.hstack([candidates, np.broadcast_to(features, (len(candidates), len(features)))])
        standardized = self._predict(inputs)
        scores = standardized @ self._weights
        if len(self._outcome_constraints):
            raw = standardized * self._output_std + self._output_mean
            violation = np.maximum(0.0, self._out_signs * (raw @ self._out_matrix.T - self._out_bounds))
            scale = np.abs(self._out_matrix) @ self._output_std
            scores = scores - self._options.constraint_penalty * (violation / scale).sum(axis=1)
        return scores

    def _greedy_picks(self, candidates: NDArray[np.float64], scores: NDArray[np.float64], n: int) -> NDArray[np.int_]:
        unit = (candidates - self._lower) / (self._upper - self._lower)
        order = np.argsort(-scores, kind="stable")
        picks: list[int] = []
        for index in order:
            if len(picks) == n:
                break
            if not picks or np.min(np.linalg.norm(unit[picks] - unit[index], axis=1)) >= self._options.min_separation:
                picks.append(int(index))
        if len(picks) < n:
            chosen = set(picks)
            picks += [int(index) for index in order if int(index) not in chosen][: n - len(picks)]
        return np.array(picks[:n], dtype=int)

    def get_best_points(self) -> Sequence[tuple[Any, Mapping, Mapping]]:
        """
        Return the observed point with the best objective score among those satisfying the outcome constraints.

        Returns
        -------
        Sequence[tuple[Any, Mapping, Mapping]]
            One ``(_id, {DOFs and context features}, {observed outputs})``.

        Raises
        ------
        ValueError
            If no point was ingested, or none has every objective and constrained output observed and feasible.
        """
        if not self._ids:
            raise ValueError("no points ingested yet")
        needed = [self._outputs.index(name) for name in self._objective_outputs]
        needed += [index for index in range(len(self._outputs)) if np.any(self._out_matrix[:, index])]
        observed = np.all(np.isfinite(self._targets[:, needed]), axis=1)
        filled = np.where(np.isfinite(self._targets), self._targets, 0.0)
        if len(self._outcome_constraints):
            violation = self._out_signs * (filled @ self._out_matrix.T - self._out_bounds)
            observed &= np.all(violation <= 0, axis=1)
        if not observed.any():
            raise ValueError(f"none of {len(self._ids)} points has the objectives observed and satisfies the constraints")
        scores = np.where(observed, filled @ self._weights, -np.inf)
        best = int(np.argmax(scores))
        names = (*self._dofs, *self._features)
        parameters = {name: float(value) for name, value in zip(names, self._inputs[best], strict=True)}
        outcomes = {
            name: float(value)
            for name, value in zip(self._outputs, self._targets[best], strict=True)
            if math.isfinite(value)
        }
        return [(self._ids[best], parameters, outcomes)]

    # CanRegisterSuggestions, TrialFaultAware

    def register_suggestions(self, suggestions: Sequence[Mapping]) -> Sequence[Mapping]:
        """
        Give suggestions new ``_id`` values and keep them pending.

        Parameters
        ----------
        suggestions : Sequence[Mapping]
            Each has every DOF; an existing ``_id`` is overwritten.

        Returns
        -------
        Sequence[Mapping]
            The suggestions with their new ``_id``, in order.

        Raises
        ------
        ValueError
            If a suggestion lacks a DOF or a DOF value is not a finite number.
        """
        values = [self._dof_values(suggestion, f"suggestion {index}") for index, suggestion in enumerate(suggestions)]
        registered = []
        for suggestion, dofs in zip(suggestions, values, strict=True):
            identifier = self._new_id()
            self._pending[identifier] = dofs
            registered.append({**suggestion, ID_KEY: identifier})
        return registered

    def register_failures(self, suggestions: Sequence[Mapping]) -> None:
        """
        Forget failed suggestions; their ``_id`` no longer supplies DOF values to :meth:`ingest`.

        Parameters
        ----------
        suggestions : Sequence[Mapping]
            Suggestions carrying ``_id``.
        """
        for suggestion in suggestions:
            identifier = suggestion.get(ID_KEY)
            if not isinstance(identifier, int) or self._pending.pop(identifier, None) is None:
                logger.debug("failed suggestion %r was not pending", identifier)

    # Checkpointable

    def checkpoint(self) -> None:
        """
        Write the model, SGD state, scaling statistics, data, pending suggestions and RNG states with ``torch.save``.

        Raises
        ------
        ValueError
            If the optimizer has no checkpoint path.
        """
        if self._checkpoint_path is None:
            raise ValueError("no checkpoint_path was given to MLPSurrogateOptimizer")
        state = {
            "parameters": self._dofs,
            "context_features": self._features,
            "outputs": self._outputs,
            "options": self._options.model_dump(),
            "model": self._model.state_dict(),
            "sgd": self._sgd.state_dict(),
            "feature_mean": self._feature_mean,
            "feature_std": self._feature_std,
            "output_mean": self._output_mean,
            "output_std": self._output_std,
            "inputs": self._inputs,
            "targets": self._targets,
            "ids": list(self._ids),
            "pending": dict(self._pending),
            "next_id": self._next_id,
            "numpy_rng": self._rng.bit_generator.state,
            "torch_rng": self._torch_generator.get_state(),
        }
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(state, self._checkpoint_path)


def build_mlp(problem: ProblemDefinition, options: MLPOptions, checkpoint_path: Path) -> MLPSurrogateOptimizer:
    """
    Build an :class:`MLPSurrogateOptimizer` for a problem.

    Parameters
    ----------
    problem : ProblemDefinition
        The validated problem; its objectives and outcome-constrained outputs are modeled.
    options : MLPOptions
        Model and search options.
    checkpoint_path : Path
        Checkpoint location; written with the ``.pt`` suffix.

    Returns
    -------
    MLPSurrogateOptimizer
        The optimizer.

    Raises
    ------
    ValueError
        If the problem has a choice DOF.
    """
    choices = [dof.name for dof in problem.dofs if isinstance(dof, ChoiceDOFSpec)]
    if choices:
        raise ValueError(f"mlp-sgd supports range DOFs only; choice DOFs: {choices}")
    weights = problem.objective_weights()
    outputs = list(weights)
    for constraint in problem.outcome_constraints:
        outputs += [name for name in constraint.names if name not in outputs]
    ranges = [dof for dof in problem.dofs if not isinstance(dof, ChoiceDOFSpec)]
    parameters = [(dof.name, float(dof.bounds[0]), float(dof.bounds[1])) for dof in ranges]
    integers = [dof.name for dof in ranges if dof.value_type == "int"]
    return MLPSurrogateOptimizer(
        parameters=parameters,
        context_features=problem.context_features,
        outputs=outputs,
        objective_weights=weights,
        outcome_constraints=problem.outcome_constraints,
        dof_constraints=problem.dof_constraints,
        options=options,
        checkpoint_path=checkpoint_path.with_suffix(".pt"),
        integer_parameters=integers,
    )


MLP_SGD = OptimizerEntry(
    name="mlp-sgd",
    version="1",
    options=MLPOptions,
    build=build_mlp,
    description="MLP surrogate trained by online SGD; contextual; scores random candidates",
)
"""Catalog entry for :class:`MLPSurrogateOptimizer`."""
