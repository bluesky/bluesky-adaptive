# Running Your Agent as a Service with Bluesky Adaptive

Running your agent as a service in the Bluesky Adaptive ecosystem enables dynamic experimental control and decision-making in an asynchronous, distributed environment. 
This document outlines how to configure and deploy your agent as a service, leveraging FastAPI for easy interaction and management.

## Basic Agent Setup

Start by defining your agent class and its operational parameters.
The following example demonstrates a basic agent setup, registering variables and defining startup and shutdown routines.

```python
import sys
from bluesky_adaptive.server import register_variable, shutdown_decorator, startup_decorator

class Agent:
    def __init__(self):
        self._depth = 10
        self._width = 60

aa = AA()
bb = {"x": 100, "y": 900}
_v = 600
_no_pv = 100

def some_function(a):
    print(f"Function is running: a = {a!r}", flush=True)

@startup_decorator
def startup1():
    print("This is startup function #1")
    aa._depth = 20

@shutdown_decorator
def shutdown1():
    print("This is shutdown function #1")

register_variable("depth", aa, "_depth", pv_type="float")
```

This agent can then be launched with with a uvicorn service by setting the `BS_AGENT_STARTUP_SCRIPT_PATH` environment variable to the path of the script and running the following command:

```bash
BS_AGENT_STARTUP_SCRIPT_PATH=path/to/agent/script uvicorn bluesky_adaptive.server:app --reload
```
`--reload` makes the server restart after code changes. Only use this option for development.

## Advanced Agent with FastAPI

For a more advanced setup involving the `bluesky_adaptive.agents` base classes, follow this pattern:

```python

class TestSequentialAgent(SequentialAgentBase):
    def __init__(self, pub_topic, sub_topic, kafka_bootstrap_servers, kafka_producer_config, tiled_profile, **kwargs):
        # Initialization code here
        pass

    def server_registrations(self):
        # Register methods and properties as variables for FastAPI interaction
        self._register_property("queue_add_position")
        self._register_method("generate_report")
        super().server_registrations()

    # Example method to register
    def report(self, **kwargs) -> dict:
        return {"test": "report"}

# Example of starting and stopping the agent itself
@startup_decorator
def startup_agent():
    agent.start()

@shutdown_decorator
def shutdown_agent():
    return agent.stop()

# Agent instantiation and registration of variables for FastAPI
agent = TestSequentialAgent(...)
register_variable("operating_mode", None, None, getter=agent.operating_mode_getter, setter=agent.operating_mode_setter, pv_type="str")
```

Here we accomplished server registrations in the class definition:

```python
def server_registrations(self):
    # Register methods and properties as variables for FastAPI interaction
    self._register_property("queue_add_position")
    self._register_method("generate_report")
    super().server_registrations()
```

with additional customization in the startup script:

```python
register_variable("operating_mode", None, None, getter=agent.operating_mode_getter, setter=agent.operating_mode_setter, pv_type="str")
```

The startup and shutdown functions are then used to start and stop the agent Kafka consumer.

This can again be started using the 
```bash
BS_AGENT_STARTUP_SCRIPT_PATH=path/to/agent/script uvicorn bluesky_adaptive.server:app --reload
```

## Running Your Agent as a Service

Once your agent is configured, you can deploy it as a service, allowing for real-time control and adjustment of experimental parameters through HTTP requests. FastAPI facilitates this by providing a robust, easy-to-use platform for service deployment.

Utilize the `startup_decorator` and `shutdown_decorator` to manage the lifecycle of your service, ensuring that resources are appropriately allocated and released. Registering variables as shown allows for dynamic interaction with the agent's operational parameters, enabling users to adjust experiment settings on the fly.

By following these guidelines, you can effectively run your agent as a service within the Bluesky Adaptive ecosystem, harnessing the power of asynchronous communication and distributed decision-making to enhance experimental workflows.

## Exploring the API through a web browser

By default, FastAPI generates an interactive API documentation using Swagger UI. You can access this documentation by navigating to the `/docs` endpoint of your FastAPI application. 
If your application is running on the default host and port, the URL for the Swagger UI would be:

```
http://127.0.0.1:8000/docs
```

This URL might change if you configured the uvicorn launch script to run on a different host or port.
When you visit this URL in a web browser, you'll see a user-friendly interface that lists all the routes, parameters, and bodies the API expects.
You can also try out the API directly from this interface by filling out the required fields and executing the requests.
