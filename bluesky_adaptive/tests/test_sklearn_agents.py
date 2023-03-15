import time as ttime
from typing import Tuple, Union

import numpy as np
import pytest
from bluesky import RunEngine
from bluesky.plans import count
from bluesky_kafka import Publisher
from bluesky_queueserver_api.http import REManagerAPI
from databroker.client import BlueskyRun
from numpy.typing import ArrayLike
from sklearn.cluster import KMeans
from sklearn.decomposition import NMF, PCA
from tiled.client import from_profile

from bluesky_adaptive.agents.base import AgentConsumer
from bluesky_adaptive.agents.sklearn import ClusterAgentBase, DecompositionAgentBase


class DummyAgentMixin:
    def __init__(
        self, pub_topic, sub_topic, kafka_bootstrap_servers, broker_authorization_config, tiled_profile, **kwargs
    ):
        qs = REManagerAPI(http_server_uri=None)
        qs.set_authorization_key(api_key="SECRET")

        kafka_consumer = AgentConsumer(
            topics=[sub_topic],
            bootstrap_servers=kafka_bootstrap_servers,
            group_id="test.communication.group",
            consumer_config={"auto.offset.reset": "earliest"},
        )

        kafka_producer = Publisher(
            topic=pub_topic,
            bootstrap_servers=kafka_bootstrap_servers,
            key="",
            producer_config=broker_authorization_config,
        )

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


class TestDecompAgent(DummyAgentMixin, DecompositionAgentBase):
    ...


class TestClusterAgent(DummyAgentMixin, ClusterAgentBase):
    ...


@pytest.mark.parametrize("estimator", [PCA(2), NMF(2)], ids=["PCA", "NMF"])
def test_decomp_agent(
    estimator, temporary_topics, kafka_bootstrap_servers, broker_authorization_config, tiled_profile, tiled_node
):
    """Tests decomposition agents reporting and readback of reports."""
    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub_topic, sub_topic):
        agent = TestDecompAgent(
            pub_topic,
            sub_topic,
            kafka_bootstrap_servers,
            broker_authorization_config,
            tiled_profile,
            estimator=estimator,
        )
        agent.start()
        for i in range(5):
            agent.tell(float(i), np.random.rand(10))
            agent.tell_cache.append(f"uid{i}")  # dummy uid
        agent.generate_report()
        xr = tiled_node[-1].report.read()
        assert xr.components.shape == (1, 2, 10)  # shape after 1 report
        assert xr.latest_data[-1].data == "uid4"  # most recent uid

        for i in range(5, 10):
            agent.tell(float(i), np.random.rand(10))
            agent.tell_cache.append(f"uid{i}")  # dummy uid
        agent.generate_report()
        assert "report" in tiled_node[-1]

        # Letting mongo catch up then checking for 2 reports
        now = ttime.monotonic()
        while len(tiled_node[-1].report.read().time) == 1:
            ttime.sleep(0.5)
            if ttime.monotonic() - now > 10:
                break

        xr = tiled_node[-1].report.read()
        assert xr.components.shape == (2, 2, 10)
        assert xr.latest_data[-1].data == "uid9"
        agent.stop()


@pytest.mark.parametrize("estimator", [PCA(2), NMF(2)], ids=["PCA", "NMF"])
def test_decomp_remodel_from_report(
    estimator,
    temporary_topics,
    kafka_bootstrap_servers,
    broker_authorization_config,
    tiled_profile,
    tiled_node,
    hw,
):
    """Tests the rebuilding of the model from a given report, including the varaible shape pieces."""
    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub_topic, sub_topic):
        agent = TestDecompAgent(
            pub_topic,
            sub_topic,
            kafka_bootstrap_servers,
            broker_authorization_config,
            tiled_profile,
            estimator=estimator,
        )
        agent.start()
        agent_uid = agent._compose_run_bundle.start_doc["uid"]
        publisher = Publisher(
            topic=sub_topic,
            bootstrap_servers=kafka_bootstrap_servers,
            producer_config=broker_authorization_config,
            key=f"{sub_topic}.key",
            flush_on_stop_doc=True,
        )
        RE = RunEngine()
        RE.subscribe(publisher)
        RE.subscribe(tiled_node.v1.insert)

        for i in range(5):
            RE(count([hw.det]))

        # Letting mongo/kafka catch up to start/stop + 7 tells
        now = ttime.monotonic()
        while len(list(tiled_node[agent_uid].documents())) != 7:
            ttime.sleep(0.5)
            if ttime.monotonic() - now > 60:
                break
        assert "tell" in tiled_node[agent_uid]
        agent.generate_report()
        agent.stop()

        # Letting mongo/kafka catch up to report
        now = ttime.monotonic()
        while not ("report" in tiled_node[agent_uid]):
            ttime.sleep(0.5)
            if ttime.monotonic() - now > 30:
                break
        model, data = agent.remodel_from_report(tiled_node[agent_uid])
        assert isinstance(model, type(estimator))
        assert data["components"].shape[0] == 2
        assert data["weights"].shape == (5, 2)
        assert len(data["observables"]) == 5
        assert len(data["independent_vars"]) == 5


