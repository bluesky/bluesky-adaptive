import time as ttime
from typing import Sequence, Tuple, Union

from bluesky_live.run_builder import RunBuilder
from databroker.client import BlueskyRun
from numpy.typing import ArrayLike
from tiled.client import from_profile

from bluesky_adaptive.agents.base import Agent
from bluesky_adaptive.agents.simple import SequentialAgentBase

from bluesky_queueserver_api.http import REManagerAPI


class TestCommunicationAgent(Agent):
    measurement_plan_name = "agent_driven_nap"
    instance_name = 'bob'

    def __init__(
        self, pub_topic, sub_topic, kafka_bootstrap_servers, broker_authorization_config, tiled_profile, **kwargs
    ):
        qs = REManagerAPI(http_server_uri=None)
        qs.set_authorization_key(api_key="SECRET")

        super().__init__(
            kafka_group_id="test.communication.group",
            kafka_bootstrap_servers=kafka_bootstrap_servers,
            kafka_producer_config=broker_authorization_config,
            kafka_consumer_config={"auto.offset.reset": "latest"},
            publisher_topic=pub_topic,
            subscripion_topics=[sub_topic],
            data_profile_name=tiled_profile,
            agent_profile_name=tiled_profile,
            qserver=qs,
            **kwargs,
        )
        self.count = 0

    @staticmethod
    def measurement_plan_args(point) -> list:
        return [1.5]

    @staticmethod
    def measurement_plan_kwargs(point) -> dict:
        return dict()

    def unpack_run(self, run: BlueskyRun) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        return 0, 0

    def report(self, report_number: int = 0) -> dict:
        return dict(agent_name=self.instance_name, report=f"report_{report_number}")

    def ask(self, batch_size: int = 1) -> Tuple[dict, Sequence]:
        return (dict(agent_name=self.instance_name, report=f"ask_{batch_size}"), [0 for _ in range(batch_size)])

    def tell(self, x, y) -> dict:
        self.count += 1
        return dict(x=x, y=y)

    def start(self):
        """Start without kafka consumer start"""
        self.builder = RunBuilder(metadata=self.metadata)
        self.agent_catalog.v1.insert("start", self.builder._cache.start_doc)


def test_agent_connection(temporary_topics, kafka_bootstrap_servers, broker_authorization_config, tiled_profile):
    """Test agent connection to http-server via RE_Manager API"""
    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub, sub):
        agent = TestCommunicationAgent(
            pub, sub, kafka_bootstrap_servers, broker_authorization_config, tiled_profile
        )
        status = agent.re_manager.status()
        assert isinstance(status, dict)
        assert "worker_environment_exists" in status

        # add an item to the queue
        if not status["worker_environment_exists"]:
            agent.re_manager.environment_open()
        agent.re_manager.queue_clear()
        agent._add_to_queue([1], "uid")
        assert agent.re_manager.status()["items_in_queue"] == 1


def test_run_plan(temporary_topics, kafka_bootstrap_servers, broker_authorization_config, tiled_profile):
    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub, sub):
        agent = TestCommunicationAgent(
            pub, sub, kafka_bootstrap_servers, broker_authorization_config, tiled_profile
        )
        # add an item to the queue
        if not agent.re_manager.status()["worker_environment_exists"]:
            agent.re_manager.environment_open()
        agent.re_manager.queue_clear()
        agent._add_to_queue([1], "uid")
        agent.re_manager.queue_start()


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


