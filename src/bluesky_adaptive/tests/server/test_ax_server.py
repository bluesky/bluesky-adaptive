from __future__ import annotations

import pytest
from pydantic import ValidationError

from bluesky_adaptive.ax.optimizer import AxOptimizer
from bluesky_adaptive.ax.server import AxOptimizerSpec


def test_ax_spec_rejects_duplicate_names_invalid_bounds_and_random_acquisition():
    with pytest.raises(ValidationError, match="unique"):
        AxOptimizerSpec.model_validate(
            {"dofs": [{"name": "x", "bounds": [0, 1]}], "objectives": [{"name": "x", "minimize": False}]}
        )
    with pytest.raises(ValidationError, match="lower bound"):
        AxOptimizerSpec.model_validate(
            {"dofs": [{"name": "x", "bounds": [1, 1]}], "objectives": [{"name": "score", "minimize": False}]}
        )
    with pytest.raises(ValidationError, match="Random search"):
        AxOptimizerSpec.model_validate(
            {
                "dofs": [{"name": "x", "bounds": [0, 1]}],
                "objectives": [{"name": "score", "minimize": False}],
                "generation": {"method": "random_search", "acquisition_function": "qExpectedImprovement"},
            }
        )


def test_ax_spec_defaults_range_kind_and_builds_optimizer():
    spec = AxOptimizerSpec.model_validate(
        {"dofs": [{"name": "x", "bounds": [0, 1]}], "objectives": [{"name": "score", "minimize": False}]}
    )
    assert spec.dofs[0].kind == "range"
    assert isinstance(spec.build(), AxOptimizer)


def test_restricted_spec_advertises_and_enforces_allowed_names():
    spec_type = AxOptimizerSpec.restricted(dofs=["x", "y"], objectives=["score"])
    schema = spec_type.model_json_schema()["$defs"]
    assert schema["RangeDOFSpec"]["properties"]["name"]["enum"] == ["x", "y"]
    assert schema["ChoiceDOFSpec"]["properties"]["name"]["enum"] == ["x", "y"]
    # A single allowed value is still listed under "enum", not only "const".
    assert schema["ObjectiveSpec"]["properties"]["name"]["enum"] == ["score"]

    valid = {
        "dofs": [
            {"name": "x", "bounds": [0, 1]},
            {"kind": "choice", "name": "y", "values": [1, 2], "parameter_type": "int"},
        ],
        "objectives": [{"name": "score", "minimize": False}],
    }
    assert isinstance(spec_type.model_validate(valid), AxOptimizerSpec)
    with pytest.raises(ValidationError) as unknown_dof:
        spec_type.model_validate({**valid, "dofs": [{"name": "z", "bounds": [0, 1]}]})
    assert unknown_dof.value.errors()[0]["loc"] == ("dofs", 0, "range", "name")
    with pytest.raises(ValidationError) as unknown_objective:
        spec_type.model_validate({**valid, "objectives": [{"name": "other", "minimize": False}]})
    assert unknown_objective.value.errors()[0]["loc"] == ("objectives", 0, "name")

    # Names left unrestricted accept anything.
    dofs_only = AxOptimizerSpec.restricted(dofs=["x"])
    assert dofs_only.model_validate({**valid, "dofs": valid["dofs"][:1], "objectives": [{"name": "any", "minimize": True}]})
