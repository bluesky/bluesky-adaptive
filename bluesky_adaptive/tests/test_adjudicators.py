import time as ttime
from typing import Sequence, Tuple, Union

from bluesky_kafka import BlueskyConsumer, Publisher
from bluesky_queueserver_api.http import REManagerAPI
from databroker.client import BlueskyRun
from event_model import compose_run
from numpy.typing import ArrayLike

from bluesky_adaptive.adjudicators.base import AgentByNameAdjudicator, NonredundantAdjudicator
from bluesky_adaptive.adjudicators.msg import Suggestion
from bluesky_adaptive.agents.base import Agent, AgentConsumer

KAFKA_TIMEOUT = 30.0  # seconds


class NoTiled:
    class V1:
        def insert(self, *args, **kwargs):
            pass

    v1 = V1


class TestAgent(Agent):
    measurement_plan_name = "agent_driven_nap"

    def __init__(self, pub_topic, sub_topic, kafka_bootstrap_servers, broker_authorization_config, qs, **kwargs):
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
        super().__init__(
            kafka_consumer=kafka_consumer,
            kafka_producer=kafka_producer,
            tiled_agent_node=None,
            tiled_data_node=None,
            qserver=qs,
            **kwargs,
        )
        self.count = 0
        self.agent_catalog = NoTiled()

    def no_tiled(*args, **kwargs):
        pass

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


class AccumulateAdjudicator(AgentByNameAdjudicator):
    def __init__(self, *args, qservers, **kwargs):
        super().__init__(*args, qservers=qservers, **kwargs)
        self.consumed_documents = []

    def process_document(self, topic, name, doc):
        self.consumed_documents.append((name, doc))
        return super().process_document(topic, name, doc)

    def until_len(self):
        if len(self.consumed_documents) >= 1:
            return False
        else:
            return True


def test_accumulate(temporary_topics, kafka_bootstrap_servers, broker_authorization_config):
    # Smoke test for the kafka comms and acumulation with `continue_polling` function
    with temporary_topics(topics=["test.adjudicator"]) as (topic,):
        publisher = Publisher(
            topic=topic,
            bootstrap_servers=kafka_bootstrap_servers,
            producer_config=broker_authorization_config,
            key=f"{topic}.key",
        )
        re_manager = REManagerAPI(http_server_uri=None)
        re_manager.set_authorization_key(api_key="SECRET")
        adjudicator = AccumulateAdjudicator(
            topics=[topic],
            bootstrap_servers=kafka_bootstrap_servers,
            group_id="test.communication.group",
            qservers={"tst": re_manager},
            consumer_config={"auto.offset.reset": "earliest"},
        )
        adjudicator.start(continue_polling=adjudicator.until_len)
        publisher("name", {"dfi": "Dfs"})
        publisher("name", {"dfi": "Dfs"})
        start_time = ttime.monotonic()
        while adjudicator.until_len():
            ttime.sleep(0.5)
            if ttime.monotonic() - start_time > KAFKA_TIMEOUT:
                break
        assert len(adjudicator.consumed_documents) == 1


def test_send_to_adjudicator(temporary_topics, kafka_bootstrap_servers, broker_authorization_config):
    def consume_until_len(kafka_topic, length):
        consumed_documents = []
        start_time = ttime.monotonic()

        def process_document(consumer, topic, name, document):
            consumed_documents.append((name, document))

        consumer = BlueskyConsumer(
            topics=[kafka_topic],
            bootstrap_servers=kafka_bootstrap_servers,
            group_id=f"{kafka_topic}.consumer.group",
            consumer_config={"auto.offset.reset": "earliest"},
            process_document=process_document,
        )

        def until_len():
            if len(consumed_documents) >= length:
                return False
            elif ttime.monotonic() - start_time > KAFKA_TIMEOUT:
                raise TimeoutError("Kafka Timeout in test environment")
            else:
                return True

        consumer.start(continue_polling=until_len)
        return consumed_documents

    # Test the internal publisher
    with temporary_topics(topics=["test.adjudicator", "test.data"]) as (adj_topic, bs_topic):
        agent = TestAgent(adj_topic, bs_topic, kafka_bootstrap_servers, broker_authorization_config, None)
        agent.kafka_producer("test", {"some": "dict"})
        cache = consume_until_len(kafka_topic=adj_topic, length=1)
        assert len(cache) == 1

    # Test agent sending to adjudicator
    with temporary_topics(topics=["test.adjudicator", "test.data"]) as (adj_topic, bs_topic):
        agent = TestAgent(adj_topic, bs_topic, kafka_bootstrap_servers, broker_authorization_config, None)
        agent.start()
        agent.generate_suggestions_for_adjudicator(1)
        cache = consume_until_len(kafka_topic=adj_topic, length=1)
        assert len(cache) == 1


def test_adjudicator_receipt(temporary_topics, kafka_bootstrap_servers, broker_authorization_config):
    # Test agent sending to adjudicator
    with temporary_topics(topics=["test.adjudicator", "test.data"]) as (adj_topic, bs_topic):
        agent = TestAgent(adj_topic, bs_topic, kafka_bootstrap_servers, broker_authorization_config, None)
        agent.start()
        adjudicator = AccumulateAdjudicator(
            topics=[adj_topic],
            bootstrap_servers=kafka_bootstrap_servers,
            group_id="test.communication.group",
            qservers={"tst": None},
            consumer_config={"auto.offset.reset": "earliest"},
        )
        adjudicator.start(continue_polling=adjudicator.until_len)
        agent.generate_suggestions_for_adjudicator(1)
        start_time = ttime.monotonic()
        while adjudicator.until_len():
            ttime.sleep(0.5)
            if ttime.monotonic() - start_time > KAFKA_TIMEOUT:
                raise TimeoutError("Adjudicator did not accumulate suggestions")
        assert len(adjudicator.consumed_documents) == 1


