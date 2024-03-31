.. _distributed:

Distributed Agents
==================

This section descibes how to work with agents whos distributed agents using the
queueserver. It is particularly pertinent when:

- The agent is slower than the measurement
- lock-step behvior is not desired
- Multiple agents should run simultaneously
- Human's and agents are meant to collaborate


These agents depend on Kafka for communication. Each time they see a stop document,
they check if the run is relevant to their decision making (*via* ``trigger_condition``),
then load the Bluesky Run through Tiled.
The run is then processed, and unpacked into the relevant independent and dependent variables
(*via* ``unpack_run``).
The agent then call's its ``tell`` method on the independent and dependent variables. In this case
The ``unpack_run`` might pull out some motor coordinates, and a 2-d detector image; and the ``tell``
might cache those as relative coordinates and some post-processing of the image.

Agents should subclass the `blusky_adaptive.agents.base.agent` and are responsible for implementing the
following.

Experiment specific
-------------------

.. automethod:: bluesky_adaptive.agents.base.Agent.measurement_plan


Agent 'brains' specific
-----------------------


.. automethod:: bluesky_adaptive.agents.base.Agent.ask
.. automethod:: bluesky_adaptive.agents.base.Agent.report
.. autoproperty:: bluesky_adaptive.agents.base.Agent.name

The complete base class is given as follows:

.. autoclass:: bluesky_adaptive.agents.base.Agent


Examples
--------

The following example creates a simple hypothetical agent that only follows a sequence of
predetermined points. While not clever, it shows the underlying mechanics.
It copies some importable code from `bluesky_adaptive.agents.simple`.
In this case the user need only construct `CMSBaseAgent` for some CMS specific values,
and combine the `SequentialAgent` and `CMSBaseAgent`.
This can be done through direct inheritence or mixins, as all necessary methods are declared
as abstract in the base Agent.

The default API supports passing the objects for communication, allowing for flexible customization.
A convenience method `from_config_kwargs` supports automatic construction of the Kafka producer and consumers,
as well as the qserver API using some common configuration keys. Both are demonstrated below for reference.

