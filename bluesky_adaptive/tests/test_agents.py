from typing import Sequence, Tuple, Union

from bluesky_live.run_builder import RunBuilder
from databroker.client import BlueskyRun
from numpy.typing import ArrayLike
from tiled.client import from_profile

from bluesky_adaptive.agents.base import Agent


class TestCommunicationAgent(Agent):
    measurement_plan_name = "test_plan"

    def __init__(
        self, pub_topic, sub_topic, kafka_bootstrap_servers, broker_authorization_config, tiled_profile, **kwargs
    ):
        super().__init__(
            kafka_group_id="test.communication.group",
            kafka_bootstrap_servers=kafka_bootstrap_servers,
            kafka_producer_config=broker_authorization_config,
            kafka_consumer_config={"auto.offset.reset": "latest"},
            publisher_topic=pub_topic,
            subscripion_topics=[sub_topic],
            data_profile_name=tiled_profile,
            agent_profile_name=tiled_profile,
            qserver_host=None,
            qserver_api_key="SECRET",
            **kwargs,
        )

    def measurement_plan_args(point) -> list:
        return list()

    def measurement_plan_kwargs(point) -> dict:
        return dict()

    def unpack_run(run: BlueskyRun) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        return 0, 0

    def report(self, report_number: int = 0) -> dict:
        return dict(agent_name=self.instance_name, report=f"report_{report_number}")

    def ask(self, batch_size: int = 1) -> Tuple[dict, Sequence]:
        return (dict(agent_name=self.instance_name, report=f"ask_{batch_size}"), [0 for _ in range(batch_size)])

    def tell(self, x, y) -> dict:
        return dict(x=x, y=y)

    def start(self):
        """Start without kafka consumer start"""
        self.builder = RunBuilder(metadata=self.metadata)
        self.agent_catalog.v1.insert("start", self.builder._cache.start_doc)


def test_agent_doc_stream(temporary_topics, kafka_bootstrap_servers, broker_authorization_config, tiled_profile):
    """Test the ability to write agent actions as bluesky run and readback in tiled"""
    cat = from_profile(tiled_profile)

    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub, sub):
        agent = TestCommunicationAgent(
            pub, sub, kafka_bootstrap_servers, broker_authorization_config, tiled_profile
        )
        agent.start()
        doc, _ = agent.ask(1)
        ask_uid = agent._write_event("ask", doc)
        doc = agent.tell(0, 0)
        _ = agent._write_event("tell", doc)
        doc = agent.report()
        _ = agent._write_event("report", doc)

        documents = list(cat[-1].documents())
        assert documents[0][0] == "start"
        assert documents[1][0] == "descriptor"
        assert documents[2][0] == "event_page"
        assert "report" in cat[-1].metadata["summary"]["stream_names"]
        assert "ask" in cat[-1].metadata["summary"]["stream_names"]
        assert "tell" in cat[-1].metadata["summary"]["stream_names"]
        assert isinstance(ask_uid, list)
