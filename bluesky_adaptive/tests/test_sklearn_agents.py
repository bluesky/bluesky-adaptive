from typing import Tuple, Union

import numpy as np
import pytest
from bluesky_kafka import Publisher
from bluesky_live.run_builder import RunBuilder
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
            consumer_config={"auto.offset.reset": "latest"},
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
            **kwargs,
        )

    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [1.5], dict()

    def unpack_run(self, run: BlueskyRun) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        return 0, 0

    def start(self):
        """Start without kafka consumer start"""
        self.builder = RunBuilder(metadata=self.metadata)
        self.agent_catalog.v1.insert("start", self.builder._cache.start_doc)

    def server_registrations(self) -> None:
        return None


class TestDecompAgent(DummyAgentMixin, DecompositionAgentBase):
    ...


class TestClusterAgent(DummyAgentMixin, ClusterAgentBase):
    ...


@pytest.mark.parametrize("estimator", [PCA(2), NMF(2)])
def test_decomp_agent(
    estimator, temporary_topics, kafka_bootstrap_servers, broker_authorization_config, tiled_profile, tiled_node
):
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
            agent.tell_cache.append(i)  # dummy uid

        agent.generate_report()
        assert "report" in tiled_node[-1]
        # TODO: Test shapes and reading of output

        agent.stop()


@pytest.mark.parametrize("estimator", [KMeans(2)])
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
            agent.tell_cache.append(i)  # dummy uid

        agent.generate_report()
        assert "report" in tiled_node[-1]
        # TODO: Test shapes and reading of output

        agent.stop()