.. code-block:: python


    from bluesky_queueserver_api.http import REManagerAPI
    import uuid
    from typing import Sequence, Tuple, Union

    import nslsii.kafka_utils
    from numpy.typing import ArrayLike

    class SequentialAgentBase(Agent, ABC):
        """Agent Mixin to take a pre-defined sequence and walk through it on ``ask``.

        Parameters
        ----------
        sequence : Sequence[Union[float, ArrayLike]]
            Sequence of points to be queried
        relative_bounds : Tuple[Union[float, ArrayLike]], optional
            Relative bounds for the members of the sequence to follow, by default None

        Attributes
        ----------
        independent_cache : list
            List of the independent variables at each observed point
        observable_cache : list
            List of all observables corresponding to the points in the independent_cache
        sequence : Sequence[Union[float, ArrayLike]]
            Sequence of points to be queried
        relative_bounds : Tuple[Union[float, ArrayLike]], optional
            Relative bounds for the members of the sequence to follow, by default None
        ask_count : int
            Number of queries this agent has made
        """

        name = "sequential"

        def __init__(
            self,
            *,
            sequence: Sequence[Union[float, ArrayLike]],
            relative_bounds: Tuple[Union[float, ArrayLike]] = None,
            **kwargs,
        ) -> None:
            super().__init__(**kwargs)
            self.independent_cache = []
            self.observable_cache = []
            self.sequence = sequence
            self.relative_bounds = relative_bounds
            self.ask_count = 0
            self._position_generator = self._create_position_generator()

        def _create_position_generator(self) -> Generator:
            """Yield points from sequence if within bounds"""
            for point in self.sequence:
                if self.relative_bounds:
                    arr = np.array(point)
                    condition = arr <= self.relative_bounds[1] or arr >= self.relative_bounds[0]
                    try:
                        if condition:
                            yield point
                            continue
                        else:
                            logger.warning(
                                f"Next point will be skipped.  {point} in sequence for {self.instance_name}, "
                                f"is out of bounds {self.relative_bounds}"
                            )
                    except ValueError:  # Array not float
                        if condition.all():
                            yield arr
                            continue
                        else:
                            logger.warning(
                                f"Next point will be skipped.  {point} in sequence for {self.instance_name}, "
                                f"is out of bounds {self.relative_bounds}"
                            )
                else:
                    yield point

        def tell(self, x, y) -> dict:
            self.independent_cache.append(x)
            self.observable_cache.append(y)
            return dict(independent_variable=x, observable=y, cache_len=len(self.independent_cache))

        def ask(self, batch_size: int = 1) -> Tuple[Sequence[dict[str, ArrayLike]], Sequence[ArrayLike]]:
            docs = []
            proposals = []
            for _ in range(batch_size):
                self.ask_count += 1
                proposals.append(next(self._position_generator))
                docs.append(dict(proposal=proposals[-1], ask_count=self.ask_count))
            return docs, proposals

        def report(self, **kwargs) -> dict:
            return dict(percent_completion=self.ask_count / len(self.sequence))

    class CMSBaseAgent:

        def measurement_plan(self, point):
            return "simple_scan", point, {}

        def unpack_run(run: BlueskyRun) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
            return run.start["experiment_state"], run.primary.data["detector"]

        @staticmethod
        def get_beamline_objects() -> dict:
            beamline_tla = "cms"
            kafka_config = nslsii.kafka_utils._read_bluesky_kafka_config_file(
                config_file_path="/etc/bluesky/kafka.yml"
            )
            qs = REManagerAPI(http_server_uri=f"https://qserver.nsls2.bnl.gov/{beamline_tla}")
            qs.set_authorization_key(api_key=None)

            kafka_consumer = bluesky_adaptive.agents.base.AgentConsumer(
                topics=[
                    f"{beamline_tla}.bluesky.runengine.documents",
                ],
                consumer_config=kafka_config["runengine_producer_config"],
                bootstrap_servers=kafka_config["bootstrap_servers"],
                group_id=f"echo-{beamline_tla}-{str(uuid.uuid4())[:8]}"
            )
            kafka_producer = bluesky_kafka.Publisher(
                topic=f"{beamline_tla}.bluesky.adjudicators",
                bootstrap_servers=kafka_config["bootstrap_servers"],
                key="cms.key",
                producer_config=kafka_config["runengine_producer_config"]
            )

            return dict(kafka_consumer=kafka_consumer,
                        kafka_producer=kafka_producer,
                        tiled_data_node=tiled.client.from_profile(f"{beamline_tla}"),
                        tiled_agent_node=tiled.client.from_profile(f"{beamline_tla}_bluesky_sandbox"),
                        qserver=qs)


        @staticmethod
        def get_beamline_kwargs() -> dict:
            beamline_tla = "cms"
            kafka_config = nslsii.kafka_utils._read_bluesky_kafka_config_file(
                config_file_path="/etc/bluesky/kafka.yml"
            )
            return dict(
                kafka_group_id=f"echo-{beamline_tla}-{str(uuid.uuid4())[:8]}",
                kafka_bootstrap_servers=kafka_config["bootstrap_servers"],
                kafka_consumer_config=kafka_config["runengine_producer_config"],
                kafka_producer_config=kafka_config["runengine_producer_config"],
                publisher_topic=f"{beamline_tla}.bluesky.adjudicators",
                subscripion_topics=[
                    f"{beamline_tla}.bluesky.runengine.documents",
                ],
                data_profile_name=f"{beamline_tla}",
                agent_profile_name=f"{beamline_tla}_bluesky_sandbox",
                qserver_host=f"https://qserver.nsls2.bnl.gov/{beamline_tla}",
                qserver_api_key=None
            )




    class CMSSequentialAgent(CMSBaseAgent, SequentialAgentBase):
        def __init__(
            self,
            *,
            sequence: Sequence[Union[float, ArrayLike]],
            relative_bounds: Tuple[Union[float, ArrayLike]] = None,
            **kwargs,
        ):
            _default_kwargs = self.get_beamline_objects()
            _default_kwargs.update(kwargs)
            super().__init__(sequence=sequence, relative_bounds=relative_bounds, **_default_kwargs)

        @classmethod
        def from_config_kwargs(cls, *,
            sequence: Sequence[Union[float, ArrayLike]],
            relative_bounds: Tuple[Union[float, ArrayLike]] = None,
            **kwargs,):
            _default_kwargs = self.get_beamline_kwargs()
            _default_kwargs.update(kwargs)
            super().from_kwargs(sequence=sequence, relative_bounds=relative_bounds, **_default_kwargs)
