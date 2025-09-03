# Included Example Agents

Bluesky Adaptive provides several example agents that can be easily integrated into your experimental workflows.
These agents demonstrate a range of functionalities from simple sequential decision-making to more complex data-driven approaches utilizing machine learning models.
These can serve as primary examples for developing your own agents, or used directly in your experiments with minimal modifications.
Below, we introduce and describe the functionality of these example agents.

## Sequential Agent

The `SequentialAgentBase` is designed for experiments requiring a predetermined sequence of measurements.
This agent iteratively provides the next point in the sequence upon each suggestion and keeps track of the number of queries made.
This is better served by actual plans since it is not particularly adaptive, but showcases the basic structure of an agent.

### Key Features of Sequential Agent

- **Sequential Decision Making**: Operates by following a pre-defined sequence of experimental conditions.
- **Boundary Checking**: Optionally enforces relative bounds for the sequence values to ensure measurements remain within desired limits.
- **Progress Reporting**: Provides a report on the percentage completion of the sequence.

```{eval-rst}
.. autoclass:: bluesky_adaptive.agents.simple.SequentialAgentBase
```

## Sklearn Estimator Agent

The `SklearnEstimatorAgentBase` and its derivatives like `DecompositionAgentBase` and `ClusterAgentBase` utilize Scikit-learn models for data-driven decision-making. These agents are primarily passive, focusing on analyzing datasets through decomposition and clustering techniques.

### Key Features of Sklearn Estimator Agent

- **Integration with Scikit-learn**: Leverages Sklearn estimators for data analysis.
- **Dataset Analysis**: Performs tasks such as PCA or clustering to analyze the experimental dataset.
- **Model Parameter Adjustment**: Allows for dynamic adjustment of model parameters.
- **Demonstrates Close and Restart Functionality**: Demonstrates how to close and restart the agent when its parameters are updated such that its output documents will change shape (e.g., changing the number of componetns in a decomposition).

```{eval-rst}
.. autoclass:: bluesky_adaptive.agents.sklearn.SklearnEstimatorAgentBase

.. autoclass:: bluesky_adaptive.agents.sklearn.DecompositionAgentBase

.. autoclass:: bluesky_adaptive.agents.sklearn.ClusterAgentBase
```

## BoTorch Agent

The `SingleTaskGPAgentBase` demonstrates the integration of BoTorch for Bayesian optimization using Gaussian Processes (GP).
This agent is suitable for optimizing experimental conditions based on a surrogate model of the experiment's outcome.
Specifically, it uses a single task GP, with a default UCB acquisition function, but can be customized to use other acquisition functions.

### Key Features of BoTorch Agent

- **Bayesian Optimization**: Utilizes a GP-based surrogate model for decision-making.
- **Customizable Acquisition Function**: Supports custom acquisition functions to dictate the exploration-exploitation balance.
- **Device Compatibility**: Operates on either CPU or CUDA-enabled GPU devices for computation.
- **State Management**: Manages and reports the state of the acquisition function for transparency and reproducibility.

```{eval-rst}
.. autoclass:: bluesky_adaptive.agents.botorch.SingleTaskGPAgentBase
```

These example agents provided by Bluesky Adaptive offer a starting point for integrating intelligent, data-driven decision-making into your experimental setups.
Whether your experiments require simple sequential steps, data analysis through machine learning models, or sophisticated optimization strategies, these agents serve as both a practical tool and a source of inspiration for developing your custom agents.

## Tsuchinoko Agent

The Tsuchinoko agent enables using both the [gpCAM](https://gpcam.readthedocs.io/en/stable/) suggestion engine and the 
[Tsuchinoko](https://tsuchinoko.readthedocs.io/en/latest/) graphical user interface.

### Key Features of Tsuchinoko Agent

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
