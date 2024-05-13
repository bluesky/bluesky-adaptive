import time
from collections import deque
from typing import Any, Optional

from bluesky_queueserver_api.api_threads import API_Threads_Mixin

from bluesky_adaptive.adjudicators.base import AdjudicatorBase
from bluesky_adaptive.agents.base import Agent, MonarchSubjectAgent


class KafkaSmoke:
    """Kafka class to stand in and do nothing for any subscriber or publisher calls"""

    def set_agent(self, *args, **kwargs):
        pass

    def subscribe(self, *args, **kwargs):
        pass

    def start(self, *args, **kwargs):
        pass

    def flush(self, *args, **kwargs):
        pass

    def stop(self, *args, **kwargs):
        pass

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        pass


class OfflineConsumer(KafkaSmoke):

    def __init__(self, topic: Optional[deque] = None, loop_on_start=False):
        """
        Offline consumer (RunRouter) that uses a queue rather than a topic.
        The queue can have only 1 subscriber, as items are popped.
        In practice this queue should be shared with an OfflineProducer.
        Can optionally be started in a thread to loop with sleeps (loop_on_start),
        or can be left to manually trigger callbacks only.

        Parameters
        ----------
        topic : Optional[deque], optional
            Proxy topic replaced by queue, by default deque()
        loop_on_start : bool, optional
            Option to have a subscription loop that blocks. Should be done in a thread, by default False
        """
        self.topic = deque() if topic is None else topic
        self.subscriptions = []
        self.loop_on_start = loop_on_start
        self._poisoned = False

    def subscribe(self, callback):
        """Updates subscriptions to include list of callables"""
        self.subscriptions.append(callback)

    def start(self, *args, **kwargs):
        if self.loop_on_start:
            while not self._poisoned:
                self.trigger()
                time.sleep(0.01)
        else:
            pass

    def stop(self, *args, **kwargs):
        self._poisoned = True

    def trigger(self):
        if len(self.topic) > 0:
            name, doc = self.topic.pop()
            for sub in self.subscriptions:
                sub(name, doc)


class OfflineBlueskyConsumer(OfflineConsumer):
    """Offline BlueskyConsumer class that does not have the expectations of a RunRouter.
    Primarily used for Adjudicator that does not work with strict document streams.
    """

    def process_document(self, topic, name, doc):
        return True

    def trigger(self):
        if len(self.topic) > 0:
            name, doc = self.topic.pop()
            self.process_document("offline_topic", name, doc)


class OfflineProducer(KafkaSmoke):
    def __init__(self, topic: Optional[deque] = None):
        """
        Offline produceer that uses a queue rather than a topic.

        Parameters
        ----------
        topic : Optional[deque], optional
            Proxy topic replaced by queue, by default deque()
        """
        self.topic = deque() if topic is None else topic

    def __call__(self, name: str, message: dict):
        """Call will append to queue (topic) a tuple of (name, message).
        For Adjudicators, this is the stream_name, message.
        For Bluesky docuemnts, this is the document name, document.
        """
        self.topic.append((name, message))


class REManagerSmoke(API_Threads_Mixin):
    """REManager Class to stand in and do nothing. Should have all methods called by Agent.re_manager"""

    def __init__(self) -> None:
        self.queue = deque()

    def status(self, *args, **kwargs):
        return {"items_in_queue": len(self.queue), "worker_environment_exists": False, "manger_state": "idle"}

    def queue_start(self, *args, **kwargs):
        pass

    def item_add(self, plan, **kwargs):
        self.queue.append(plan)

    def set_authorization_key(self, *args, **kwargs):
        pass


class TiledSmoke:
    class v1:
        def insert(self, *args, **kwargs):
            pass

    def __getitem__(self, *args, **kwargs):
        return None


class OfflineAgent(Agent):
    def __init__(
        self,
        *,
        qserver=REManagerSmoke(),
        kafka_producer=OfflineProducer(),
        tiled_data_node=TiledSmoke(),
        tiled_agent_node=TiledSmoke(),
        loop_consumer_on_start=False,
        **kwargs
    ):
        """Basic async agent for offline activities and testing.
        Each core communication attribute can be overriden, e.g. using Tiled but no Queue Server or Kafka.
        The agent.kafka_consumer can start a loop in a thread, or have subscriptions manually triggered.

        Parameters
        ----------
        qserver : Optional[bluesky_queueserver_api.api_threads.API_Threads_Mixin]
            Stand-in object to manage communication with Queue Server, by default REManagerSmoke()
        kafka_producer :  Optional[Publisher], optional
            Stand-in Kafka producer for adjudicators, by default OfflineProducer()
        tiled_data_node :  optional
            Stand-in tiled container, by default TiledSmoke()
        tiled_agent_node : optional
            Stand-in tiled container, by default TiledSmoke()
        loop_consumer_on_start : bool, optional
            Whether to have a running thread for the agent's stand-in kafka consumer, or limit to manual trigger.
            By default False
        """
        self.kafka_queue = deque()
        super().__init__(
            kafka_consumer=OfflineConsumer(self.kafka_queue, loop_on_start=loop_consumer_on_start),
            qserver=qserver,
            kafka_producer=kafka_producer,
            tiled_agent_node=tiled_agent_node,
            tiled_data_node=tiled_data_node,
            **kwargs,
        )


class OfflineMonarchSubject(MonarchSubjectAgent):
    """Basic async agent for offline activities and testing.
    Each core communication attribute can be overriden, e.g. using Tiled but no Queue Server or Kafka.
    The agent.kafka_consumer can start a loop in a thread, or have subscriptions manually triggered."""

    def __init__(
        self,
        *,
        qserver=REManagerSmoke(),
        subject_queueserver=REManagerSmoke(),
        kafka_producer=OfflineProducer(),
        tiled_data_node=TiledSmoke(),
        tiled_agent_node=TiledSmoke(),
        loop_consumer_on_start=False,
        **kwargs
    ):
        self.kafka_queue = deque()
        super().__init__(
            kafka_consumer=OfflineConsumer(self.kafka_queue, loop_on_start=loop_consumer_on_start),
            qserver=qserver,
            subject_qserver=subject_queueserver,
            kafka_producer=kafka_producer,
            tiled_agent_node=tiled_agent_node,
            tiled_data_node=tiled_data_node,
            **kwargs,
        )


class OfflineAdjudicator(AdjudicatorBase):
    """Example offline adjudicator. Others can be assembled in a similar fashion with OfflineConsumer."""

    def __init__(self, consumer=OfflineBlueskyConsumer(), *args, **kwargs):
        super().__init__(consumer=consumer, *args, **kwargs)
