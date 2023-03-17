# Adjudicators

The purpose of an adjudicator is to provide another layer of indirection between the agents and the RunEngine Manager.
This is not required, as agents can send plans directly to the queue.
Alternatively, many agents can send plans to an adjudicator that acts as a meta-agent, filtering and deciding which plans from many agents make it to the queue.
In this way, the adjudicator acts as an extra experiment manager.
Feedback is not provided directly to the agents (i.e. no two way communication), so this is in effect, much like how high level human management communicates with low level employees.

Each adjudicator is required to implement `make_judgments`, which accepts no args or kwargs, and should return a list of tuples that contain the RE manager API, the agent name, and the Suggestion.
These tuples will by validated by Pydantic models, or can be `Judgment` objects.
This enables an agent to suggest many plans at once, to multiple beamlines!
Adjustable properties can be incorperated by the server, allowing for web and caproto control.

`make_judgments` can be called promptly after every new document, or only on user command.


## Use Case: Avoiding redundancy
One challenge of having many agents who can write to the queue is they don't know what other agents are suggesting. This can cause multiple agents to have the same idea about the next experiment, and lead an autonomous experiment to run the same plans redundantly. For example, if I had two Bayesian optimization agents that were minimizing their surrogate model uncertainty, they may have a similar idea for the next best area to measure.
An adjudicator can ensure that only one measurement gets scheduled, but both agents will still recive the data.

## Use Case: Meta-analysis of many similar agents
You may want to filter down the number of plans comming from multiple agents that are using the same underlying technique.
This mechanism for increasing diversity could be applied to a suite of exploitative optimizers, or maybe complementary decomposition approaches (NMF/PCA/Kmeans) that are suggesting regions near their primary components.
An adjudicator that is conducting analysis of many agents will take careful thought and should be tuned to the set of agents it is attending to.

## Pydantic Message API Enables multi-experiment, multi-beamline suggestions
```python
suggestion = Suggestion(ask_uid="123", plan_name="test_plan", plan_args=[1, 3], plan_kwargs={"md": {}})
msg = AdjudicatorMsg(
    agent_name="aardvark",
    suggestions_uid="456",
    suggestions={
        "pdf": [
            suggestion,
            suggestion,
        ],
        "bmm": [
            suggestion,
        ],
    },
)
```
