import time

import numpy as np
import pytest
from pydantic import BaseModel

from bluesky_adaptive.protocols import (
    CanRegisterSuggestions,
    Checkpointable,
    ContextAware,
    Optimizer,
    TrialFaultAware,
)
from bluesky_adaptive.server.problem import EntryConfig, ProblemDefinition, ProblemSpec, parse_linear_constraint
from prototypes.passive_server.mlp import MLPOptions, MLPSurrogateOptimizer, build_mlp

A, B, W = 0.1, 0.5, 0.3


def ridge(x, c):
    return np.exp(-(((x - (A + B * c)) / W) ** 2))


def contextual(**options) -> MLPSurrogateOptimizer:
    return MLPSurrogateOptimizer([("x", -1.0, 1.0)], ["c"], ["y"], {"y": 1.0}, options=MLPOptions(**options))


def test_ridge_peak_tracks_context():
    start = time.perf_counter()
    rng = np.random.default_rng(1)
    optimizer = contextual(exploration=0.0)
    for _ in range(4):
        x, c = rng.uniform(-1, 1, 50), rng.uniform(-1, 1, 50)
        optimizer.ingest([{"x": xi, "c": ci, "y": yi} for xi, ci, yi in zip(x, c, ridge(x, c), strict=True)])
    for context in (-0.8, 0.8):
        (top,) = optimizer.suggest_in_context({"c": context})
        assert abs(top["x"] - (A + B * context)) < 0.2
        assert set(top) == {"x", "_id"}
    assert time.perf_counter() - start < 5


def test_context_is_required():
    optimizer = contextual()
    with pytest.raises(ValueError, match="suggest_in_context"):
        optimizer.suggest()
    with pytest.raises(ValueError, match="'c'"):
        optimizer.suggest_in_context({"other": 1.0})
    with pytest.raises(ValueError, match="'c'"):
        optimizer.ingest([{"x": 0.0, "y": 1.0}])


def test_ingest_by_id_uses_pending_dofs():
    optimizer = contextual()
    suggestions = optimizer.suggest_in_context({"c": 0.2}, 3)
    assert len({suggestion["_id"] for suggestion in suggestions}) == 3
    optimizer.ingest([{"_id": suggestion["_id"], "c": 0.2, "y": float(i)} for i, suggestion in enumerate(suggestions)])
    ((best_id, parameters, outcomes),) = optimizer.get_best_points()
    assert best_id == suggestions[2]["_id"]
    assert parameters == {"x": suggestions[2]["x"], "c": 0.2}
    assert outcomes == {"y": 2.0}
    assert not optimizer.pending
    with pytest.raises(ValueError, match=r"\['x'\]"):
        optimizer.ingest([{"_id": suggestions[0]["_id"], "c": 0.2, "y": 1.0}])


def test_register_and_fail_suggestions():
    optimizer = contextual()
    registered = optimizer.register_suggestions([{"x": 0.1, "_id": "a"}, {"x": 0.2, "_id": "a"}])
    assert [suggestion["x"] for suggestion in registered] == [0.1, 0.2]
    first, second = (suggestion["_id"] for suggestion in registered)
    assert first != second
    optimizer.register_failures([registered[0]])
    assert set(optimizer.pending) == {second}
    with pytest.raises(ValueError, match=r"\['x'\]"):
        optimizer.ingest([{"_id": first, "c": 0.0, "y": 1.0}])
    optimizer.ingest([{"_id": second, "c": 0.0, "y": 1.0}])
    assert optimizer.get_best_points()[0][1]["x"] == 0.2
    with pytest.raises(ValueError, match="lacks parameters"):
        optimizer.register_suggestions([{"y": 1.0}])


def test_suggestions_satisfy_dof_constraints():
    constraint = parse_linear_constraint("x1 + x2 <= 0", ["x1", "x2"])
    optimizer = MLPSurrogateOptimizer(
        [("x1", -1.0, 1.0), ("x2", -1.0, 1.0)],
        [],
        ["y"],
        {"y": 1.0},
        dof_constraints=[constraint],
        options=MLPOptions(epochs_per_ingest=20, exploration=0.5),
    )
    suggestions = list(optimizer.suggest(20))
    rng = np.random.default_rng(0)
    points = rng.uniform(-1, 1, (10, 2))
    optimizer.ingest([{"x1": x1, "x2": x2, "y": x1 + x2} for x1, x2 in points])
    suggestions += optimizer.suggest(20)
    assert all(suggestion["x1"] + suggestion["x2"] <= 0 for suggestion in suggestions)


