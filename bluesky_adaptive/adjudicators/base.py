import logging
from abc import ABC, abstractmethod
from collections import deque
from copy import deepcopy
from threading import Lock, Thread
from typing import Callable, Sequence, Tuple

from bluesky_kafka import BlueskyConsumer
from bluesky_queueserver_api import BPlan
from bluesky_queueserver_api.api_threads import API_Threads_Mixin

from bluesky_adaptive.adjudicators.msg import DEFAULT_NAME, AdjudicatorMsg, Judgment, Suggestion
from bluesky_adaptive.agents.base import Agent as BaseAgent

logger = logging.getLogger(__name__)


class DequeSet:
    def __init__(self, maxlen=100):
        self._set = set()
        self._dequeue = deque()
        self._maxlen = maxlen

    def __contains__(self, d):
        return d in self._set

    def append(self, d):
        if d in self:
            logger.debug(f"Attempt to add redundant point to DequeSet ignored: {d}")
            return
        self._set.add(d)
        self._dequeue.append(d)
        while len(self._dequeue) >= self._maxlen:
            discarded = self._dequeue.popleft()
            self._set.remove(discarded)


class AdjudicatorBase(BlueskyConsumer, ABC):
    """
    An agent adjudicator that listens to published suggestions by agents.
    This Base approach (as per `process_document`) only retains the most recent suggestions by any named agents.
    Other mechanisms for tracking can be provided as in example sub-classes.

    Parameters
    ----------
    topics : list of str
        List of existing_topics as strings such as ["topic-1", "topic-2"]
    bootstrap_servers : str
        Comma-delimited list of Kafka server addresses as a string
        such as ``'broker1:9092,broker2:9092,127.0.0.1:9092'``
    group_id : str
        Required string identifier for the consumer's Kafka Consumer group.
    """

    _register_method = BaseAgent._register_method
    _register_property = BaseAgent._register_property

    def __init__(self, topics: list[str], bootstrap_servers: str, group_id: str, *args, **kwargs):
        super().__init__(topics, bootstrap_servers, group_id, *args, **kwargs)
        self._lock = Lock()
        self._thread = None
        self._current_suggestions = {}  # agent_name: AdjudicatorMsg
        self._ask_uids = DequeSet()
        self._prompt = True

        try:
            self.server_registrations()
        except RuntimeError as e:
            logger.warning(f"Agent server unable to make registrations. Continuing regardless of\n {e}")

    def start(self, *args, **kwargs):
        self._thread = Thread(
            target=BlueskyConsumer.start,
            name="adjudicator-loop",
            daemon=True,
            args=[self] + list(args),
            kwargs=kwargs,
        )
        self._thread.start()

    def process_document(self, topic, name, doc):
        if name != DEFAULT_NAME:
            return True
        with self._lock:
            logger.info(f"{doc['agent_name']=}, {doc['suggestions_uid']=}")
            self._current_suggestions[doc["agent_name"]] = AdjudicatorMsg(**doc)

        if self.prompt_judgment:
            self._make_judgments_and_add_to_queue()

    @property
    def current_suggestions(self):
        """Dictionary of {agent_name:AdjudicatorMsg}, deep copied at each grasp."""
        with self._lock:
            ret = deepcopy(self._current_suggestions)
        return ret

    @property
    def agent_names(self):
        with self._lock:
            ret = list(self._current_suggestions.keys())
        return ret

    @property
    def prompt_judgment(self) -> bool:
        return self._prompt

    @prompt_judgment.setter
    def prompt_judgment(self, flag: bool):
        self._prompt = flag

    def _add_suggestion_to_queue(self, re_manager: API_Threads_Mixin, agent_name: str, suggestion: Suggestion):
        if suggestion.ask_uid in self._ask_uids:
            logger.debug(f"Ask uid {suggestion.ask_uid} has already been seen. Not adding anything to the queue.")
            return
        else:
            self._ask_uids.append(suggestion.ask_uid)
        kwargs = suggestion.plan_kwargs
        kwargs.setdefault("md", {})
        kwargs["md"]["agent_ask_uid"] = suggestion.ask_uid
        kwargs["md"]["agent_name"] = agent_name
        plan = BPlan(suggestion.plan_name, *suggestion.plan_args, **kwargs)
        r = re_manager.item_add(plan, pos="back")
        logger.debug(f"Sent http-server request by adjudicator\n." f"Received reponse: {r}")

    def server_registrations(self) -> None:
        """
        Method to generate all server registrations during agent initialization.
        This method can be used in subclasses, to override or extend the default registrations.
        """
        self._register_method("make_judgements", "_make_judgments_and_add_to_queue")
        self._register_property("prompt_judgment")
        self._register_property("current_suggestions")

    def _make_judgments_and_add_to_queue(self):
        """Internal wrapper for making judgements, validating, and adding to queue."""
        judgments = self.make_judgments()
        for judgment in judgments:
            if not isinstance(judgment, Judgment):
                judgment = Judgment(*judgment)  # Validate
            self._add_suggestion_to_queue(judgment.re_manager, judgment.agent_name, judgment.suggestion)

    @abstractmethod
    def make_judgments(self) -> Sequence[Tuple[API_Threads_Mixin, str, Suggestion]]:
        """Instance method to make judgements based on current suggestions.
        The returned tuples will be deconstructed to add suggestions to the queue.
        """
        ...


