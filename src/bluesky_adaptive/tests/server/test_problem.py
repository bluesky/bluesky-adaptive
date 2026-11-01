import pytest
from pydantic import BaseModel, ValidationError

from bluesky_adaptive.server.problem import (
    ChoiceDOFSpec,
    EntryConfig,
    ProblemDefinition,
    ProblemSpec,
    RangeDOFSpec,
    parse_linear_constraint,
    parse_linear_objective,
)

NAMES = ("dcm_c2-pitch", "m1.pitch", "a", "b")


class _Empty(BaseModel):
    pass


def _spec(**overrides) -> dict:
    spec = {
        "queueserver": "qs",
        "dofs": [
            {"name": "dcm_c2-pitch", "actuator": "dcm_c2.pitch", "bounds": [-1e-4, 1e-4]},
            {"name": "x", "bounds": [0, 1]},
        ],
        "objectives": [{"name": "flux"}],
        "acquisition_plan": {"name": "adaptive_acquire"},
        "evaluation": {"name": "metrics"},
        "optimizer": {"name": "bo"},
    }
    spec.update(overrides)
    return spec


def _definition(spec: dict, outputs=("flux", "width"), features=()) -> ProblemDefinition:
    return ProblemDefinition.from_spec(
        ProblemSpec.model_validate(spec),
        outputs=outputs,
        context_features=features,
        evaluation=EntryConfig("metrics", _Empty()),
        optimizer=EntryConfig("bo", _Empty()),
    )


@pytest.mark.parametrize(
    ("expression", "terms", "op", "bound"),
    [
        ("dcm_c2-pitch - 2 * m1.pitch <= 1e-4", (("dcm_c2-pitch", 1.0), ("m1.pitch", -2.0)), "<=", 1e-4),
        ("0.5*a + 3 * b >= -2", (("a", 0.5), ("b", 3.0)), ">=", -2.0),
        ("-a + b <= 0", (("a", -1.0), ("b", 1.0)), "<=", 0.0),
        ("- 2.5e-3 * a >= - 1E+2", (("a", -2.5e-3),), ">=", -100.0),
        ("-1.5 * dcm_c2-pitch <= .5", (("dcm_c2-pitch", -1.5),), "<=", 0.5),
        ("a + 2 * a - b >= 1", (("a", 3.0), ("b", -1.0)), ">=", 1.0),
    ],
)
def test_parse_linear_constraint(expression, terms, op, bound):
    constraint = parse_linear_constraint(expression, NAMES)
    assert constraint.terms == terms
    assert (constraint.op, constraint.bound) == (op, pytest.approx(bound))


def test_constraint_canonical_form_reparses_and_measures_violation():
    constraint = parse_linear_constraint("dcm_c2-pitch - 2 * m1.pitch <= 1e-4", NAMES)
    reparsed = parse_linear_constraint(constraint.canonical(), NAMES)
    assert (reparsed.terms, reparsed.op, reparsed.bound) == (constraint.terms, constraint.op, constraint.bound)
    assert constraint.violation({"dcm_c2-pitch": 3e-4, "m1.pitch": 0.0}) == pytest.approx(2e-4)
    assert constraint.violation({"dcm_c2-pitch": 0.0, "m1.pitch": 0.0}) == 0.0


@pytest.mark.parametrize(
    ("expression", "match"),
    [
        ("z <= 1", "unknown name 'z'"),
        ("a < 1", "'<='"),
        ("a<=1", "whitespace"),
        ("a-b <= 1", "binary '-' must be surrounded by whitespace"),
        ("a+b <= 1", "'\\+' in 'a\\+b' must be surrounded by whitespace"),
        ("a <= 1 b", "unexpected 'b'"),
        ("a + b", "expected '<=' or '>='"),
        ("2 a <= 1", "expected '\\*'"),
        ("a <= b", "expected a number"),
    ],
)
def test_parse_linear_constraint_rejects(expression, match):
    with pytest.raises(ValueError, match=match):
        parse_linear_constraint(expression, NAMES)