def test_adjudicator_by_name(temporary_topics, kafka_bootstrap_servers, broker_authorization_config):
    with temporary_topics(topics=["test.adjudicator", "test.data"]) as (adj_topic, bs_topic):
        re_manager = REManagerAPI(http_server_uri=None)
        re_manager.set_authorization_key(api_key="SECRET")
        adjudicator = AccumulateAdjudicator(
            topics=[adj_topic],
            bootstrap_servers=kafka_bootstrap_servers,
            group_id="test.communication.group",
            qservers={"tst": re_manager},
            consumer_config={"auto.offset.reset": "earliest"},
        )
        adjudicator.primary_agent = "good"
        adjudicator.prompt_judgment = False
        adjudicator.start()

        good_agent = TestAgent(
            adj_topic,
            bs_topic,
            kafka_bootstrap_servers,
            broker_authorization_config,
            re_manager,
            endstation_key="tst",
        )
        good_agent.instance_name = "good"
        good_agent.start()
        evil_agent = TestAgent(
            adj_topic,
            bs_topic,
            kafka_bootstrap_servers,
            broker_authorization_config,
            re_manager,
            endstation_key="tst",
        )
        evil_agent.instance_name = "evil"
        evil_agent.start()

        re_manager = good_agent.re_manager
        status = re_manager.status()
        if not status["worker_environment_exists"]:
            re_manager.environment_open()
        re_manager.queue_clear()

        # Make sure we can put something on the queue from the adjudicator
        adjudicator._add_suggestion_to_queue(
            re_manager,
            "good",
            Suggestion(ask_uid="test", plan_name="agent_driven_nap", plan_args=[1.5], plan_kwargs={}),
        )
        assert re_manager.status()["items_in_queue"] == 1

        # Make sure suggestions are making it to adjudicator
        good_agent.generate_suggestions_for_adjudicator(1)
        start_time = ttime.monotonic()
        while not adjudicator.consumed_documents:
            ttime.sleep(0.1)
            if ttime.monotonic() - start_time > KAFKA_TIMEOUT:
                raise TimeoutError("Adjudicator did not accumulate suggestions")
        assert adjudicator.current_suggestions
        assert "good" in adjudicator.agent_names

        # Make sure adjudicator can throw the right suggestions onto the queue
        good_agent.generate_suggestions_for_adjudicator(1)
        evil_agent.generate_suggestions_for_adjudicator(1)
        start_time = ttime.monotonic()
        while len(adjudicator.current_suggestions) < 2:
            ttime.sleep(0.1)
            if ttime.monotonic() - start_time > KAFKA_TIMEOUT:
                raise TimeoutError("Adjudicator did not accumulate suggestions")
        judgments = adjudicator.make_judgments()
        assert adjudicator.primary_agent in adjudicator.current_suggestions.keys()
        assert len(judgments) == 1
        assert judgments[0].agent_name == "good"
        assert judgments[0].re_manager == re_manager


def test_nonredundant_adjudicator(temporary_topics, kafka_bootstrap_servers, broker_authorization_config):
    def _hash_suggestion(tla, suggestion: Suggestion):
        return f"{tla} {suggestion.plan_name} {str(suggestion.plan_args)}"

    with temporary_topics(topics=["test.adjudicator", "test.data"]) as (adj_topic, bs_topic):
        re_manager = REManagerAPI(http_server_uri=None)
        re_manager.set_authorization_key(api_key="SECRET")
        adjudicator = NonredundantAdjudicator(
            topics=[adj_topic],
            bootstrap_servers=kafka_bootstrap_servers,
            group_id="test.communication.group",
            qservers={"tst": re_manager},
            consumer_config={"auto.offset.reset": "earliest"},
            hash_suggestion=_hash_suggestion,
        )
        adjudicator.prompt_judgment = False
        adjudicator.start()
        agent = TestAgent(
            adj_topic,
            bs_topic,
            kafka_bootstrap_servers,
            broker_authorization_config,
            re_manager,
            endstation_key="tst",
        )
        agent.start()
        # Assure 5 suggestions that are the same only land as 1 judgement
        agent.generate_suggestions_for_adjudicator(5)
        start_time = ttime.monotonic()
        while not adjudicator.current_suggestions:
            ttime.sleep(0.1)
            if ttime.monotonic() - start_time > KAFKA_TIMEOUT:
                raise TimeoutError("Adjudicator did not accumulate suggestions")
        judgments = adjudicator.make_judgments()
        assert len(judgments) == 1

        # Assure that additional suggestions don't pass judgement
        agent.generate_suggestions_for_adjudicator(1)
        start_time = ttime.monotonic()
        while not adjudicator.current_suggestions:
            ttime.sleep(0.1)
            if ttime.monotonic() - start_time > KAFKA_TIMEOUT:
                raise TimeoutError("Adjudicator did not accumulate suggestions")
        judgments = adjudicator.make_judgments()
        assert len(judgments) == 0