class AgentByNameAdjudicator(AdjudicatorBase):
    """Adjudicator that only allows messages from a set primary agent, and uses a single qserver.
    Parameters
    ----------
    qservers :  dict[str, API_Threads_Mixin]
        Dictionary of objects to manage communication with Queue Server. These should be keyed by the beamline TLA
        expected in AdjudicatorMsg.suggestions dictionary.
    """

    def __init__(self, *args, qservers: dict[str, API_Threads_Mixin], **kwargs):
        self._primary_agent = ""
        self._re_managers = qservers
        super().__init__(*args, **kwargs)

    @property
    def primary_agent(self):
        return self._primary_agent

    @primary_agent.setter
    def primary_agent(self, name: str):
        self._primary_agent = name

    def server_registrations(self) -> None:
        self._register_property("priamry_agent")
        super().server_registrations()

    def make_judgments(self) -> Sequence[Tuple[API_Threads_Mixin, str, Suggestion]]:
        judgments = []

        if self.primary_agent not in self.agent_names:
            logger.debug(f"Agent {self.primary_agent} not known to the Adjudicator")
        else:
            adjudicator_msg = self.current_suggestions[self.primary_agent]
            for key, manager in self._re_managers.items():
                suggestions = adjudicator_msg.suggestions.get(key, [])
                for suggestion in suggestions:
                    judgments.append(
                        Judgment(re_manager=manager, agent_name=self.primary_agent, suggestion=suggestion)
                    )
        return judgments


class NonredundantAdjudicator(AdjudicatorBase):
    """Use a hashing function to convert any suggestion into a unique hash.

    Parameters
    ----------
    topics : list of str
        List of existing_topics as strings such as ["topic-1", "topic-2"]
    bootstrap_servers : str
        Comma-delimited list of Kafka server addresses as a string
        such as ``'broker1:9092,broker2:9092,127.0.0.1:9092'``
    group_id : str
        Required string identifier for the consumer's Kafka Consumer group.
    qservers :  dict[str, API_Threads_Mixin]
        Dictionary of objects to manage communication with Queue Server. These should be keyed by the beamline TLA
        expected in AdjudicatorMsg.suggestions dictionary.
    hash_suggestion : Callable
        Function that takes the tla and Suggestion object, and returns a hashable object as ::

            def hash_suggestion(tla: str, suggestion: Suggestion) -> Hashable: ...


        This hashable object will be used to check redundancy in a set.

    Examples
    --------
    >>> def hash_suggestion(tla: str, suggestion: Suggestion):
    >>>     # Uses only the tla, plan name, and args to define redundancy, avoiding any details in kwargs
    >>>     return f"{tla} {suggestion.plan_name} {str(suggestion.plan_args)}"
    """

    def __init__(
        self,
        topics: list[str],
        bootstrap_servers: str,
        group_id: str,
        *args,
        qservers: dict[str, API_Threads_Mixin],
        hash_suggestion: Callable,
        **kwargs,
    ):
        super().__init__(topics, bootstrap_servers, group_id, *args, **kwargs)
        self.hash_suggestion = hash_suggestion
        self.suggestion_set = set()
        self._re_managers = qservers

    def make_judgments(self) -> Sequence[Tuple[API_Threads_Mixin, str, Suggestion]]:
        """Loop over all recieved adjudicator mesages, and their suggested plans by beamline,
        seeking redundancy."""
        passing_judgements = []
        for agent_name, adjudicator_msg in self.current_suggestions.items():
            for tla, suggestions in adjudicator_msg.suggestions.items():
                for suggestion in suggestions:
                    hashable = self.hash_suggestion(tla, suggestion)
                    if hashable in self.suggestion_set:
                        continue
                    else:
                        passing_judgements.append(Judgment(self._re_managers[tla], agent_name, suggestion))
                        self.suggestion_set.add(hashable)
        return passing_judgements
