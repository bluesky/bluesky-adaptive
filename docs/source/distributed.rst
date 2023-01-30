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

.. autoproperty:: bluesky_adaptive.agents.base.Agent.measurement_plan_name
.. automethod:: bluesky_adaptive.agents.base.Agent.measurement_plan_args
.. automethod:: bluesky_adaptive.agents.base.Agent.measurement_plan_kwargs


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

.. code-block:: python

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
            return dict(independent_variable=[x], observable=[y], cache_len=[len(self.independent_cache)])

        def ask(self, batch_size: int = 1) -> Tuple[dict, Sequence]:
            doc = defaultdict(list)
            for _ in range(batch_size):
                self.ask_count += 1
                doc["proposal"].append(next(self._position_generator))
            doc["ask_count"] = [self.ask_count]
            return doc, doc["proposal"]

        def report(self, **kwargs) -> dict:
            return dict(percent_completion=[self.ask_count / len(self.sequence)])

    class CMSBaseAgent:
        measurement_plan_name = "simple_scan"

        @staticmethod
        def measurement_plan_args(point) -> list:
            """
            List of arguments to pass to plan from a point to measure.
            This is a good place to transform relative into absolute motor coords.
            """
            return point

        @staticmethod
        def measurement_plan_kwargs(point) -> dict:
            """
            Construct dictionary of keyword arguments to pass the plan, from a point to measure.
            This is a good place to transform relative into absolute motor coords.
            """
            return {}

        def unpack_run(run: BlueskyRun) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
            return run.start["experiment_state"], run.primary.data["detector"]

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
                qserver_api_key=None,
            )


    class CMSSequentialAgent(SequentialAgentBase, CMSBaseAgent):
        def __init__(
            self,
            *,
            sequence: Sequence[Union[float, ArrayLike]],
            relative_bounds: Tuple[Union[float, ArrayLike]] = None,
            **kwargs,
        ) -> None:
            _default_kwargs = self.get_beamline_kwargs()
            _default_kwargs.update(kwargs)
            super().__init__(sequence=sequence, relative_bounds=relative_bounds, **_default_kwargs)
