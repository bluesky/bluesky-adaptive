from typing import Tuple, Union

import numpy as np
import torch
from botorch.acquisition import UpperConfidenceBound
from databroker.client import BlueskyRun
from numpy.typing import ArrayLike
from tiled.client import from_profile
from xarray import Dataset

from bluesky_adaptive.agents.botorch import SingleTaskGPAgentBase


class OfflineKafka:
    def set_agent(self, *args, **kwargs):
        pass

    def subscribe(self, *args, **kwargs):
        pass

    def start(self, *args, **kwargs):
        pass

    def flush(self, *args, **kwargs):
        pass

    def stop(self, *args, **kwargs):
        pass


class OfflineAgentMixin:
    def __init__(self, tiled_profile, **kwargs):
        qs = None
        kafka_consumer = OfflineKafka()
        kafka_producer = OfflineKafka()
        tiled_data_node = from_profile(tiled_profile)
        tiled_agent_node = from_profile(tiled_profile)
        super().__init__(
            kafka_consumer=kafka_consumer,
            kafka_producer=kafka_producer,
            tiled_agent_node=tiled_agent_node,
            tiled_data_node=tiled_data_node,
            qserver=qs,
            ask_on_tell=False,
            **kwargs,
        )
        self.counter = 0

    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [1.5], dict()

    def unpack_run(self, run: BlueskyRun) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        self.counter += 1
        return self.counter, np.random.rand(10)


class TestGPAgent(OfflineAgentMixin, SingleTaskGPAgentBase):
    ...


def test_gp_agent(tiled_profile, tiled_node):
    # Test tell, ask, and report
    bounds = torch.Tensor([(0, 1), (0, 1)])
    agent = TestGPAgent(tiled_profile, bounds=bounds)
    agent.start()
    agent_uid = agent._compose_run_bundle.start_doc["uid"]
    for i in range(5):
        uid = f"uid{i}"
        x = np.random.rand(2)
        y = 1 - np.sum(np.sin(x)) + np.random.rand() * 0.01
        doc = agent.tell(x, y)
        doc["exp_uid"] = uid
        agent._write_event("tell", doc)
        agent.tell_cache.append(uid)
    agent.generate_report()
    doc, query = agent.ask()
    agent._write_event("ask", doc[0])
    doc, query = agent.ask()
    agent._write_event("ask", doc[0])
    agent.stop()
    assert len(query) == 1
    run = tiled_node[agent_uid]
    xr = run.ask.read()
    assert xr["candidate"].shape == (2, 1, 2)
    assert xr["acquisition_value"].shape == (2,)
    xr = run.report.read()
    assert xr["STATEDICT-beta"].shape == (1,)
    xr = run.tell.read()
    assert isinstance(xr, Dataset)


def test_remodel_from_report(tiled_profile, tiled_node):
    bounds = torch.Tensor([(0, 1), (0, 1)])
    agent = TestGPAgent(tiled_profile, bounds=bounds)
    agent.start()
    agent_uid = agent._compose_run_bundle.start_doc["uid"]
    for i in range(5):
        uid = f"uid{i}"
        x = np.random.rand(2)
        y = 1 - np.sum(np.sin(x)) + np.random.rand() * 0.01
        doc = agent.tell(x, y)
        doc["exp_uid"] = uid
        agent._write_event("tell", doc)
        agent.tell_cache.append(uid)
    agent.generate_report()
    agent.stop()

    new_agent = TestGPAgent(
        tiled_profile, bounds=bounds, partial_acq_function=lambda gp: UpperConfidenceBound(gp, beta=1.5)
    )
    # TODO: Make agent with different acq params to see reset
    acqf, gp = new_agent.remodel_from_report(tiled_node[agent_uid])
    assert (
        gp.get_parameter("covar_module.base_kernel.raw_lengthscale")
        == agent.surrogate_model.get_parameter("covar_module.base_kernel.raw_lengthscale")
    ).all()
    assert acqf.beta == 0.1
