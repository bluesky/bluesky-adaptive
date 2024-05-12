import time
from collections import deque
from typing import Any, Optional


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
        Offline consumer that uses a queue rather than a topic.
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