def test_parse_linear_objective():
    objective = parse_linear_objective("a - 0.5 * m1.pitch", NAMES, minimize=True)
    assert objective.terms == (("a", 1.0), ("m1.pitch", -0.5))
    assert objective.minimize is True


def test_parse_linear_objective_rejects_comparison():
    with pytest.raises(ValueError, match="unexpected '<='"):
        parse_linear_objective("a + b <= 1", NAMES, minimize=False)


def test_dof_type_defaults_to_range_and_discriminates_choice():
    spec = ProblemSpec.model_validate(
        _spec(dofs=[{"name": "x", "bounds": [0, 1]}, {"type": "choice", "name": "filter", "values": ["Al", "Cu"]}])
    )
    assert isinstance(spec.dofs[0], RangeDOFSpec)
    assert isinstance(spec.dofs[1], ChoiceDOFSpec)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"scalarized_objective": {"expression": "flux"}}, id="objectives-and-scalarized"),
        pytest.param({"objectives": []}, id="neither-objective"),
        pytest.param({"dofs": [{"name": "x", "bounds": [0, 1]}, {"name": "x", "bounds": [0, 2]}]}, id="duplicate-dof"),
        pytest.param(
            {
                "dofs": [
                    {"name": "x", "actuator": "m.x", "bounds": [0, 1]},
                    {"name": "y", "actuator": "m.x", "bounds": [0, 1]},
                ]
            },
            id="duplicate-actuator",
        ),
        pytest.param({"dofs": [{"name": "x", "bounds": [1, 0]}]}, id="inverted-bounds"),
        pytest.param({"dofs": [{"name": "x y", "bounds": [0, 1]}]}, id="name-with-space"),
    ],
)
def test_problem_spec_rejects(overrides):
    with pytest.raises(ValidationError):
        ProblemSpec.model_validate(_spec(**overrides))


def test_from_spec_parses_expressions_and_derives_actuators():
    problem = _definition(
        _spec(dof_constraints=["dcm_c2-pitch + x <= 1"], outcome_constraints=["width <= 3"]),
        features=("c1",),
    )
    assert problem.dof_names == ("dcm_c2-pitch", "x")
    assert problem.actuators == ("dcm_c2.pitch",)
    assert problem.dof_constraints[0].terms == (("dcm_c2-pitch", 1.0), ("x", 1.0))
    assert problem.outcome_constraints[0].names == ("width",)
    assert problem.context_features == ("c1",)


@pytest.mark.parametrize(
    ("overrides", "outputs", "features"),
    [
        pytest.param({"objectives": [{"name": "nope"}]}, ("flux",), (), id="objective-not-output"),
        pytest.param(
            {"scalarized_objective": {"expression": "flux - nope"}, "objectives": []}, ("flux",), (), id="scalarized"
        ),
        pytest.param({"dof_constraints": ["x + y <= 1"]}, ("flux",), (), id="dof-constraint-unknown-dof"),
        pytest.param({"outcome_constraints": ["x <= 1"]}, ("flux",), (), id="outcome-constraint-over-dof"),
        pytest.param({}, ("flux", "x"), (), id="dof-output-collision"),
        pytest.param({}, ("flux",), ("x",), id="dof-feature-collision"),
        pytest.param({}, ("flux",), ("flux",), id="output-feature-collision"),
        pytest.param({}, ("flux", "bad name"), (), id="output-not-a-name"),
    ],
)
def test_from_spec_rejects(overrides, outputs, features):
    with pytest.raises(ValueError):
        _definition(_spec(**overrides), outputs=outputs, features=features)


@pytest.mark.parametrize(
    ("overrides", "weights"),
    [
        ({"objectives": [{"name": "flux"}, {"name": "width", "minimize": True}]}, {"flux": 1.0, "width": -1.0}),
        (
            {"objectives": [], "scalarized_objective": {"expression": "flux - 0.5 * width", "minimize": True}},
            {"flux": -1.0, "width": 0.5},
        ),
        ({"objectives": [], "scalarized_objective": {"expression": "2 * flux"}}, {"flux": 2.0}),
    ],
)
def test_objective_weights(overrides, weights):
    assert _definition(_spec(**overrides)).objective_weights() == weights
