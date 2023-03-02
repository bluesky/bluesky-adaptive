import inspect
import sys
from abc import ABC, abstractmethod
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from logging import getLogger
from typing import Dict, Iterable, List, Literal, Optional, Sequence, Tuple, Union

import msgpack
import tiled
from bluesky_kafka import Publisher, RemoteDispatcher
from bluesky_live.run_builder import RunBuilder
from bluesky_queueserver_api import BPlan
from bluesky_queueserver_api.api_threads import API_Threads_Mixin
from databroker.client import BlueskyRun
from numpy.typing import ArrayLike
from xkcdpass import xkcd_password as xp

logger = getLogger("bluesky_adaptive.agents")
PASSWORD_LIST = xp.generate_wordlist(wordfile=xp.locate_wordfile(), min_length=3, max_length=6)


class AgentConsumer(RemoteDispatcher):
    def __init__(
        self,
        *,
        topics,
        bootstrap_servers,
        group_id,
        agent=None,
        consumer_config=None,
        polling_duration=0.05,
        deserializer=msgpack.loads,
    ):
        """Dispatch documents from Kafka to bluesky callbacks, modified for agent usage.
        This allows subscribing the dispatcher to an on-stop protocol for agents to be told about
        new Bluesky runs. It also provides an interface to trigger changes to the agent using the same
        Kafka topics.

        Parameters
        ----------
        topics : list
            List of topics as strings such as ["topic-1", "topic-2"]
        bootstrap_servers : str
            Comma-delimited list of Kafka server addresses as a string such as ``'127.0.0.1:9092'``
        group_id : str
            Required string identifier for Kafka Consumer group
        agent : Agent
            Instance of the agent to send directives to. Must be set to send directives.
        consumer_config : dict
            Override default configuration or specify additional configuration
            options to confluent_kafka.Consumer.
        polling_duration : float
            Time in seconds to wait for a message before running function work_while_waiting.
            Default is 0.05.
        deserializer : function, optional
            optional function to deserialize data. Default is msgpack.loads.
        """
        super().__init__(topics, bootstrap_servers, group_id, consumer_config, polling_duration, deserializer)
        self._agent = agent

    def _agent_action(self, topic, doc):
        """Exposes agent methods via the kafka topic.
        This allows bluesky plans, or adjudicators to interface with agent hyperparameters or settings.

        Parameters
        ----------
        topic : str
            the Kafka topic of the message containing name and doc
        doc : dict
            agent document expecting
            {
            'action': 'method_name',
            'args': [arg1,arg2,...],
            'kwargs': {kwarg1:val1, kwarg2:val2}
            }

        Returns
        -------
        continue_polling : bool
        """
        action = doc["action"]
        args = doc["args"]
        kwargs = doc["kwargs"]
        try:
            getattr(self._agent, action)(*args, **kwargs)
        except AttributeError as e:
            logger.error(
                f"Unavailable action sent to agent {self._agent.instance_name} on topic: {topic}\n" f"{e}"
            )
        except TypeError as e:
            logger.error(
                f"Type error for {action} sent to agent {self._agent.instance_name} on topic: {topic}\n"
                f"Are you sure your args and kwargs were appropriate?\n"
                f"Args received: {args}\n"
                f"Kwargs received: {kwargs}\n"
                f"Expected signature: {inspect.signature(getattr(self.agent, action))}\n"
                f"{e}"
            )
        return True

    def process_document(self, consumer, topic, name, doc):
        """
        Processes bluesky documents.
        Optionally
        Sends bluesky document to RemoteDispatcher.process(name, doc)
        If this method returns False the BlueskyConsumer will break out of the
        polling loop.

        Parameters
        ----------
        topic : str
            the Kafka topic of the message containing name and doc
        name : str
            bluesky document name: `start`, `descriptor`, `event`, etc.
        doc : dict
            bluesky document

        Returns
        -------
        continue_polling : bool
            return False to break out of the polling loop, return True to continue polling
        """
        if name == self._agent.instance_name:
            return self._agent_action(topic, doc)
        else:
            return super().process_document(consumer, topic, name, doc)

    def set_agent(self, agent):
        self._agent = agent


