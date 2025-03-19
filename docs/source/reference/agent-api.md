# Agent API

We need to decide on the 'brains' of an agent or reccomender, i.e. how it will make decisions. These can span from simple sequential agents, to Gaussian Process based Bayesian optimization.
A well designed agent would be reusable across different experiments, and some examples are provided in `bluesky-adaptive` that make use of `sklearn` and `BoTorch`.
These can be effectively combined with beamline/experiment specific base classes through multiple inheritence to satisfy all the abstract methods of an `Agent`.

An agent can be broken down into these three methods:

- `ingest`: in which the agent ingests some new data
- `suggest`: in which the agent suggests what data to acquire next
- `report`: in which the agent reports about its current thinking

These three methods are the core of the agent API, that works for both the lock-step reccomendation engines, and the asynchronous agents that can be run as services.
Critically, each of the three methods will return documents that are stored as unique streams in the event model. This enables us to look back at what an agent saw and was thinking as an experimental campaign progressed. As such, it is important that the shape of each field in these docs remain consistent throughout the experiment.
A simple, but sufficiently complex example is provided in `bluesky_adaptive.agents.botorch.SingleTaskGPAgentBase`.

## A Note on Documents

```{note}
In the lockstep API the agent methods below are not expected to return documents (as of 0.3.1). 
In the asynchronous API, the agent methods below are expected to return documents.
These documents are treated like detectors in the event model, and are stored in the event model as a unique stream.
Because of this, it is critical for the agent to return documents that are consistent in shape and content.
```

This allows the Databroker, or Tiled, to slice the output of the agent and reconstruct the full dataset that informed the agent's decision making.
For example, we may want to look at a particular component of a decompositio over time, or a subset of weights in a neural network. 
If the agent has registered methdos that will change the shape fo the document stream, it should be closed and restarted. 
This can be accomplished automatically using `self.close_and_restart()` after the modification.
Detailed use of this can be found in the [sklearn example agents](../reference/example-agents.md), as a common need for decomposition and clustering agents is to change the number of components or clusters as more data is gathered. 

## Ingest

The `ingest` method converts the (x,y) pair into pytorch tensors and manages any GPU/CPU needs. It returns a document that holds the independent and dependent pair, as well as the current cache length.
This operation occurs every time the triggering document is received, therefore it should be implemented for speed.
This enables an agent to be loaded in Tiled, and the data it's been made aware of to be sliced, e.g., ```node[agent_uid].ingest.data['observable'][-10:]```.

```python
def ingest(self, x, y):
    if self.inputs is None:
        self.inputs = torch.atleast_2d(torch.tensor(x, device=self.device))
        self.targets = torch.atleast_1d(torch.tensor(y, device=self.device))
    else:
        self.inputs = torch.cat([self.inputs, torch.atleast_2d(torch.tensor(x, device=self.device))], dim=0)
        self.targets = torch.cat([self.targets, torch.atleast_1d(torch.tensor(y, device=self.device))], dim=0)
        self.inputs.to(self.device)
        self.targets.to(self.device)
    self.surrogate_model.set_train_data(self.inputs, self.targets, strict=False)
    return dict(independent_variable=x, observable=y, cache_len=len(self.targets))
```

## Suggest

The `suggest` method returns a list of documents and corresponding list of points to query/acquire.
In the BoTorch case, the agent fits a GP and optimizes and acquisition function get suggested points.
The returned documents retain one suggestion per document, along with its acquisition value, and the full state dictionary of the model for retrospective inspection.
It also highlights the most recent uid the agent ingested, and amount of data the agent knows about. In this way, it is possible to reconstruct the full dataset that informed this decision WITHOUT placing the full dataset in the document (through the use of the tell stream and queries).
Because the event model is more compatible with numpy arrays, we also take the step of converting the data here from (possibly GPU based) tensors into numpy arrays.

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
                latest_data=self.tell_cache[-1],
                cache_len=self.inputs.shape[0],
                **{
                    "STATEDICT-" + ":".join(key.split(".")): val.detach().cpu().numpy()
                    for key, val in acqf.state_dict().items()
                },
            )
        ],
        torch.atleast_1d(candidate).detach().cpu().numpy().tolist(),
    )
```

## Report

The `report` for _this_ agent is nearly identical to the `suggest` method, except it does not suggest points, and only returns one document. Other agents may have more complex reports, such as a decomposition of a dataset, or a summary of the agent's internal confidence.

```python
def report(self):
        """Fit GP, and construct acquisition function.
        Document retains state dictionary.
        """
        fit_gpytorch_mll(self.mll)
        acqf = self._partial_acqf(self.surrogate_model)
        return dict(
            latest_data=self.tell_cache[-1],
            cache_len=self.inputs.shape[0],
            **{
                "STATEDICT-" + ":".join(key.split(".")): val.detach().cpu().numpy()
                for key, val in acqf.state_dict().items()
            },
        )
```

## Beamline and experiment specific methods

### Lock-step Agents

In the case of lock-step agents, the agent follows a functional approach and assembles information directly from the document model.
In the examples provided in [the accompanying docs](lock-step), the agent retreives its data using `independent_keys` and `dependent_keys` arguments to process the relevant documents.
There is no need for a trigger condition, and the measurement plan is passed as an argument to the adaptive plans assuming that the only "independent variable" changes are motor movements.
For increased flexibility, these plans and factories can be used as boilerplate chaning the inner plans to suite the experiment needs.

### Distrubted Asynchronous Agents

In the case of distributed asynchronous agents, the agent follows an object oriented approach and requires a few additional methods.
These methods include `unpack_run`, `measurement_plan`, and optionally `trigger_condition`. When an agent detects a stop document via Kafka they check if the run is relevant to their decision making using `trigger_condition(uid)`, then load the Bluesky Run through Tiled.
The run is unpacked into independent and dependent variables. When it is time for an agent to make a decision and add something to the queue, it selects a new value(s) for the independent variable and generates a measurement plan.

The `unpack_run(run: BlueskyRun)` method consumes a run and returns a pair of independent and dependent variables. This is blocking and should be fast with respect to the experiment. Many `unpack_run` implementations will be as simple as grabbing keys from run metadata and loading the data as the dependent variable. More complex---yet still fast---approaches at BMM involve some preprocessing:

```python
def unpack_run(self, run):
    """Gets Chi(k) and absolute motor position"""
    run_preprocessor = Pandrosus()
    run_preprocessor.fetch(run, mode=self.read_mode)
    y = getattr(run_preprocessor.group, self.exp_data_type)
    if self.roi is not None:
        ordinate = getattr(run_preprocessor.group, self._ordinate)
        idx_min = np.where(ordinate < self.roi[0])[0][-1] if len(np.where(ordinate < self.roi[0])[0]) else None
        idx_max = np.where(ordinate > self.roi[1])[0][-1] if len(np.where(ordinate > self.roi[1])[0]) else None
        y = y[idx_min:idx_max]
    return run.baseline.data["xafs_x"][0], y
```

Lastly, the `measurement_plan` consumes the next point (independent variable) provided by the agent decision making, and converts this into a plan name, arguments, and keyword arguments (`measurement_plan(self, point: ArrayLike) -> Tuple[str, List, dict]`).
Ideally this should use a plan already in the scope of regular beamline operations, but can also use some agent specific plans that are developed for the particular task. For example, if an agent is trying to explore position, it may have a custom plan like `agent_change_position_and_scan`.

### Name 
An optional `name` can be given as a property/static methods as a means of distinguishing algorithmic approaches in logs.
