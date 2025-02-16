import os
import time as ttime
from typing import Tuple, Union

import numpy as np
import pytest
from bluesky import RunEngine
from bluesky.plans import count
from numpy.typing import ArrayLike
from sklearn.cluster import KMeans
from sklearn.decomposition import NMF, PCA

from bluesky_adaptive.agents.sklearn import ClusterAgentBase, DecompositionAgentBase
from bluesky_adaptive.utils.offline import OfflineAgent, OfflineProducer

from ..typing import BlueskyRunLike


class DummyAgentMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, suggest_on_ingest=False, **kwargs)
        self.counter = 0

    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [1.5], dict()

    def unpack_run(self, run: BlueskyRunLike) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        self.counter += 1
        return self.counter, np.random.rand(10)


class DecompTestAgent(DummyAgentMixin, DecompositionAgentBase, OfflineAgent): ...


class ClusterTestAgent(DummyAgentMixin, ClusterAgentBase, OfflineAgent): ...


@pytest.mark.parametrize("estimator", [PCA(2), NMF(2)], ids=["PCA", "NMF"])
def test_decomp_agent(estimator):
    """Tests decomposition agents reporting and readback of reports."""
    agent = DecompTestAgent(
        estimator=estimator,
    )
    agent.start()
    for i in range(5):
        agent.ingest(float(i), np.random.rand(10))
        agent.known_uid_cache.append(f"uid{i}")  # dummy uid
    doc = agent.report()
    components = doc["components"]
    assert components.shape == (2, 10)  # shape after 1 report
    assert doc["latest_data"] == "uid4"  # most recent uid

    for i in range(5, 10):
        agent.ingest(float(i), np.random.rand(10))
        agent.known_uid_cache.append(f"uid{i}")  # dummy uid
    doc = agent.report()
    components = doc["components"]
    assert components.shape == (2, 10)  # shape after 1 report
    assert doc["latest_data"] == "uid9"  # most recent uid

    agent.stop()


@pytest.mark.xfail(
    os.environ.get("GITHUB_ACTIONS") == "true",
    raises=TimeoutError,
    reason="Databroker timeout awaiting for documents to write",
)  # Allow timeout in GHA CI/CD
@pytest.mark.parametrize("estimator", [PCA(2), NMF(2)], ids=["PCA", "NMF"])
def test_decomp_remodel_from_report(
    estimator,
    catalog,
    hw,
):
    """Tests the rebuilding of the model from a given report, including the varaible shape pieces."""
    agent = DecompTestAgent(estimator=estimator, tiled_data_node=catalog, tiled_agent_node=catalog)
    agent.start()
    agent_uid = agent._compose_run_bundle.start_doc["uid"]
    publisher = OfflineProducer(agent.kafka_consumer.topic)

    RE = RunEngine()
    RE.subscribe(publisher)
    RE.subscribe(catalog.v1.insert)

    for _ in range(5):
        RE(count([hw.det]))
        agent.kafka_consumer.trigger()

    assert "ingest" in catalog[agent_uid]
    assert len(catalog[agent_uid].ingest.data["time"]) == 5
    agent.generate_report()

    # Letting databroker catch up to report. Should take little time
    now = ttime.monotonic()
    while not ("report" in catalog[agent_uid]):
        ttime.sleep(0.1)
        if ttime.monotonic() - now > 3:
            raise TimeoutError
    agent.stop()
    model, data = agent.remodel_from_report(catalog[agent_uid])
    assert isinstance(model, type(estimator))
    assert data["components"].shape[0] == 2
    assert data["weights"].shape == (5, 2)
    assert len(data["observables"]) == 5
    assert len(data["independent_vars"]) == 5


@pytest.mark.parametrize("estimator", [KMeans(2)], ids=["KMeans"])
def test_cluster_agent(estimator):
    agent = ClusterTestAgent(
        estimator=estimator,
    )
    agent.start()
    for i in range(5):
        agent.ingest(float(i), np.random.rand(10))
        agent.known_uid_cache.append(f"uid{i}")  # dummy uid
    doc = agent.report()
    cluster_centers = doc["cluster_centers"]
    assert cluster_centers.shape == (2, 10)  # shape after 1 report
    assert doc["latest_data"] == "uid4"  # most recent uid

    for i in range(5, 10):
        agent.ingest(float(i), np.random.rand(10))
        agent.known_uid_cache.append(f"uid{i}")  # dummy uid
    doc = agent.report()
    cluster_centers = doc["cluster_centers"]
    assert cluster_centers.shape == (2, 10)  # shape after 2 reports the same
    assert doc["latest_data"] == "uid9"  # most recent uid
    agent.stop()


@pytest.mark.xfail(
    os.environ.get("GITHUB_ACTIONS") == "true",
    raises=TimeoutError,
    reason="Databroker timeout awaiting for documents to write",
)  # Allow timeout in GHA CI/CD
@pytest.mark.parametrize("estimator", [KMeans(2)], ids=["KMeans"])
def test_cluster_remodel_from_report(
    estimator,
    catalog,
    hw,
):
    """Tests the rebuilding of the model from a given report, including the varaible shape pieces."""
    agent = ClusterTestAgent(estimator=estimator, tiled_data_node=catalog, tiled_agent_node=catalog)
    agent.start()
    agent_uid = agent._compose_run_bundle.start_doc["uid"]
    publisher = OfflineProducer(agent.kafka_consumer.topic)

    RE = RunEngine()
    RE.subscribe(publisher)
    RE.subscribe(catalog.v1.insert)

    for _ in range(5):
        RE(count([hw.det]))
        agent.kafka_consumer.trigger()

    assert "ingest" in catalog[agent_uid]
    assert len(catalog[agent_uid].ingest.data["time"]) == 5
    agent.generate_report()

    # Letting mongo/kafka catch up to report
    now = ttime.monotonic()
    while not ("report" in catalog[agent_uid]):
        ttime.sleep(0.1)
        if ttime.monotonic() - now > 3:
            raise TimeoutError
    agent.stop()
    model, data = agent.remodel_from_report(catalog[agent_uid])
    assert isinstance(model, type(estimator))
    assert data["cluster_centers"].shape[0] == 2
    assert data["distances"].shape == (5, 2)
    assert data["clusters"].shape == (5,)
    assert len(data["observables"]) == 5
    assert len(data["independent_vars"]) == 5
