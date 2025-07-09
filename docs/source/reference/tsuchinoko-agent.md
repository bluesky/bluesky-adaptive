# Tsuchinoko Agent

The Tsuchinoko agent enables using both the [gpCAM](https://gpcam.readthedocs.io/en/stable/) suggestion engine and the 
[Tsuchinoko](https://tsuchinoko.readthedocs.io/en/latest/) graphical user interface.

## Key Features of Tsuchinoko Agent

- **Bayesian Optimization**: Utilizes a GP-based surrogate model for decision-making.
- **Highly Customizable**: Modular flexibility of the suggestion algorithm with acquisition, kernel, noise, prior mean, and cost functions.
- **High Performance**: Fast training and prediction with options to support distributed processing on HPC.
- **Feedback and Control**: Visualization and live control of the agent from the Tsuchinoko desktop application keeps the user _in the loop_.

```{eval-rst}
.. autoclass:: bluesky_adaptive.agents.tsuchinoko.TsuchinokoAgent
```

To utilize the Tsuchinoko agent, as with other agent classes, the `measurement_plan` and `unpack_run` abstract methods
must be defined in a subclass.

To run the Tsuchinoko agent, you would need:
- An installation of `bluesky_adaptive` with the optional `tsuchinoko` dependencies installed.
- A running `TsuchinokoAgent`
- A running `tsuchinoko` instance with a `BlueskyAdaptiveEngine` as its execution engine and a `GPCAMInProcessEngine` as
  its adaptive engine.

See `tests/test_tsuchinoko_agents.py` for refenece.