def test_feedback_to_queue(
    temporary_topics, kafka_bootstrap_servers, broker_authorization_config, tiled_profile, publisher_factory
):
    """
    Publish a stop document to kafka with the right start uid for a manufactured run.
    Ensure something lands on the queue.
    This test seems very brittle, sometimes failing with the following stderr:
    GroupCoordinator/1001: Timed out LeaveGroupRequest in flight (after 5003ms, timeout #0):
        possibly held back by preceeding blocking JoinGroupRequest with timeout in 292896ms
    GroupCoordinator/1001: Timed out 1 in-flight, 0 retry-queued, 0 out-queue, 0 partially-sent requests
    GroupCoordinator: 1 request(s) timed out: disconnect (after 10110ms in state UP)
    """
    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub_topic, sub_topic):
        agent = TestCommunicationAgent(
            pub_topic, sub_topic, kafka_bootstrap_servers, broker_authorization_config, tiled_profile
        )
        agent.start()
        agent.enable_continuous_suggesting()
        agent.enable_direct_to_queue()

        if not agent.re_manager.status()["worker_environment_exists"]:
            agent.re_manager.environment_open()
        agent.re_manager.queue_clear()

        # Drop a Bluesky Run into the databroker
        builder = RunBuilder(metadata={})
        start = builder._cache.start_doc
        agent.exp_catalog.v1.insert("start", start)
        builder.close()
        agent.exp_catalog.v1.insert("stop", builder._cache.stop_doc)

        # Consume for a limited time only. Publish stop doc to reference Run in continue_polling loop
        # Overriden agent.start() takes care of the rest
        def consume_for_time(sec=3):
            # Fake publish a stop doc with the right uuid.
            fake_publisher = publisher_factory(
                topic=sub_topic,
                key=f"{sub_topic}.key",
                flush_on_stop_doc=True,
            )
            start_time = ttime.monotonic()

            def until_time():
                if ttime.monotonic() > start_time + sec:
                    return False
                else:
                    fake_publisher("start", builder._cache.start_doc)
                    fake_publisher("stop", builder._cache.stop_doc)
                    ttime.sleep(0.1)
                    return True

            agent.kafka_consumer.start(continue_polling=until_time)

        consume_for_time()
        assert agent.count > 0
        assert agent.re_manager.status()["items_in_queue"] > 0


class TestSequentialAgent(SequentialAgentBase):
    measurement_plan_name = "agent_driven_nap"

    def __init__(
        self, pub_topic, sub_topic, kafka_bootstrap_servers, broker_authorization_config, tiled_profile, **kwargs
    ):
        qs = REManagerAPI(http_server_uri=None)
        qs.set_authorization_key(api_key="SECRET")

        super().__init__(
            kafka_group_id="test.communication.group",
            kafka_bootstrap_servers=kafka_bootstrap_servers,
            kafka_producer_config=broker_authorization_config,
            kafka_consumer_config={"auto.offset.reset": "latest"},
            publisher_topic=pub_topic,
            subscripion_topics=[sub_topic],
            data_profile_name=tiled_profile,
            agent_profile_name=tiled_profile,
            qserver=qs,
            **kwargs,
        )
        self.count = 0

    @staticmethod
    def measurement_plan_args(point) -> list:
        return [1.5]

    @staticmethod
    def measurement_plan_kwargs(point) -> dict:
        return dict()

    def unpack_run(self, run: BlueskyRun) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        return 0, 0

    def start(self):
        """Start without kafka consumer start"""
        self.builder = RunBuilder(metadata=self.metadata)
        self.agent_catalog.v1.insert("start", self.builder._cache.start_doc)


def test_sequntial_agent(temporary_topics, kafka_bootstrap_servers, broker_authorization_config, tiled_profile):
    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub_topic, sub_topic):
        agent = TestSequentialAgent(
            pub_topic,
            sub_topic,
            kafka_bootstrap_servers,
            broker_authorization_config,
            tiled_profile,
            sequence=[1, 2, 3],
        )
        agent.start()

        for i in [1, 2, 3]:
            _, points = agent.ask()
            assert len(points) == 1
            assert points[0] == i

        agent.stop()


def test_sequential_agent_array(
    temporary_topics, kafka_bootstrap_servers, broker_authorization_config, tiled_profile
):
    from itertools import product

    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub_topic, sub_topic):
        agent = TestSequentialAgent(
            pub_topic,
            sub_topic,
            kafka_bootstrap_servers,
            broker_authorization_config,
            tiled_profile,
            sequence=product([1, 2, 3], [4, 5, 6]),
        )
        agent.start()

        _, points = agent.ask()
        assert len(points) == 1
        assert points[0][0] == 1 and points[0][1] == 4
        _, points = agent.ask(2)
        assert len(points) == 2
        assert points[0][0] == 1
        assert points[1][1] == 6