def test_outcome_constraint_steers_suggestion_and_best_point():
    constraint = parse_linear_constraint("g <= 0", ["y", "g"])
    optimizer = MLPSurrogateOptimizer(
        [("x", -1.0, 1.0)], [], ["y", "g"], {"y": 1.0}, outcome_constraints=[constraint], options=MLPOptions(exploration=0.0)
    )
    xs = np.linspace(-1, 1, 41)
    optimizer.ingest([{"x": x, "y": -((x - 0.5) ** 2), "g": x} for x in xs])
    (top,) = optimizer.suggest()
    assert -0.3 < top["x"] <= 0.05
    ((_, parameters, outcomes),) = optimizer.get_best_points()
    assert parameters["x"] == pytest.approx(0.0)
    assert outcomes["g"] <= 0


def test_best_points_need_feasible_data():
    constraint = parse_linear_constraint("g <= 0", ["y", "g"])
    optimizer = MLPSurrogateOptimizer([("x", -1.0, 1.0)], [], ["y", "g"], {"y": 1.0}, outcome_constraints=[constraint])
    with pytest.raises(ValueError, match="no points ingested"):
        optimizer.get_best_points()
    optimizer.ingest([{"x": 0.5, "y": 1.0, "g": 1.0}, {"x": -0.5, "y": float("nan"), "g": -1.0}])
    with pytest.raises(ValueError, match="satisfies the constraints"):
        optimizer.get_best_points()


def test_suggestions_are_uniform_before_min_points():
    optimizer = contextual(min_points=5, exploration=0.0)
    optimizer.ingest([{"x": 0.3, "c": 0.0, "y": 1.0}])
    xs = np.array([suggestion["x"] for suggestion in optimizer.suggest_in_context({"c": 0.0}, 40)])
    assert np.std(xs) > 0.4
    assert np.sum(xs < 0) >= 10 and np.sum(xs > 0) >= 10


class _Params(BaseModel):
    pass


def _problem(dofs, objective="diag2_total", outcome_constraints=()):
    spec = ProblemSpec.model_validate(
        {
            "queueserver": "qs",
            "dofs": dofs,
            "objectives": [{"name": objective}],
            "outcome_constraints": list(outcome_constraints),
            "acquisition_plan": {"name": "adaptive_acquire"},
            "evaluation": {"name": "image-metrics"},
            "optimizer": {"name": "mlp-sgd"},
            "context": {"name": "baseline-signals"},
        }
    )
    return ProblemDefinition.from_spec(
        spec,
        outputs=["diag2_total", "diag2_x_centroid", "diag2_y_centroid"],
        context_features=["dcm_c1-pitch"],
        evaluation=EntryConfig("image-metrics", _Params()),
        optimizer=EntryConfig("mlp-sgd", MLPOptions()),
        context=EntryConfig("baseline-signals", _Params()),
    )


def test_build_mlp_rejects_choice_dofs(tmp_path):
    problem = _problem([{"type": "choice", "name": "filter", "values": ["a", "b"]}])
    with pytest.raises(ValueError, match="filter"):
        build_mlp(problem, MLPOptions(), tmp_path / "optimizer")


def test_build_mlp_contextual(tmp_path):
    problem = _problem(
        [
            {"type": "range", "name": "dcm_c2-pitch", "actuator": "dcm_c2.pitch", "bounds": [-1e-4, 1e-4]},
            {"type": "range", "name": "steps", "bounds": [0.5, 4.5], "value_type": "int"},
        ],
        outcome_constraints=["diag2_y_centroid <= 120"],
    )
    optimizer = build_mlp(problem, MLPOptions(epochs_per_ingest=5), tmp_path / "optimizer")
    for protocol in (Optimizer, ContextAware, Checkpointable, CanRegisterSuggestions, TrialFaultAware):
        assert isinstance(optimizer, protocol)
    assert optimizer.outputs == ("diag2_total", "diag2_y_centroid")
    assert optimizer.context_features == ("dcm_c1-pitch",)
    with pytest.raises(ValueError, match="requires a context"):
        optimizer.suggest()
    suggestions = optimizer.suggest_in_context({"dcm_c1-pitch": 1e-5}, 4)
    assert all(isinstance(s["steps"], int) and 1 <= s["steps"] <= 4 for s in suggestions)
    assert all(-1e-4 <= s["dcm_c2-pitch"] <= 1e-4 for s in suggestions)
    optimizer.ingest([{"_id": s["_id"], "dcm_c1-pitch": 1e-5, "diag2_total": 1.0} for s in suggestions])
    optimizer.checkpoint()
    assert (tmp_path / "optimizer.pt").is_file()
