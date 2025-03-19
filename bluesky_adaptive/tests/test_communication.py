"""Testing the communication patterns of Bluesky Adaptive Async Agents"""

import os
from typing import Sequence, Tuple, Union

import pytest
from bluesky_kafka import Publisher
from bluesky_queueserver_api.http import REManagerAPI
from event_model import compose_run
from numpy.typing import ArrayLike
from tiled.client import from_profile

from bluesky_adaptive.agents.base import Agent, AgentConsumer

from ..typing import BlueskyRunLike


class BasicCommunicationAgent(Agent):
    measurement_plan_name = "agent_driven_nap"
    instance_name = "bob"

    def __init__(
        self, pub_topic, sub_topic, kafka_bootstrap_servers, kafka_producer_config, tiled_profile, **kwargs
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
            producer_config=kafka_producer_config,
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
        self.count = 0

    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [1.5], dict()

    def unpack_run(self, run: BlueskyRunLike) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        return 0, 0

    def report(self, report_number: int = 0) -> dict:
        return dict(agent_name=self.instance_name, report=f"report_{report_number}")

    def suggest(self, batch_size: int = 1) -> Tuple[dict, Sequence]:
        return (
            [dict(agent_name=self.instance_name, report=f"suggest_{batch_size}")],
            [0 for _ in range(batch_size)],
        )

    def ingest(self, x, y) -> dict:
        self.count += 1
        return dict(x=x, y=y)

    def start(self):
        """Start without kafka consumer start"""
        self._compose_run_bundle = compose_run(metadata=self.metadata)
        self.agent_catalog.v1.insert("start", self._compose_run_bundle.start_doc)

    def server_registrations(self) -> None:
        return None


def test_agent_connection(temporary_topics, kafka_bootstrap_servers, kafka_producer_config, tiled_profile):
    """Test agent connection to http-server via RE_Manager API"""
    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub, sub):
        agent = BasicCommunicationAgent(pub, sub, kafka_bootstrap_servers, kafka_producer_config, tiled_profile)
        status = agent.re_manager.status()
        assert isinstance(status, dict)
        assert "worker_environment_exists" in status

        # add an item to the queue
        if not status["worker_environment_exists"]:
            agent.re_manager.environment_open()
        agent.re_manager.queue_clear()
        agent._add_to_queue([1], "uid")
        assert agent.re_manager.status()["items_in_queue"] == 1


def test_run_plan(temporary_topics, kafka_bootstrap_servers, kafka_producer_config, tiled_profile):
    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub, sub):
        agent = BasicCommunicationAgent(pub, sub, kafka_bootstrap_servers, kafka_producer_config, tiled_profile)
        # add an item to the queue
        if not agent.re_manager.status()["worker_environment_exists"]:
            agent.re_manager.environment_open()
        agent.re_manager.queue_clear()
        agent._add_to_queue([1], "uid")
        agent.re_manager.queue_start()


@pytest.mark.skipif(
    condition=os.environ.get("GITHUB_ACTIONS") == "true",
    reason="Comms Test producing seg faults on GHA.",
)
def test_agent_doc_stream(temporary_topics, kafka_bootstrap_servers, kafka_producer_config, tiled_profile):
    """Test the ability to write agent actions as bluesky run and readback in tiled"""
    cat = from_profile(tiled_profile)

    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub, sub):
        agent = BasicCommunicationAgent(pub, sub, kafka_bootstrap_servers, kafka_producer_config, tiled_profile)
        agent.start()
        docs, _ = agent.suggest(1)
        suggest_uid = agent._write_event("suggest", docs[0])
        doc = agent.ingest(0, 0)
        _ = agent._write_event("ingest", doc)
        doc = agent.report()
        _ = agent._write_event("report", doc)

        documents = list(cat[-1].documents())
        assert documents[0][0] == "start"
        assert documents[1][0] == "descriptor"
        assert documents[2][0] == "event_page"
        assert "report" in cat[-1].metadata["summary"]["stream_names"]
        assert "suggest" in cat[-1].metadata["summary"]["stream_names"]
        assert "ingest" in cat[-1].metadata["summary"]["stream_names"]
        assert isinstance(suggest_uid, str)
