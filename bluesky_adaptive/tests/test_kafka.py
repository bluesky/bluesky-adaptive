from bluesky_kafka import BlueskyConsumer, Publisher, RemoteDispatcher

from bluesky_adaptive.agents.base import AgentConsumer


def test_pubsub_smoke(temporary_topics, publisher_factory, consume_documents_from_kafka_until_first_stop_document):

    with temporary_topics(topics=["test.publisher.and.subscriber"]) as (topic,):
        bluesky_publisher = publisher_factory(
            topic=topic,
            key=f"{topic}.key",
            flush_on_stop_doc=True,
        )
        bluesky_publisher("start", {"uid": "123"})
        bluesky_publisher("stop", {"start": "123"})
        consumed_bluesky_documents = consume_documents_from_kafka_until_first_stop_document(kafka_topic=topic)
        assert len(consumed_bluesky_documents) == 2


def test_pubsub_smoke2(
    temporary_topics,
    broker_authorization_config,
    kafka_bootstrap_servers,
    consume_documents_from_kafka_until_first_stop_document,
):

    with temporary_topics(topics=["test.publisher.and.subscriber"]) as (topic,):
        publisher = Publisher(
            topic=topic,
            bootstrap_servers=kafka_bootstrap_servers,
            producer_config=broker_authorization_config,
            key=f"{topic}.key",
        )
        publisher("start", {"uid": "123"})
        publisher("stop", {"start": "123"})
        consumed_bluesky_documents = consume_documents_from_kafka_until_first_stop_document(kafka_topic=topic)
        assert len(consumed_bluesky_documents) == 2


def test_pubsub_smoke3(
    temporary_topics,
    broker_authorization_config,
    kafka_bootstrap_servers,
):
    """Testing locally scoped consumers"""

    def consume_until_len(kafka_topic, length):
        consumed_documents = []

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
            else:
                return True

        consumer.start(continue_polling=until_len)
        return consumed_documents

    with temporary_topics(topics=["test.publisher.and.subscriber"]) as (topic,):
        publisher = Publisher(
            topic=topic,
            bootstrap_servers=kafka_bootstrap_servers,
            producer_config=broker_authorization_config,
            key=f"{topic}.key",
        )
        publisher("start", {"uid": "123"})
        publisher("stop", {"start": "123"})
        cache = consume_until_len(kafka_topic=topic, length=1)
        assert len(cache) == 1


def test_dispatcher(kafka_bootstrap_servers, broker_authorization_config, temporary_topics):
    """Test agent collection of documents and increment by command."""

    def fixed_consumer(topics):
        """Collects the first 10 documents and returns the agent"""
        consumed_documents = []

        def process_document(name, document):
            consumed_documents.append((name, document))

        dispatcher = RemoteDispatcher(
            topics,
            bootstrap_servers=kafka_bootstrap_servers,
            group_id="dummy.agent.group",
            consumer_config={"auto.offset.reset": "earliest"},
        )
        dispatcher.subscribe(process_document)

        def until_len():
            if len(consumed_documents) >= 2:
                return False
            else:
                return True

        dispatcher.start(continue_polling=until_len)
        return consumed_documents

    with temporary_topics(topics=["test.publisher.and.subscriber"]) as (topic,):
        publisher = Publisher(
            topic=topic,
            bootstrap_servers=kafka_bootstrap_servers,
            producer_config=broker_authorization_config,
            key=f"{topic}.key",
        )
        publisher("start", {"uid": "123"})
        publisher("stop", {"start": "123"})
        docs = fixed_consumer([topic])
        assert len(docs) == 2


def test_agent_consumer(kafka_bootstrap_servers, broker_authorization_config, temporary_topics):
    """Test agent collection of documents and increment by command."""

    class Foo:
        agent_name = ""

    def fixed_consumer(topics):
        """Collects the first 10 documents and returns the agent"""
        consumed_documents = []

        def process_document(name, document):
            consumed_documents.append((name, document))

        consumer = AgentConsumer(
            topics=topics,
            bootstrap_servers=kafka_bootstrap_servers,
            group_id="dummy.agent.group",
            consumer_config={"auto.offset.reset": "earliest"},
            agent=Foo(),
        )
        consumer.subscribe(process_document)

        def until_len():
            if len(consumed_documents) >= 2:
                return False
            else:
                return True

        consumer.start(continue_polling=until_len)
        return consumed_documents

    with temporary_topics(topics=["test.publisher.and.subscriber"]) as (topic,):
        publisher = Publisher(
            topic=topic,
            bootstrap_servers=kafka_bootstrap_servers,
            producer_config=broker_authorization_config,
            key=f"{topic}.key",
        )
        publisher("start", {"uid": "123"})
        publisher("stop", {"start": "123"})
        consumed_docs = fixed_consumer(topics=[topic])

        assert len(consumed_docs) == 2


def test_agent_interaction(kafka_bootstrap_servers, broker_authorization_config, temporary_topics):
    class DummyAgent:
        agent_name = "dummy_agent"

        def __init__(self, topics):
            self.counter = 0
            self.consumer = AgentConsumer(
                topics=topics,
                bootstrap_servers=kafka_bootstrap_servers,
                group_id="dummy.agent.group",
                agent=self,
                consumer_config={"auto.offset.reset": "earliest"},
            )
            self.cache = []

        def increase(self):
            self.counter += 1

        def tell(self, name, doc):
            self.cache.append((name, doc))

    def fixed_consumer(topics):
        """Collects the first 10 documents and returns the agent"""
        consumed_documents = []

        def process_document(name, document):
            consumed_documents.append((name, document))

        agent = DummyAgent(topics)
        agent.consumer.subscribe(process_document)
        agent.consumer.subscribe(agent.tell)

        def until_len():
            if len(consumed_documents) >= 2:
                return False
            else:
                return True

        agent.consumer.start(continue_polling=until_len)
        return consumed_documents, agent

    with temporary_topics(topics=["test.publisher.and.subscriber"]) as (topic,):
        publisher = Publisher(
            topic=topic,
            bootstrap_servers=kafka_bootstrap_servers,
            producer_config=broker_authorization_config,
            key=f"{topic}.key",
        )

        publisher("dummy_agent", dict(action="increase", args=[], kwargs={}))
        publisher("start", {"uid": "123"})
        publisher("stop", {"start": "123"})

        consumed_docs, agent = fixed_consumer(topics=[topic])

        assert agent.counter == 1
        # Only bluesky documents get consumed by the callbacks
        assert len(consumed_docs) == 2
        assert len(agent.cache) == 2
