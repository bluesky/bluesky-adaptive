# Developing Communication Between an Asynchronous Agent and Experiment

The details of code necessary for `unpack_run`, `measurement_plan`, and `trigger_condition` will all vary between experimental setups, but is likely available in the current Blueky orchestration.
What often presents a challenge is in ensuring communication between the orhestration and the agent.
These components generally involve access to the experimental set-up, so can be a blocker for new users. 
It is advisable for experimental or beamline staff to develop a Mixin class or set of default objects for initializing the agent communication. 

The [asynchronous agent class](../reference/distributed.rst) is initialized with a Kafka consumer (and optionally a producer), Tiled objects for reading and writing documents, and an object for the QueueServer API.
Many of these can be tested without interfering with experimental operations.
For data consumers, it is imporatnt to ensure that there are appropriate permissions to read the data that the agent will need to make decisions.

A commonly used pattern is to test and initialize these components in a mixin class.
An `AgentConsumer` class is provided for Kafka consumers with additional functionality beyond the Bluesky Kafka RunRouter.
In the example below, all addresses will need to be adjusted for your use case. 

```python
from bluesky_adaptive.agents.base import AgentConsumer
class ExperimentSpecificMixin:

    @classmethod
    def from_defaults(cls):
        kwargs = cls.get_experiment_objects()
        return cls(**kwargs)

    def get_experiment_objects():
        exp_tla = "tla"
        kafka_config = nslsii.kafka_utils._read_bluesky_kafka_config_file(
            config_file_path="..."
        )
        qs = REManagerAPI(zmq_control_addr="tcp://qserver-address:60615")

        kafka_consumer = AgentConsumer(
            topics=[
                f"{exp_tla}.bluesky.analyzed.documents",
            ],
            consumer_config=kafka_config["runengine_producer_config"],
            bootstrap_servers=",".join(kafka_config["bootstrap_servers"]),
            group_id=f"echo-{exp_tla}-{str(uuid.uuid4())[:8]}",
        )

        kafka_producer = Publisher(
            topic=f"{exp_tla}.bluesky.adjudicators",
            bootstrap_servers=",".join(kafka_config["bootstrap_servers"]),
            key="{exp_tla}.key",
            producer_config=kafka_config["runengine_producer_config"],
        )

        return dict(
            kafka_consumer=kafka_consumer,
            kafka_producer=kafka_producer,
            tiled_data_node=tiled.client.from_uri(
            f"https://tiled.nsls2.bnl.gov/api/v1/node/metadata/{beamline_tla}/raw"
            ),
            tiled_agent_node=tiled.client.from_uri(
                f"https://tiled.nsls2.bnl.gov/api/v1/node/metadata/{beamline_tla}/processed"
            ),
            qserver=qs,
        )
```

Given this mixin, you can readily test the communication of your agent using the API's of [`bluesky-kafka`](https://github.com/bluesky/bluesky-kafka), [`tiled`](https://github.com/bluesky/tiled), and [`bluesky-queueserver`](https://github.com/bluesky/bluesky-queueserver).


```python
object_dict = ExperimentSpecificMixin.get_experiment_objects()
kafka_consumer = object_dict["kafka_consumer"]
kafka_producer = object_dict["kafka_producer"]
tiled_data_node = object_dict["tiled_data_node"]
tiled_agent_node = object_dict["tiled_agent_node"]
qserver = object_dict["qserver"]
```

Poll the Kafka consumer to ensure that it is working. This should be accomplished with an ongoing experiment where the Run Engine is producing documents. 

```python
kafka_consumer.poll(timeout=1.0)
```

Publish a message to the Kafka producer to ensure that it is working. 

```python
kafka_producer.publish_message(
    message={"key": "value"},
    key="key",
)
```

Read and write to the tiled data node to ensure that it is working. 

```python
run = tiled_data_node[known_uid]
# Check that the data is as expected
...
compose_run_bundle = compose_run(metadata={})    
tiled_agent_node.v1.insert("start", compose_run_bundle.start_doc)
stop_doc = compose_run_bundle.compose_stop(exit_status="success", reason="")
tiled_agent_node.v1.insert("stop", stop_doc)
run = tiled_agent_node[compose_run_bundle.start_doc["uid"]]
# Check that you can get the empty BlueskyRun.
```

Ping the QueueServer to ensure that it is working. 

```python
qserver.ping()
```