class Agent(ABC):
    """Abstract base class for a single plan agent. These agents should consume data, decide where to measure next,
    and execute a single type of plan (something akin to move and count).
    Alternatively, these agents can be used for soley reporting.

    Base agent sets up a kafka subscription to listen to new stop documents, a catalog to read for experiments,
    a catalog to write agent status to, a kafka publisher to write agent documents to,
    and a manager API for the queue-server. Each time a stop document is read,
    the respective BlueskyRun is unpacked by the ``unpack_run`` method into an independent and dependent variable,
    and told to the agent by the ``tell`` method.

    Children of Agent should implment the following, through direct inheritence or mixin classes:
    Experiment specific:
    - measurement_plan
    - unpack_run
    Agent specific:
    - tell
    - ask
    - report (optional)
    - name (optional)

    Parameters
    ----------
    kafka_consumer : AgentConsumer
        Consumer (subscriber) of Kafka Bluesky documents. It should be subcribed to the sources of
        Bluesky stop documents that will trigger ``tell``.
        AgentConsumer is a child class of bluesky_kafka.RemoteDispatcher that enables
        kafka messages to trigger agent directives.
    kafka_producer : Publisher
        Bluesky Kafka publisher to produce document stream of agent actions.
    tiled_data_node : tiled.client.node.Node
        Tiled node to serve as source of data (BlueskyRuns) for the agent.
    tiled_agent_node : tiled.client.node.Node
        Tiled node to serve as storage for the agent documents.
    qserver : bluesky_queueserver_api.api_threads.API_Threads_Mixin
        Object to manage communication with Queue Server
    agent_run_suffix : Optional[str], optional
        Agent name suffix for the instance, by default generated using 2 hyphen separated words from xkcdpass.
    metadata : Optional[dict], optional
        Optional extra metadata to add to agent start document, by default {}
    ask_on_tell : bool, optional
        Whether to ask for new points every time an agent is told about new data.
        To create a truly passive agent, it is best to implement an ``ask`` as a method that does nothing.
        To create an agent that only suggests new points periodically or on another trigger, ``ask_on_tell``
        should be set to False.
        By default True
        Can be adjusted using ``enable_continuous_suggesting`` and ``disable_continuous_suggesting``.
    direct_to_queue : Optional[bool], optional
        Whether the agent suggestions will be placed directly on the queue. If false,
        the suggestions will be sent to a Kafka topic for an Adjudicator to process.
        By default True
        Can be adjusted using ``enable_direct_to_queue`` and ``disable_direct_to_queue``.
    report_on_tell : bool, optional
        Whether to create a report every time an agent is told about new data.
        By default False.
        Can be adjusted using ``enable_continuous_reporting`` and ``disable_continuous_reporting``.
    default_report_kwargs : Optional[dict], optional
        Default kwargs for calling the ``report`` method, by default None
    queue_add_position : Optional[Union[int, Literal[&quot;front&quot;, &quot;back&quot;]]], optional
        Starting postion to add to the queue if adding directly to the queue, by default "back".
    """

    def __init__(
        self,
        *,
        kafka_consumer: AgentConsumer,
        kafka_producer: Publisher,
        tiled_data_node: tiled.client.node.Node,
        tiled_agent_node: tiled.client.node.Node,
        qserver: API_Threads_Mixin,
        agent_run_suffix: Optional[str] = None,
        metadata: Optional[dict] = None,
        ask_on_tell: Optional[bool] = True,
        direct_to_queue: Optional[bool] = True,
        report_on_tell: Optional[bool] = False,
        default_report_kwargs: Optional[dict] = None,
        queue_add_position: Optional[Union[int, Literal["front", "back"]]] = None,
    ):

        logger.debug("Initializing agent.")
        self.kafka_consumer = kafka_consumer
        self.kafka_consumer.set_agent(self)
        self.kafka_consumer.subscribe(self._on_stop_router)

        self.kafka_producer = kafka_producer
        logger.debug("Kafka set up successfully.")

        self.exp_catalog = tiled_data_node
        logger.info(f"Reading data from catalog: {self.exp_catalog}")

        self.agent_catalog = tiled_agent_node
        logger.info(f"Writing data to catalog: {self.agent_catalog}")

        self.metadata = metadata or {}
        self.instance_name = (
            f"{self.name}-{agent_run_suffix}"
            if agent_run_suffix
            else f"{self.name}-{xp.generate_xkcdpassword(PASSWORD_LIST, numwords=2, delimiter='-')}"
        )
        self.metadata["agent_name"] = self.instance_name

        self._ask_on_tell = ask_on_tell
        self._report_on_tell = report_on_tell
        self.default_report_kwargs = {} if default_report_kwargs is None else default_report_kwargs

        self.builder = None
        self.re_manager = qserver
        self._queue_add_position = "back" if queue_add_position is None else queue_add_position
        self._direct_to_queue = direct_to_queue
        self.default_plan_md = dict(agent_name=self.instance_name, agent_class=str(type(self)))

    @abstractmethod
    def measurement_plan(self, point: ArrayLike) -> Tuple[str, List, dict]:
        """Fetch the string name of a registered plan, as well as the positional and keyword
        arguments to pass that plan.

        Args/Kwargs is a common place to transform relative into absolute motor coords, or
        other device specific parameters.

        Parameters
        ----------
        point : ArrayLike
            Next point to measure using a given plan

        Returns
        -------
        plan_name : str
        plan_args : List
            List of arguments to pass to plan from a point to measure.
        plan_kwargs : dict
            Dictionary of keyword arguments to pass the plan, from a point to measure.
        """
        ...

    @staticmethod
    @abstractmethod
    def unpack_run(run: BlueskyRun) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        """
        Consume a Bluesky run from tiled and emit the relevant x and y for the agent.

        Parameters
        ----------
        run : BlueskyRun

        Returns
        -------
        independent_var :
            The independent variable of the measurement
        dependent_var :
            The measured data, processed for relevance
        """
        ...

    @abstractmethod
    def tell(self, x, y) -> Dict[str, List]:
        """
        Tell the agent about some new data
        Parameters
        ----------
        x :
            Independent variable for data observed
        y :
            Dependent variable for data observed

        Returns
        -------
        dict
            Dictionary to be unpacked or added to a document

        """
        ...

    @abstractmethod
    def ask(self, batch_size: int) -> Tuple[Dict[str, List], Sequence]:
        """
        Ask the agent for a new batch of points to measure.

        Parameters
        ----------
        batch_size : int
            Number of new points to measure

        Returns
        -------
        doc : dict
            key metadata from the ask approach
        next_points : Sequence
            Sequence of independent variables of length batch size

        """
        ...

    def report(self, **kwargs) -> Dict[str, List]:
        """
        Create a report given the data observed by the agent.
        This could be potentially implemented in the base class to write document stream.
        Additional functionality for converting the report dict into an image or formatted report is
        the duty of the child class.
        """

        raise NotImplementedError

    def tell_many(self, xs, ys) -> Sequence[Dict[str, List]]:
        """
        Tell the agent about some new data. It is likely that there is a more efficient approach to
        handling multiple observations for an agent. The default behavior is to iterate over all
        observations and call the ``tell`` method.

        Parameters
        ----------
        xs : list, array
            Array of independent variables for observations
        ys : list, array
            Array of dependent variables for observations

        Returns
        -------
        list_of_dict

        """
        tell_emits = []
        for x, y in zip(xs, ys):
            tell_emits.append(self.tell(x, y))
        return tell_emits

    @property
    def queue_add_position(self) -> Union[int, Literal["front", "back"]]:
        return self._queue_add_position

    @queue_add_position.setter
    def queue_add_position(self, position: Union[int, Literal["front", "back"]]):
        self._queue_add_position = position

    def update_priority(self, position: Union[int, Literal["front", "back"]]):
        """Convenience method to update the priority of a direct to queue agent

        Parameters
        ----------
        position : Union[int, Literal[&quot;front&quot;, &quot;back&quot;]]
            Position in priority for queue.
        """
        self.queue_add_position = position

    @property
    def ask_on_tell(self) -> bool:
        return self._ask_on_tell

    @ask_on_tell.setter
    def ask_on_tell(self, flag: bool):
        self._ask_on_tell = flag

    @property
    def report_on_tell(self) -> bool:
        return self._report_on_tell

    @report_on_tell.setter
    def report_on_tell(self, flag: bool):
        self._report_on_tell = flag

    def enable_continuous_reporting(self):
        """Enable agent to report each time it receives data."""
        self.report_on_tell = True

    def disable_continuous_reporting(self):
        """Disable agent to report each time it receives data."""
        self.report_on_tell = False

    def enable_continuous_suggesting(self):
        """Enable agent to suggest new points to the queue each time it receives data."""
        self.ask_on_tell = True

    def disable_continuous_suggesting(self):
        """Disable agent to suggest new points to the queue each time it receives data."""
        self.ask_on_tell = False

    def enable_direct_to_queue(self):
        self._direct_to_queue = True

    def disable_direct_to_queue(self):
        self._direct_to_queue = False

    @property
    def name(self) -> str:
        """Short string name"""
        return "agent"

    @classmethod
    def build_from_argparse(cls, parser: ArgumentParser, **kwargs):
        args = parser.parse_args()
        _kwargs = vars(args)
        _kwargs.update(kwargs)
        return cls.__init__(**_kwargs)

    @classmethod
    def constructor_argparser(cls) -> ArgumentParser:
        """Convenience method to put all arguments into a parser"""
        parser = ArgumentParser(description=cls.__doc__, formatter_class=RawDescriptionHelpFormatter)
        parser.add_argument("--kafka-group-id", required=True)
        parser.add_argument("--kafka-bootstrap-servers", required=True)
        parser.add_argument("--kafka-consumer-config", required=True)
        parser.add_argument("--kafka-producer-config", required=True)
        parser.add_argument("--publisher-topic", required=True)
        parser.add_argument("--subscription-topics", required=True)
        parser.add_argument("--data-profile-name", required=True)
        parser.add_argument("--agent-profile-name", required=True)
        parser.add_argument("--qserver-host", required=True)
        parser.add_argument("--qserver-api-key", required=True)
        parser.add_argument("--metadata")

        return parser

    def _write_event(self, stream, doc):
        """Add event to builder as event page, and publish to catalog"""
        for key in doc:
            doc[key] = [doc[key]]  # Encapsulate everything in a list for event pages
        if not doc:
            logger.info(f"No doc presented to write_event for stream {stream}")
            return
        if stream in self.builder._streams:
            self.builder.add_data(stream, data=doc)
        else:
            self.builder.add_stream(stream, data=doc)
            self.agent_catalog.v1.insert(*self.builder._cache._ordered[-2])  # Add descriptor for first time
        self.agent_catalog.v1.insert(*self.builder._cache._ordered[-1])
        return self.builder._cache._ordered[-1][1]["uid"]

    def _add_to_queue(self, next_points, uid):
        """
        Adds a single set of points to the queue as bluesky plans

        Parameters
        ----------
        next_points : Iterable
            New points to measure
        uid : str

        Returns
        -------

        """
        for point in next_points:
            plan_name, args, kwargs = self.measurement_plan(point)
            kwargs.setdefault("md", {})
            kwargs["md"].update(self.default_plan_md)
            kwargs["md"]["agent_ask_uid"] = uid
            plan = BPlan(
                plan_name,
                *args,
                **kwargs,
            )
            r = self.re_manager.item_add(plan, pos=self.queue_add_position)
            logger.debug(f"Sent http-server request for point {point}\n." f"Received reponse: {r}")
        return

    def _check_queue_and_start(self):
        """
        If the queue runs out of plans, it will stop.
        That is, adding a plan to an empty queue will not run the plan.
        This will not be an issue when there are many agents adding plans to a queue.
        Giving agents the autonomy to start the queue is a risk that will be mitigated by
        only allowing the beamline scientists to open and close the environment.
        A queue cannot be started in a closed environment.
        """
        status = self.re_manager.status(reload=True)
        if (
            status["items_in_queue"] == 1
            and status["worker_environment_exists"] is True
            and status["manager_state"] == "idle"
        ):
            self.re_manager.queue_start()
            logger.info("Agent is starting an idle queue with exactly 1 item.")

    def add_suggestions_to_queue(self, batch_size: int):
        """Calls ask, adds suggestions to queue, and writes out event"""
        doc, next_points = self.ask(batch_size)
        uid = self._write_event("ask", doc)
        logger.info(f"Issued ask and adding to the queue. {uid}")
        self._add_to_queue(next_points, uid)
        self._check_queue_and_start()

    def _create_suggestion_list(self, points, uid):
        """Create suggestions for adjudicator"""
        raise NotImplementedError
        """Not implementing yet to lighten PR load. Copied is implementation from MMM.
        suggestions = []
        for point in points:
            kwargs = self.measurement_plan_kwargs(point)
            kwargs.setdefault("md", {})
            kwargs["md"].update(self.default_plan_md)
            kwargs["md"]["agent_ask_uid"] = uid
            args = self.measurement_plan_args(point)
            suggestions.append(
                Suggestion(
                    ask_uid=uid,
                    plan_name=self.measurement_plan_name,
                    plan_args=args,
                    plan_kwargs=kwargs,
                )
            )
        return suggestions
        """

    def generate_suggestions_for_adjudicator(self, batch_size: int):
        raise NotImplementedError
        """ Not implementing yet to lighten PR load. Copied is implementation from MMM.
        doc, next_points = self.ask(batch_size)
        uid = self._write_event("ask", doc)
        logger.info(f"Issued ask and sending to adjudicator. {uid}")
        suggestions = self._create_suggestion_list(next_points, uid)
        msg = AdjudicatorMsg(
            agent_name=self.agent_name,
            suggestions_uid=str(uuid.uuid4()),
            suggestions={self.beamline_tla: suggestions},
        )
        self.kafka_producer(ADJUDICATOR_STREAM_NAME, msg.dict())
        """

    def generate_report(self, **kwargs):
        doc = self.report(**kwargs)
        uid = self._write_event("report", doc)
        logger.info(f"Issued report request and writing event. {uid}")

    @staticmethod
    def trigger_condition(uid) -> bool:
        return True

    def _on_stop_router(self, name, doc):
        """Document router that runs each time a stop document is seen."""
        if name != "stop":
            return

        uid = doc["run_start"]
        if not self.trigger_condition(uid):
            logger.debug(
                f"New data detected, but trigger condition not met. The agent will ignore this start doc: {uid}"
            )
            return

        logger.info(f"New data detected, telling the agent about this start doc: {uid}")
        run = self.exp_catalog[uid]
        try:
            independent_variable, dependent_variable = self.unpack_run(run)
        except KeyError as e:
            logger.warning(f"Ignoring key error in unpack for data {uid}:\n {e}")
            return

        # Tell
        logger.debug("Telling agent about some new data.")
        doc = self.tell(independent_variable, dependent_variable)
        doc["exp_uid"] = [uid]
        self._write_event("tell", doc)

        # Report
        if self.report_on_tell:
            self.generate_report(**self.default_report_kwargs)

        # Ask
        if self.ask_on_tell:
            if self._direct_to_queue:
                self.add_suggestions_to_queue(1)
            else:
                self.generate_suggestions_for_adjudicator(1)

    def tell_agent_by_uid(self, uids: Iterable):
        """Give an agent an iterable of uids to learn from.
        This is an optional behavior for priming an agent without a complete restart."""
        logger.info("Telling agent list of uids")
        for uid in uids:
            logger.info(f"Telling agent about start document{uid}")
            run = self.exp_catalog[uid]
            independent_variable, dependent_variable = self.unpack_run(run)
            doc = self.tell(independent_variable, dependent_variable)
            doc["exp_uid"] = [uid]
            self._write_event("tell", doc)

    def start(self, ask_at_start=False):
        logger.debug("Issuing Agent start document and starting to listen to Kafka")
        self.builder = RunBuilder(metadata=self.metadata)
        self.agent_catalog.v1.insert("start", self.builder._cache.start_doc)
        logger.info(f"Agent name={self.builder._cache.start_doc['agent_name']}")
        logger.info(f"Agent start document uuid={self.builder._cache.start_doc['uid']}")
        if ask_at_start:
            self.add_suggestions_to_queue(1)
        self.kafka_consumer.start()

    def stop(self, exit_status="success", reason=""):
        logger.debug("Attempting agent stop.")
        self.builder.close(exit_status=exit_status, reason=reason)
        self.agent_catalog.v1.insert("stop", self.builder._cache.stop_doc)
        self.kafka_producer.flush()
        self.kafka_consumer.stop()
        logger.info(
            f"Stopped agent with exit status {exit_status.upper()}"
            f"{(' for reason: ' + reason) if reason else '.'}"
        )

    def signal_handler(self, signal, frame):
        self.stop(exit_status="abort", reason="forced exit ctrl+c")
        sys.exit(0)

    @staticmethod
    def qserver_from_host_and_key(host: str, key: str):
        """Convenience method to prouduce RE Manager object to manage communication with Queue Server.
        This is one of several paradigms for communication, albeit a common one.
        See bluesky_queueserver_api documentation for more details.


        Parameters
        ----------
        host : str
            URI for host of HTTP Server
        key : str
            Authorization key for HTTP Server API

        Returns
        -------
        qserver : bluesky_queueserver_api.api_threads.API_Threads_Mixin
        """
        from bluesky_queueserver_api.http import REManagerAPI

        qserver = REManagerAPI(http_server_uri=host)
        qserver.set_authorization_key(api_key=key)
        return qserver

    @classmethod
    def from_config_kwargs(
        cls,
        kafka_group_id: str,
        kafka_bootstrap_servers: str,
        kafka_consumer_config: dict,
        kafka_producer_config: dict,
        publisher_topic: str,
        subscripion_topics: List[str],
        data_profile_name: str,
        agent_profile_name: str,
        qserver_host: str,
        qserver_api_key: str,
        **kwargs,
    ):
        """Convenience method for producing an Agent from keyword arguments describing the
        Kafka, Tiled, and Qserver setup.
        Assumes tiled is loaded from profile, and the REManagerAPI is based on the http api.

        Parameters
        ----------
        kafka_group_id : str
            Required string identifier for the consumer's Kafka Consumer group.
        kafka_bootstrap_servers : str
            Comma-delimited list of Kafka server addresses as a string
            such as ``'broker1:9092,broker2:9092,127.0.0.1:9092'``
        kafka_consumer_config : dict
            Override default configuration or specify additional configuration
            options to confluent_kafka.Consumer.
        kafka_producer_config : dict
            Override default configuration or specify additional configuration
            options to confluent_kafka.Producer.
        publisher_topic : str
            Existing topic to publish agent documents to.
        subscripion_topics : List[str]
            List of existing_topics as strings such as ["topic-1", "topic-2"]. These should be
            the sources of the Bluesky stop documents that trigger ``tell`` and agent directives.
        data_profile_name : str
            Tiled profile name to serve as source of data (BlueskyRuns) for the agent.
        agent_profile_name : str
            Tiled profile name to serve as storage for the agent documents.
        qserver_host : str
            Host to POST requests to. Something akin to 'http://localhost:60610'
        qserver_api_key : str
            Key for API security.
        kwargs : dict
            Additional keyword arguments for init
        """
        from bluesky_queueserver_api.http import REManagerAPI
        from tiled.client import from_profile

        kafka_consumer = AgentConsumer(
            topics=subscripion_topics,
            bootstrap_servers=kafka_bootstrap_servers,
            group_id=kafka_group_id,
            consumer_config=kafka_consumer_config,
        )
        kafka_producer = Publisher(
            topic=publisher_topic,
            bootstrap_servers=kafka_bootstrap_servers,
            key="",
            producer_config=kafka_producer_config,
        )
        tiled_data_node = from_profile(data_profile_name)
        tiled_agent_node = from_profile(agent_profile_name)

        re_manager = REManagerAPI(http_server_uri=qserver_host)
        re_manager.set_authorization_key(api_key=qserver_api_key)

        if "metadata" in kwargs:
            kwargs["metadata"].update(
                dict(tiled_data_profile=data_profile_name, tiled_agent_profile=agent_profile_name)
            )

        return cls.__init__(
            kafka_consumer=kafka_consumer,
            kafka_producer=kafka_producer,
            tiled_data_node=tiled_data_node,
            tiled_agent_node=tiled_agent_node,
            qserver=re_manager,
            **kwargs,
        )
