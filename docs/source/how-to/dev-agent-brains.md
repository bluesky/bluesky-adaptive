# Developing Agent/Recommender Algorithms

Implementing your own agent or recommender algorithm in the Bluesky Adaptive framework involves developing a class that follows the `ingest`, `suggest`, and optionally, `report` method pattern.
These methods form the core logic of how your agent interacts with experimental data, makes decisions, and potentially provides insights into its decision-making process.

When experimenting with new agent logic, it's recommended to start with a simple agent that can be easily tested and debugged. As you gain confidence in your agent's performance, you can gradually introduce more complex decision-making logic.
All that is reccomended to begin development is a mechanism for generating data to pass the agent via the `ingest` method.
This can be done by a simple simulation or by iteratting over historical data.

## The `ingest` Method

The `ingest` method is where the agent is informed about new experimental data. This is where you can update the agent's internal state based on the outcomes of past experiments. It's crucial that this method executes quickly to not delay the experimental process.

```python
def ingest(self, x, y) -> dict:
    # Example logic to update the agent's state
    self.update_internal_state(x, y)
    return {"x": x, "y": y}
```

The `ingest_many` method is exactly like the ingest method, but for multiple entries at a time. For instance, if your `BlueskyRun` actually collects several points in your experiment space. This is not required for the async agents that build off the base class, but the default implementation is a simple loop over the `ingest` method.
Therefore, it is advised to implement this method if vectorized operations are possible.

```python
def ingest_many(self, xs, ys) -> List[dict]:
    self.vetorized_update_internal_state(xs, ys)
    return [{"x": x, "y": y} for x, y in zip(xs, ys)]
```

## The `suggest` Method

The `suggest` method is called to query your agent for its next recommendation on what experiment should be conducted next.
This method should return the parameters for the next step in the experiment based on the current state of the agent.
This can include the next set of conditions to test or the next location to sample.
It should also return a dictionary whos contents will be stored as an event document in the Bluesky document model.
This dictionary gives you the opportunity to record any additional information about the agent's decision-making process, the reasoning behind the decision, or any other relevant details that you may want to analyze later.
Specifically, the `suggest` method should return a tuple of two sequences: a sequence of dictionaries and a sequence of arrays.
Even if you are only returning one step, it should be in a list.

```python
def suggest(self, batch_size) -> tuple[Sequence[Dict[str, ArrayLike]], Sequence[ArrayLike]]:
    # Example logic to determine the next step
    next_steps = [self.calculate_next_step() for _ in range(batch_size)]
    return ([{"next_step": next_step, "reasoning":..., "other_info":...} for next_step in next_steps], 
        [np.atleast_1d(next_step) for next_step in next_steps]
    )
```

## The `report` Method

The `report` method is useful for passive agents that are too expensive to run as callbacks, or for monitoring the health of an active agent.
This method can be implemented to provide a summary or analysis of the agent’s current state.
Because this report is stored in Tiled, it can be accessed by other clients or tools for further analysis or visualization.

```python
def report(self) -> dict:
    # Example logic to generate a report
    report = self.generate_summary()
    return report
```

When developing your agent, consider the specific needs of your experimental workflow and how the agent can best serve those needs through these three methods. Depending on your application, you might prioritize rapid decision-making, detailed analysis of each step, or robust reporting for human oversight.

## What should I include in the document/dictionary?
```{note}
Document values should be arrays or scalars that do not change shape throughout the experiment. Keys should be strings.
For more information see [the reference api](../reference/agent-api).
```

The `ingest` document, like the `ingest` method should be lightweight. Downstream it is stored with timestamps, so it is not necessary to include that information in the document.
These timestamps can make it useful for queries such as, "Given this report at this time, what was the most recent data the agent had seen?".

The `suggest` document should include any information that you would like to be able to query later. This could be the reasoning behind the decision, the agent's internal state, or any other information that you think would be useful to have in the future.
Particularly, if you are developing a new agent, it is useful to include the reasoning behind the decision, as this can help you debug the agent's behavior later.
This also enables user trust in the agent's decision-making process.
A more lengthy example of an `suggest` document can be found in the reference implementation of a BoTorch agent. It stores everything the needed to recreate the exact model state at the time of the decision.

```python
def suggest(self, batch_size=1):
    """Fit GP, optimize acquisition function, and return next points.
    Document retains candidate, acquisition values, and state dictionary.
    """
    if batch_size > 1:
        logger.warning(f"Batch size greater than 1 is not implemented. Reducing {batch_size} to 1.")
        batch_size = 1
    fit_gpytorch_mll(self.mll)
    acqf = self._partial_acqf(self.surrogate_model)
    acqf.to(self.device)
    candidate, acq_value = optimize_acqf(
        acq_function=acqf,
        bounds=self.bounds,
        q=batch_size,
        num_restarts=self.num_restarts,
        raw_samples=self.raw_samples,
    )
    return (
        [
            dict(
                candidate=candidate.detach().cpu().numpy(),
                acquisition_value=acq_value.detach().cpu().numpy(),
                latest_data=self.known_uid_cache[-1],
                cache_len=self.inputs.shape[0],
                **{
                    "STATEDICT-" + ":".join(key.split(".")): val.detach().cpu().numpy()
                    for key, val in acqf.state_dict().items()
                },
            )
        ],
        torch.atleast_1d(candidate).detach().cpu().numpy(),
    )
```

The `report` document should include any information that you would like to be able to query later or even live.
This could be the agent's internal state, the agent's performance, or any other information that you think would be useful to have.
The `report` is designed explicityly for processing that would be useful to have in a callback but is too expensive to run in a callback.
For example, a dataset decomposition or refinement could be run in a `report` method, and linked to downstream visualization or agents. 

```python
def report(self):
    """Return a dictionary of the agent's current state."""
    return {
        "state": self.state,
        "model": self.model.state_dict(),
        "optimizer": self.optimizer.state_dict(),
        "scheduler": self.scheduler.state_dict(),
        "latest_data": self.known_uid_cache[-1],
        "cache_len": self.inputs.shape[0],
    }
```
