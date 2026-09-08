from pathlib import Path

import numpy as np
import pytest
from numpy.typing import ArrayLike

pytest.importorskip("xopt")

from xopt.generators.random import RandomGenerator
from xopt.vocs import VOCS

from bluesky_adaptive.agents.xopt import XoptGeneratorAgentBase
from bluesky_adaptive.utils.offline import OfflineAgent

from ..typing import BlueskyRunLike


class XoptTestAgent(XoptGeneratorAgentBase, OfflineAgent):
    measurement_plan_name = "agent_driven_nap"

    def measurement_plan(self, point: ArrayLike) -> tuple[str, list, dict]:
        return self.measurement_plan_name, [1.0], {}

    @staticmethod
    def unpack_run(run: BlueskyRunLike) -> tuple[float, float]:
        return 0.0, 0.0

    def server_registrations(self) -> None:
        return None


@pytest.fixture
def random_generator():
    vocs = VOCS(
        variables={"x1": [0.0, 1.0], "x2": [0.0, 1.0]},
        objectives={"y1": "MINIMIZE"},
        constraints={},
        constants={},
    )
    return RandomGenerator(vocs=vocs)


def test_construct_from_generator_object(random_generator):
    agent = XoptTestAgent(generator=random_generator, suggest_on_ingest=False)

    assert agent.generator is random_generator
    assert not hasattr(agent, "xopt")


def test_ingest_and_suggest(random_generator):
    agent = XoptTestAgent(generator=random_generator, suggest_on_ingest=False)

    ingest_doc = agent.ingest([0.25, 0.75], [0.5])
    assert ingest_doc["cache_len"] == 1

    docs, points = agent.suggest(batch_size=2)
    assert len(docs) == 2
    assert len(points) == 2
    assert np.asarray(points[0]).shape == (2,)


def test_construct_from_generator_yaml(tmp_path: Path):
    yaml_text = """
name: random
vocs:
  variables:
    x1: [0.0, 1.0]
    x2: [0.0, 1.0]
  objectives:
    y1: MINIMIZE
  constraints: {}
  constants: {}
"""
    yaml_file = tmp_path / "generator.yml"
    yaml_file.write_text(yaml_text)

    agent = XoptTestAgent.from_generator_yaml(yaml_file, suggest_on_ingest=False)
    assert agent.generator.name == "random"
    assert not hasattr(agent, "xopt")


def test_reject_top_level_xopt_fields(tmp_path: Path):
    yaml_text = """
generator:
  name: random
  vocs:
    variables:
      x1: [0.0, 1.0]
    objectives:
      y1: MINIMIZE
    constraints: {}
    constants: {}
evaluator:
  function: module:function
"""
    yaml_file = tmp_path / "bad.yml"
    yaml_file.write_text(yaml_text)

    with pytest.raises(ValueError, match="only the 'generator' key"):
        XoptTestAgent.from_generator_yaml(yaml_file, suggest_on_ingest=False)


def test_basic_optimization_loop_with_agent():
    """Use suggest/ingest loop to minimize a simple 1D quadratic objective."""
    vocs = VOCS(
        variables={"x": [0.0, 1.0]},
        objectives={"y": "MINIMIZE"},
        constraints={},
        constants={},
    )
    generator = RandomGenerator(vocs=vocs)
    agent = XoptTestAgent(generator=generator, suggest_on_ingest=False)

    def objective(x):
        return float((x - 0.25) ** 2)

    best_y = float("inf")
    for _ in range(100):
        docs, points = agent.suggest(batch_size=1)
        assert len(docs) == 1
        assert len(points) == 1
        x = float(np.asarray(points[0])[0])
        y = objective(x)
        agent.ingest([x], [y])
        best_y = min(best_y, y)

    assert best_y < 1e-3
