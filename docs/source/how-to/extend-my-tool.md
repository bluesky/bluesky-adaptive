# Extending Your Existing Python Tool for Use with Bluesky-Adaptive

So you already have a python tool that works with some experiments elsewhere or not using the Bluesky stack.
Whether you need to synchronize your tool's operations with the experiment's execution or allow your tool to operate asynchronously, Bluesky-Adaptive provides the necessary tools and frameworks to achieve seamless integration.
This guide is divided into two main approaches: the lockstep approach for synchronous operations and the asynchronous approach for more distributed and flexible integration.

## Lockstep Approach with Bluesky-Adaptive

The lockstep approach involves synchronizing your tool's operations with the execution of experiments in a way that each step waits for the previous one to complete before proceeding.
This method is ideal for experiments where real-time decision-making based on the latest available data is crucial.
While there are fewer moving parts in this approach, it requires tighter integration between your tool and the `RunEngine`.

### Steps to Integrate Using Lockstep Approach

1. **Identify the Decision Rate**: Determine how frequently your tool needs to analyze data and make decisions. 
This will help you decide where to insert your tool's functionality in the workflow. 
Some of this depends on how the experimental plans are structured. There is [reference material](../reference/lock-step.rst) describing the distinctions between
per-event and per-run decision making.

2. **Identify Decision Relevant Data**: First, identify points in your experimental workflow where decisions based on real-time data could optimize the experimental outcomes. Namely, choose your independent and dependent varaiables.
The "reccomender factories" take arguments that specify the independent and dependent variables as keys to be extracted from their document stream (start/event/stop).

3. **Identify the plan to take a reading**: The adaptive plans need to know how to take a reading.
This is done by specifying a plan that takes a reading and the detectors to be read.

4. **Define Agent Methods**: Your object requires some notion of `ask` and `tell` methods. `report` is optional and not implemented in the lockstep case.
These methods are used by the `reccomender_factory` to interact with your agent.
Currently, there is no abstract base class to enforce these methods, because there are only two methods to implement.
The `tell` method should be fast, and is often a caching operation, e.g., updating the arrays your agent uses to make decisions.
The `ask` method can then be used to trigger your existing logic to make a decision and return the next point to measure (in independent variable space from step 2).
Unlike the asynchronous case, in the lockstep case, neither of these methods currently return documents, but this may change in the future.

    ```python
    from numpy.typing import ArrayLike
    from typing import Tuple

    class YourAgent:
        def __init__(self, *args, **kwargs):
            # Initialization code for your tool
            pass

        def tell(self, x: ArrayLike, y: ArrayLike):
            # Process new data
            pass

        def ask(self) -> ArrayLike:
            # Decide on the next experiment step
            # Here you should call your tools pre-existing logic to make a choice.
            next_step = self.your_existing_logic()
            return next_step
    ```

5. **Execute with RunEngine**: Execute your custom adaptive measurement plan with the Bluesky RunEngine, ensuring real-time data analysis and adaptive decision-making.

    ```python
    RE = RunEngine({})
    reccomender, queue = reccomender_factory(your_agent, independent_keys, dependent_keys)
    your_adaptive_plan = adaptive_plan(dets, first_point, to_reccomender=reccomender, from_reccomender=queue)
    RE(your_adaptive_plan)
    ```

## Asynchronous Approach with Bluesky-Adaptive

The asynchronous approach allows your tool to operate independently from the experiment's main execution thread, making decisions and suggesting actions without blocking ongoing measurements.
This method is suited for more complex experimental setups where multiple sources of data or decision-making agents are involved.
It does involve more moving parts, which may require additional infrastructure to manage (e.g., Kafka, QueueServer, etc.).

### Steps to Integrate Using Asynchronous Approach

1. **Inherit from Base Agent Class**: Your tool should inherit from `bluesky_adaptive.agents.base.Agent`, implementing the required methods such as `ask`, `tell`, and `report`. 
An abstract base class like `base.Agent` will protect you at runtime from missing any of the required methods.
This allows your tool to receive data, make decisions, and suggest future actions.
The instructions here are the same as above, but the agent specific methods should also return a dictionary that is stored as an event document in the Bluesky document model.
Again, the `tell` method should be fast, as this happens every time a new event is emitted. The `ask` and `report` have no obligation to be quick, as they are not necessarily called in the same tight loop.


    ```python
    from bluesky_adaptive.agents.base import Agent
    from numpy.typing import ArrayLike
    from typing import Tuple

    class YourAsyncAgent(Agent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Initialization code for your tool

        def tell(self, x: ArrayLike, y: ArrayLike) -> dict:
            # Process new data
            return {}

        def ask(self) -> Tuple[ArrayLike, dict]:
            # Decide on the next experiment step
            return next_step, {}

        def report(self) -> dict:
            # Generate a report of the agent's current state
            return {}
    ```

2. **Define experiment specific methods**: The other methods detailed in [the reference section](../reference/agent-api.md) solve the same problems as declaring independent and dependent "keys" in the lockstep case.
These allow for more sophisticated interactions between the agent, data, and orchestration. 
Required methods here are:
   - `unpack_run`: Which extracts the independent and dependent variables from a `BlueskyRun` read from Tiled. 
   - `measurement_plan`: Consumes the next point (independent variable) provided by the agent decision making, and converts this into a plan name, arguments, and keyword arguments. This returns a `string` (plan name), `list` (args), and `dict` (kwargs).
   - `trigger_condition`: Which determines whether or not a `BlueskyRun` is relevant to agent. This is useful in settings where some measurements are background, or ancilary from an agent's perspective. If not imlpemented, the agent will be triggered by all runs.

    ```python
    class YourAsyncAgent(Agent):
        def unpack_run(self, run: BlueskyRun) -> Tuple[ArrayLike, ArrayLike]:
            # Extract independent and dependent variables from the run
            ...
            return x, y
        
        def measurement_plan(self, next_step: ArrayLike) -> Tuple[str, list, dict]:
            # Convert the next step into a plan name, args, and kwargs
            ...
            return plan_name, args, kwargs

        def trigger_condition(self, run: BlueskyRun) -> bool:
            # Determine if the run is relevant to the agent
            ...
            return True
    ```

3. **Deploy the Required Stack**: To use the asynchronous agents, the following stack is required during the experiment.
   - **Kafka**: To communicate between the agent and the experiment.
   - **QueueServer**: To manage the agent's responses and experimental orchestration.
   - **Tiled**: For storing the data and metadata from the experiment and agent processes. 

4. **Test the agent in Python**: Test your agent in Python to ensure that it can communicate with the experiment and make decisions based on the data received.

    ```python
    agent = YourAsyncAgent()
    agent.tell(x, y)
    next_step, report = agent.ask()
    ```
    You can also start the agent in a separate process to ensure it can communicate via Kafka messages. This will start a Kafka subscriber and block the process until the agent is stopped.
    ```python
    agent.start()
    ```

5. **Deploy Agent as a Service**: Deploy your agent as a service to run in parallel with the experiment. This can be done using the `bluesky_adaptive.server` and `uvicorn` as shown in the [how-to](./use-service.md).
