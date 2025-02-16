from typing import Tuple, Union

import numpy as np
import torch
from botorch.acquisition import UpperConfidenceBound
from numpy.typing import ArrayLike
from xarray import Dataset

from bluesky_adaptive.agents.botorch import SingleTaskGPAgentBase
from bluesky_adaptive.utils.offline import OfflineAgent

from ..typing import BlueskyRunLike


class GPTestAgent(SingleTaskGPAgentBase, OfflineAgent):
    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [1.5], dict()

    def unpack_run(self, run: BlueskyRunLike) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        self.counter += 1
        return self.counter, np.random.rand(10)


def test_gp_agent(catalog):
    # Test ingest, suggest, and report; uses Tiled functionality
    bounds = torch.Tensor([(0, 1), (0, 1)])
    agent = GPTestAgent(bounds=bounds, tiled_data_node=catalog, tiled_agent_node=catalog)
    agent.start()
    agent_uid = agent._compose_run_bundle.start_doc["uid"]
    for i in range(5):
        uid = f"uid{i}"
        x = np.random.rand(2)
        y = 1 - np.sum(np.sin(x)) + np.random.rand() * 0.01
        doc = agent.ingest(x, y)
        doc["exp_uid"] = uid
        agent._write_event("ingest", doc)
        agent.known_uid_cache.append(uid)
    agent.generate_report()
    doc, query = agent.suggest()
    agent._write_event("suggest", doc[0])
    doc, query = agent.suggest()
    agent._write_event("suggest", doc[0])
    agent.stop()
    assert len(query) == 1
    run = catalog[agent_uid]
    xr = run.suggest.read()
    assert xr["candidate"].shape == (2, 1, 2)
    assert xr["acquisition_value"].shape == (2,)
    xr = run.report.read()
    assert xr["STATEDICT-beta"].shape == (1,)
    xr = run.ingest.read()
    assert isinstance(xr, Dataset)


def test_remodel_from_report(catalog):
    bounds = torch.Tensor([(0, 1), (0, 1)])
    agent = GPTestAgent(bounds=bounds, tiled_data_node=catalog, tiled_agent_node=catalog)
    agent.start()
    agent_uid = agent._compose_run_bundle.start_doc["uid"]
    for i in range(5):
        uid = f"uid{i}"
        x = np.random.rand(2)
        y = 1 - np.sum(np.sin(x)) + np.random.rand() * 0.01
        doc = agent.ingest(x, y)
        doc["exp_uid"] = uid
        agent._write_event("ingest", doc)
        agent.known_uid_cache.append(uid)
    agent.generate_report()
    agent.stop()

    new_agent = GPTestAgent(
        bounds=bounds,
        partial_acq_function=lambda gp: UpperConfidenceBound(gp, beta=1.5),
    )
    acqf, gp = new_agent.remodel_from_report(catalog[agent_uid])
    try:
        param = gp.get_parameter("covar_module.base_kernel.raw_lengthscale")
        assert (param == agent.surrogate_model.get_parameter("covar_module.base_kernel.raw_lengthscale")).all()
    except AttributeError:
        param = gp.get_parameter("covar_module.raw_lengthscale")
        assert (param == agent.surrogate_model.get_parameter("covar_module.raw_lengthscale")).all()
    assert acqf.beta == 0.1
