# flake8: noqa
import threading
from typing import Callable, Literal, Tuple, Union

from bluesky_kafka import Publisher
from bluesky_kafka.utils import create_topics, delete_topics
from bluesky_queueserver_api.http import REManagerAPI
from databroker.client import BlueskyRun
from numpy.typing import ArrayLike
from tiled.client import from_profile

from bluesky_adaptive.agents.base import AgentConsumer
from bluesky_adaptive.agents.simple import SequentialAgentBase
from bluesky_adaptive.server import register_variable, shutdown_decorator, start_task, startup_decorator


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

        # Use a string operating mode to show how we might swap more complex things like kernels, hyperparameters
        # The get/set are in terms of string, but the important object is a float (or something more complex).
        self._operating_mode = "sleepy"
        self._sleep_duration = 1.5

        # Regular attribute
        self.test_attr = 123

        self.registrations()

    def _register_property(self, name, property_name=None, **kwargs):
        [kwargs.pop(key, None) for key in ("getter", "setter")]  # Cannot pass getter/setter
        property_name = name if property_name is None else property_name
        register_variable(
            name,
            getter=lambda: getattr(self.__class__, property_name).fget(self),
            setter=lambda x: getattr(self.__class__, property_name).fset(self, x),
            **kwargs,
        )

    def _register_method(self, name, method_name=None, **kwargs):
        [kwargs.pop(key, None) for key in ("getter", "setter")]  # Cannot pass getter/setter
        method_name = name if method_name is None else method_name
        if not isinstance(getattr(self, method_name), Callable):
            raise TypeError(f"Method {method_name} must be a callable function.")
        register_variable(name, setter=lambda value: start_task(getattr(self, method_name)(*value[0], **value[1])))

    def registrations(self):
        self._register_method("generate_report")
        self._register_method("add_suggestions_to_queue_inner", "add_suggestions_to_queue")
        self._register_property("queue_add_position")

    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [self._sleep_duration], dict()

    def unpack_run(self, run: BlueskyRun) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        return 0, 0

    def operating_mode_setter(self, mode: Literal["sleepy", "awake"]):
        def set_function(mode):
            self._operating_mode = mode
            self._sleep_duration = {"sleepy": 1.5, "awake": 0.1}[mode]

        task_info = start_task(set_function, mode, run_in_background=False)
        print(f"task_info = {task_info}")
        return mode

    def operating_mode_getter(self):
        return self._operating_mode

    def report(self, **kwargs) -> dict:
        return {"test": "report"}


# Block of borrowed code from tests ###############################################################
broker_authorization_config = {
    "acks": 1,
    "enable.idempotence": False,
    "request.timeout.ms": 1000,
    "bootstrap.servers": "127.0.0.1:9092",
}
tiled_profile = "testing_sandbox"
kafka_bootstrap_servers = "127.0.0.1:9092"
bootstrap_servers = kafka_bootstrap_servers
admin_client_config = broker_authorization_config
topics = ["test.publisher", "test.subscriber"]
pub_topic, sub_topic = topics
# Block of borrowed code from tests ###############################################################

agent_thread = None
agent = TestSequentialAgent(
    pub_topic,
    sub_topic,
    kafka_bootstrap_servers,
    broker_authorization_config,
    tiled_profile,
    sequence=[1, 2, 3],
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
def startup_agent():
    agent.start()


@shutdown_decorator
def shutdown_agent():
    return agent.stop()


@shutdown_decorator
def shutdown_topics():
    delete_topics(
        bootstrap_servers=bootstrap_servers,
        topics_to_delete=topics,
        admin_client_config=admin_client_config,
    )


register_variable("test_attr", agent, "test_attr")
register_variable(
    "operating_mode",
    None,
    None,
    getter=agent.operating_mode_getter,
    setter=agent.operating_mode_setter,
    pv_type="str",
)


def add_suggestions_to_queue(batch_size):
    start_task(agent.add_suggestions_to_queue, batch_size)


def generate_report(args_kwargs):
    """Cheap setter wrapper for generate report.
    All setters must take a single value, so this takes args and kwargs as a tuple to unpack.

    Parameters
    ----------
    args_kwargs : Tuple[List, dict]
        Tuple of args and kwargs passed to the API `POST` as a `value`
    """
    _, kwargs = args_kwargs
    start_task(agent.generate_report, **kwargs)


register_variable("add_suggestions_to_queue", setter=add_suggestions_to_queue)
register_variable("generate_report", setter=generate_report)
