import time as ttime
from typing import Sequence, Tuple, Union

from bluesky_kafka import Publisher
from bluesky_queueserver_api.http import REManagerAPI
from databroker.client import BlueskyRun
from event_model import compose_run
from numpy.typing import ArrayLike
from tiled.client import from_profile

from bluesky_adaptive.agents.base import Agent, AgentConsumer, MonarchSubjectAgent
from bluesky_adaptive.agents.simple import SequentialAgentBase


class TestCommunicationAgent(Agent):
    measurement_plan_name = "agent_driven_nap"
    instance_name = "bob"

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
        self.count = 0

    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [1.5], dict()

    def unpack_run(self, run: BlueskyRun) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        return 0, 0

    def report(self, report_number: int = 0) -> dict:
        return dict(agent_name=self.instance_name, report=f"report_{report_number}")

    def ask(self, batch_size: int = 1) -> Tuple[dict, Sequence]:
        return ([dict(agent_name=self.instance_name, report=f"ask_{batch_size}")], [0 for _ in range(batch_size)])

    def tell(self, x, y) -> dict:
        self.count += 1
        return dict(x=x, y=y)

    def start(self):
        """Start without kafka consumer start"""
        self._compose_run_bundle = compose_run(metadata=self.metadata)
        self.agent_catalog.v1.insert("start", self._compose_run_bundle.start_doc)

    def server_registrations(self) -> None:
        return None


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
        docs, _ = agent.ask(1)
        ask_uid = agent._write_event("ask", docs[0])
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
        assert isinstance(ask_uid, str)


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

        compose_run_bundle = compose_run(metadata={})
        start = compose_run_bundle.start_doc
        agent.exp_catalog.v1.insert("start", start)
        stop = compose_run_bundle.compose_stop()
        agent.exp_catalog.v1.insert("stop", stop)

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
                    fake_publisher("start", start)
                    fake_publisher("stop", stop)
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
        self.count = 0

    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [1.5], dict()

    def unpack_run(self, run: BlueskyRun) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        return 0, 0

    def start(self):
        """Start without kafka consumer start"""
        self._compose_run_bundle = compose_run(metadata=self.metadata)
        self.agent_catalog.v1.insert("start", self._compose_run_bundle.start_doc)

    def server_registrations(self) -> None:
        return None


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


def test_close_and_restart(temporary_topics, kafka_bootstrap_servers, broker_authorization_config, tiled_profile):
    "Starts agent, restarts it, closes it. Tests for 2 bluesky runs with same agent name."

    class Agent(TestSequentialAgent):
        """Actually start kafka"""

        def start(self):
            return SequentialAgentBase.start(self)

    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub_topic, sub_topic):
        agent = Agent(
            pub_topic,
            sub_topic,
            kafka_bootstrap_servers,
            broker_authorization_config,
            tiled_profile,
            sequence=[1, 2, 3],
        )
        agent.start()
        ttime.sleep(1.0)
        agent.close_and_restart()
        agent.stop()
        node = from_profile(tiled_profile)
        assert node[-1].metadata["start"]["agent_name"] == node[-2].metadata["start"]["agent_name"]
        assert node[-1].metadata["start"]["uid"] != node[-2].metadata["start"]["uid"]


class TestMonarchSubject(MonarchSubjectAgent, SequentialAgentBase):
    """A bad monarch subject, where the monarch and subject queues are the same."""

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
            subject_qserver=qs,
            **kwargs,
        )

    def measurement_plan(self, point: ArrayLike):
        return "agent_driven_nap", [0.5], dict()

    def subject_measurement_plan(self, point: ArrayLike):
        return "agent_driven_nap", [0.7], dict()

    def subject_ask(self, batch_size: int):
        return [dict()], [0.0 for _ in range(batch_size)]

    def unpack_run(self, run: BlueskyRun):
        return 0, 0

    def server_registrations(self) -> None:
        return None


def test_monarch_subject(temporary_topics, kafka_bootstrap_servers, broker_authorization_config, tiled_profile):
    with temporary_topics(topics=["test.publisher", "test.subscriber"]) as (pub_topic, sub_topic):
        agent = TestMonarchSubject(
            pub_topic,
            sub_topic,
            kafka_bootstrap_servers,
            broker_authorization_config,
            tiled_profile,
            sequence=[1, 2, 3],
        )

        agent.start()
        while True:
            # Awaiting the agent build before artificial ask
            if agent._compose_run_bundle is not None:
                if agent._compose_run_bundle.start_doc["uid"] in agent.agent_catalog:
                    break
            else:
                continue

        # add an item to the queue by the channel of monarch and subject
        # Suggestions may land in history or queue depending on timing
        if not agent.re_manager.status()["worker_environment_exists"]:
            agent.re_manager.environment_open()
        agent.re_manager.queue_clear()
        agent.re_manager.history_clear()

        agent.add_suggestions_to_queue(1)
        while agent.re_manager.status()["manager_state"] == "executing_queue":
            continue
        assert agent.re_manager.status()["items_in_queue"] + agent.re_manager.status()["items_in_history"] == 1

        agent.add_suggestions_to_subject_queue(1)
        while agent.re_manager.status()["manager_state"] == "executing_queue":
            continue
        assert agent.re_manager.status()["items_in_queue"] + agent.re_manager.status()["items_in_history"] == 2

        agent.stop()
