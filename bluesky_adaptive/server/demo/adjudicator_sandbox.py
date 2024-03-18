# BS_AGENT_STARTUP_SCRIPT_PATH=./bluesky_adaptive/server/demo/adjudicator_sandbox.py \
# uvicorn bluesky_adaptive.server:app
from bluesky_kafka.utils import create_topics, delete_topics
from bluesky_queueserver_api.http import REManagerAPI

from bluesky_adaptive.adjudicators.base import NonredundantAdjudicator
from bluesky_adaptive.adjudicators.msg import Suggestion
from bluesky_adaptive.server import shutdown_decorator, startup_decorator

kafka_producer_config = {
    "acks": 1,
    "enable.idempotence": False,
    "request.timeout.ms": 1000,
    "bootstrap.servers": "127.0.0.1:9092",
}
tiled_profile = "testing_sandbox"
kafka_bootstrap_servers = "127.0.0.1:9092"
bootstrap_servers = kafka_bootstrap_servers
admin_client_config = kafka_producer_config
topics = ["test.publisher", "test.subscriber"]
adj_topic, sub_topic = topics


re_manager = REManagerAPI(http_server_uri=None)
re_manager.set_authorization_key(api_key="SECRET")


def _hash_suggestion(tla, suggestion: Suggestion):
    return f"{tla} {suggestion.plan_name} {str(suggestion.plan_args)}"


adjudicator = NonredundantAdjudicator(
    topics=[adj_topic],
    bootstrap_servers=kafka_bootstrap_servers,
    group_id="test.communication.group",
    qservers={"tst": re_manager},
    consumer_config={"auto.offset.reset": "earliest"},
    hash_suggestion=_hash_suggestion,
)


@startup_decorator
def startup_topics():
    delete_topics(
        bootstrap_servers=bootstrap_servers,
        topics_to_delete=topics,
        admin_client_config=admin_client_config,
    )
    create_topics(
        bootstrap_servers=bootstrap_servers,
        topics_to_create=topics,
        admin_client_config=admin_client_config,
    )


@startup_decorator
def startup_adjudicator():
    adjudicator.start()


@shutdown_decorator
def shutdown_agent():
    return adjudicator.stop()


@shutdown_decorator
def shutdown_topics():
    delete_topics(
        bootstrap_servers=bootstrap_servers,
        topics_to_delete=topics,
        admin_client_config=admin_client_config,
    )