@pytest.mark.parametrize("estimator", [KMeans(2)], ids=["KMeans"])
def test_cluster_agent(
    estimator, temporary_topics, kafka_bootstrap_servers, broker_authorization_config, tiled_profile, tiled_node
):
    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub_topic, sub_topic):
        agent = TestClusterAgent(
            pub_topic,
            sub_topic,
            kafka_bootstrap_servers,
            broker_authorization_config,
            tiled_profile,
            estimator=estimator,
        )
        agent.start()
        for i in range(5):
            agent.tell(float(i), np.random.rand(10))
            agent.tell_cache.append(f"uid{i}")  # dummy uid
        agent.generate_report()
        xr = tiled_node[-1].report.read()
        assert xr.cluster_centers.shape == (1, 2, 10)  # shape after 1 report
        assert xr.latest_data[-1].data == "uid4"  # most recent uid

        for i in range(5, 10):
            agent.tell(float(i), np.random.rand(10))
            agent.tell_cache.append(f"uid{i}")  # dummy uid
        agent.generate_report()
        assert "report" in tiled_node[-1]

        # Letting mongo catch up then checking for 2 reports
        now = ttime.monotonic()
        while len(tiled_node[-1].report.read().time) == 1:
            ttime.sleep(0.5)
            if ttime.monotonic() - now > 10:
                break

        xr = tiled_node[-1].report.read()
        assert xr.cluster_centers.shape == (2, 2, 10)
        assert xr.latest_data[-1].data == "uid9"
        agent.stop()


@pytest.mark.parametrize("estimator", [KMeans(2)], ids=["KMeans"])
def test_cluster_remodel_from_report(
    estimator,
    temporary_topics,
    kafka_bootstrap_servers,
    broker_authorization_config,
    tiled_profile,
    tiled_node,
    hw,
):
    """Tests the rebuilding of the model from a given report, including the varaible shape pieces."""
    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub_topic, sub_topic):
        agent = TestClusterAgent(
            pub_topic,
            sub_topic,
            kafka_bootstrap_servers,
            broker_authorization_config,
            tiled_profile,
            estimator=estimator,
        )
        agent.start()
        agent_uid = agent._compose_run_bundle.start_doc["uid"]
        publisher = Publisher(
            topic=sub_topic,
            bootstrap_servers=kafka_bootstrap_servers,
            producer_config=broker_authorization_config,
            key=f"{sub_topic}.key",
            flush_on_stop_doc=True,
        )
        RE = RunEngine()
        RE.subscribe(publisher)
        RE.subscribe(tiled_node.v1.insert)

        for i in range(5):
            RE(count([hw.det]))

        # Letting mongo/kafka catch up to start/stop + 7 tells
        now = ttime.monotonic()
        while len(list(tiled_node[agent_uid].documents())) != 7:
            ttime.sleep(0.5)
            if ttime.monotonic() - now > 60:
                break
        assert "tell" in tiled_node[agent_uid]
        agent.generate_report()
        agent.stop()

        # Letting mongo/kafka catch up to report
        now = ttime.monotonic()
        while not ("report" in tiled_node[agent_uid]):
            ttime.sleep(0.5)
            if ttime.monotonic() - now > 30:
                break
        model, data = agent.remodel_from_report(tiled_node[agent_uid])
        assert isinstance(model, type(estimator))
        assert data["cluster_centers"].shape[0] == 2
        assert data["distances"].shape == (5, 2)
        assert data["clusters"].shape == (5,)
        assert len(data["observables"]) == 5
        assert len(data["independent_vars"]) == 5
