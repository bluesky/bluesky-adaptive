from bluesky_adaptive.adjudicators.base import AgentByNameAdjudicator, NonredundantAdjudicator
from bluesky_adaptive.adjudicators.msg import Suggestion
from bluesky_adaptive.utils.offline import OfflineBlueskyConsumer, OfflineConsumer, OfflineProducer, REManagerSmoke

from .test_agents import NapAndCountAgent

KAFKA_TIMEOUT = 30.0  # seconds


class NoTiled:
    class V1:
        def insert(self, *args, **kwargs):
            pass

    v1 = V1


class AccumulateAdjudicator(AgentByNameAdjudicator):
    def __init__(self, consumer=OfflineConsumer(), *args, qservers, **kwargs):
        super().__init__(consumer, *args, qservers=qservers, **kwargs)
        self.consumed_documents = []

    def process_document(self, topic, name, doc):
        self.consumed_documents.append((name, doc))
        return super().process_document(topic, name, doc)

    def until_len(self):
        if len(self.consumed_documents) >= 1:
            return False
        else:
            return True


def test_accumulate():
    # Smoke test for the kafka comms and acumulation with `continue_polling` function

    re_manager = REManagerSmoke()
    consumer = OfflineBlueskyConsumer()
    adjudicator = AccumulateAdjudicator(
        consumer=consumer,
        qservers={"tst": re_manager},
    )
    producer = OfflineProducer(topic=consumer.topic)
    adjudicator.start(continue_polling=adjudicator.until_len)
    producer("name", {"dfi": "Dfs"})
    consumer.trigger()
    producer("name", {"dfi": "Dfs"})
    consumer.trigger()
    assert len(adjudicator.consumed_documents) == 2


def test_send_to_adjudicator():
    re_manager = REManagerSmoke()
    consumer = OfflineBlueskyConsumer()
    adjudicator = AccumulateAdjudicator(
        consumer=consumer,
        qservers={"tst": re_manager},
    )
    producer = OfflineProducer(topic=consumer.topic)
    agent = NapAndCountAgent(direct_to_queue=False, kafka_producer=producer)

    # Test the internal publisher
    agent.kafka_producer("test", {"some": "dict"})
    consumer.trigger()
    assert len(adjudicator.consumed_documents) == 1

    # Test the agent method
    agent.start()
    agent.generate_suggestions_for_adjudicator(1)
    consumer.trigger()
    assert len(adjudicator.consumed_documents) == 2


def test_adjudicator_by_name():
    re_manager = REManagerSmoke()
    consumer = OfflineBlueskyConsumer()

    adjudicator = AgentByNameAdjudicator(consumer=consumer, qservers={"tst": re_manager})
    adjudicator.primary_agent = "good"
    adjudicator.prompt_judgment = False
    adjudicator.start()

    producer = OfflineProducer(topic=consumer.topic)
    good_agent = NapAndCountAgent(
        direct_to_queue=False, kafka_producer=producer, endstation_key="tst", qserver=re_manager
    )
    good_agent.instance_name = "good"
    good_agent.start()
    evil_agent = NapAndCountAgent(
        direct_to_queue=False, kafka_producer=producer, endstation_key="tst", qserver=re_manager
    )
    evil_agent.instance_name = "evil"
    evil_agent.start()

    # Make sure we can put something on the queue from the adjudicator
    adjudicator._add_suggestion_to_queue(
        re_manager,
        "good",
        Suggestion(suggestion_uid="test", plan_name="agent_driven_nap", plan_args=[1.5], plan_kwargs={}),
    )
    assert re_manager.status()["items_in_queue"] == 1

    # Make sure suggestions are making it to adjudicator
    good_agent.generate_suggestions_for_adjudicator(1)
    consumer.trigger()
    assert adjudicator.current_suggestions
    assert "good" in adjudicator.agent_names

    # Make sure adjudicator can throw the right suggestions onto the queue
    good_agent.generate_suggestions_for_adjudicator(1)
    consumer.trigger()
    evil_agent.generate_suggestions_for_adjudicator(1)
    consumer.trigger()
    judgments = adjudicator.make_judgments()
    assert adjudicator.primary_agent in adjudicator.current_suggestions.keys()
    assert len(judgments) == 1
    assert judgments[0].agent_name == "good"
    assert judgments[0].re_manager == re_manager


def test_nonredundant_adjudicator(temporary_topics, kafka_bootstrap_servers, kafka_producer_config):
    def _hash_suggestion(tla, suggestion: Suggestion):
        return f"{tla} {suggestion.plan_name} {str(suggestion.plan_args)}"

    re_manager = REManagerSmoke()
    consumer = OfflineBlueskyConsumer()
    adjudicator = NonredundantAdjudicator(
        consumer=consumer,
        qservers={"tst": re_manager},
        hash_suggestion=_hash_suggestion,
    )
    adjudicator.prompt_judgment = False
    adjudicator.start()

    producer = OfflineProducer(topic=consumer.topic)
    agent = NapAndCountAgent(
        direct_to_queue=False, kafka_producer=producer, endstation_key="tst", qserver=re_manager
    )
    agent.start()

    # Assure 5 suggestions that are the same only land as 1 judgement
    agent.generate_suggestions_for_adjudicator(5)
    for _ in range(5):
        consumer.trigger()

    judgments = adjudicator.make_judgments()
    assert len(judgments) == 1

    # Assure that additional suggestions don't pass judgement
    agent.generate_suggestions_for_adjudicator(1)
    consumer.trigger()
    judgments = adjudicator.make_judgments()
    assert len(judgments) == 0
