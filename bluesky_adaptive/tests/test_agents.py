from typing import Callable, Literal, Sequence, Tuple, Union

from bluesky_kafka import Publisher
from event_model import compose_run
from numpy.typing import ArrayLike

from bluesky_adaptive.agents.simple import SequentialAgentBase
from bluesky_adaptive.utils.offline import OfflineAgent, OfflineMonarchSubject, OfflineProducer

from ..typing import BlueskyRunLike


class NapAndCountAgent(OfflineAgent):
    measurement_plan_name = "agent_driven_nap"
    instance_name = "bob"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.count = 0

    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [1.5], dict()

    def unpack_run(self, run: BlueskyRunLike) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        return 0, 0

    def report(self, report_number: int = 0) -> dict:
        return dict(agent_name=self.instance_name, report=f"report_{report_number}")

    def suggest(self, batch_size: int = 1) -> Tuple[dict, Sequence]:
        return (
            [dict(agent_name=self.instance_name, report=f"suggestion_{batch_size}")],
            [0 for _ in range(batch_size)],
        )

    def ingest(self, x, y) -> dict:
        self.count += 1
        return dict(x=x, y=y)

    def server_registrations(self) -> None:
        return None


def test_feedback_to_queue(tiled_profile: Literal["testing_sandbox"], publisher_factory: Callable[..., Publisher]):
    """
    Publish a stop document to kafka with the right start uid for a manufactured run.
    Ensure something lands on the queue.
    """

    agent = NapAndCountAgent()
    agent.start()
    agent.enable_continuous_suggesting()
    agent.enable_direct_to_queue()

    producer = OfflineProducer(topic=agent.kafka_queue)
    compose_run_bundle = compose_run(metadata={})
    start = compose_run_bundle.start_doc
    agent.exp_catalog.v1.insert("start", start)
    producer("start", start)
    stop = compose_run_bundle.compose_stop()
    agent.exp_catalog.v1.insert("stop", stop)
    producer("stop", stop)
    agent.kafka_consumer.trigger()

    assert agent.count > 0
    assert agent.re_manager.status()["items_in_queue"] > 0
    agent.stop()


class SequentialTestAgent(SequentialAgentBase, OfflineAgent):
    measurement_plan_name = "agent_driven_nap"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.count = 0

    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [1.5], dict()

    def unpack_run(self, run: BlueskyRunLike) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        return 0, 0

    def server_registrations(self) -> None:
        return None


def test_sequntial_agent():
    agent = SequentialTestAgent(sequence=[1, 2, 3])
    for i in [1, 2, 3]:
        _, points = agent.suggest()
        assert len(points) == 1
        assert points[0] == i


def test_sequential_agent_array():
    from itertools import product

    agent = SequentialTestAgent(sequence=product([1, 2, 3], [4, 5, 6]))
    _, points = agent.suggest()
    assert len(points) == 1
    assert points[0][0] == 1 and points[0][1] == 4
    _, points = agent.suggest(2)
    assert len(points) == 2
    assert points[0][0] == 1
    assert points[1][1] == 6


def test_close_and_restart(tiled_node):
    "Starts agent, restarts it, closes it. Tests for 2 bluesky runs with same agent name."
    agent = SequentialTestAgent(
        tiled_data_node=tiled_node,
        tiled_agent_node=tiled_node,
        sequence=[1, 2, 3],
    )
    agent.start()
    agent.close_and_restart()
    agent.stop()
    assert tiled_node[-1].metadata["start"]["agent_name"] == tiled_node[-2].metadata["start"]["agent_name"]
    assert tiled_node[-1].metadata["start"]["uid"] != tiled_node[-2].metadata["start"]["uid"]


class MonarchSubjectTestAgent(SequentialAgentBase, OfflineMonarchSubject):

    def measurement_plan(self, point: ArrayLike):
        return "agent_driven_nap", [0.5], dict()

    def subject_measurement_plan(self, point: ArrayLike):
        return "agent_driven_nap", [0.7], dict()

    def subject_suggest(self, batch_size: int):
        return [dict()], [0.0 for _ in range(batch_size)]

    def unpack_run(self, run: BlueskyRunLike):
        return 0, 0

    def server_registrations(self) -> None:
        return None


def test_monarch_subject():
    agent = MonarchSubjectTestAgent(sequence=[1, 2, 3])
    agent.start()
    agent.add_suggestions_to_queue(1)
    assert agent.re_manager.status()["items_in_queue"] == 1

    agent.add_suggestions_to_subject_queue(1)
    assert agent.subject_re_manager.status()["items_in_queue"] == 1
    agent.stop()
