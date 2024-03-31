# Lockstep Agents and Synchronous Integration with RunEngine

## Overview

In the realm of experimental orchestration, the concept of adaptiveness represents the ability of an experiment to respond dynamically to the data it generates.
`bluesky-adaptive` enriches the `bluesky` ecosystem by providing tools for in-the-loop feedback, enabling experiments to make intelligent, data-driven decisions in real time.
There are two primary computational motifs for adaptive behavior: synchronous and asynchronous (See [Axes of Adaptive](axes-of-adaptive)).
Lockstep agents working synchronously with the RunEngine offer a powerful means of ensuring that each step in an experiment is optimally informed by the latest available data.

## Synchronous Communication: In-the-Loop Decisions

Lockstep agents are designed to operate in close synchronization with the RunEngine, making decisions on a per-event or per-run basis.
This synchronous, "in-the-loop" communication model requires the experiment to pause momentarily, awaiting the agent's recommendation before proceeding.
While this may introduce a slight delay in the data collection process, it ensures that each measurement is made under the most favorable conditions, as determined by the latest analysis.
This approach is particularly beneficial when the cost of suboptimal measurements is high or when the agent's decision-making process is sufficiently rapid.

### Adaptive Plans and Factory Functions

The core of synchronous integration lies in the utilization of adaptive plans and factory functions within `bluesky-adaptive`.
These tools enable the seamless coordination of data collection queues and callbacks with the RunEngine.
By employing adaptive plans, experimenters can dynamically refine the granularity of a scan, adjust experimental parameters for optimal signal quality, or select the next set of samples for investigation—all based on real-time feedback from the data.

### Practical Implementation

Implementing lockstep agents requires a clear understanding of the experiment's goals and the types of decisions that need to be made.
`bluesky-adaptive` offers a framework for defining these agents and integrating them with the RunEngine.
Experimenters can specify the conditions under which the agent should intervene, the criteria for evaluating the data, and the logic for determining the next course of action.
This flexibility allows for the creation of highly customized adaptive experiments tailored to the specific needs of each scientific inquiry.

For more detailed information on implementing lockstep agents, refer to the [How-To](../how-to/index) sections of this documentation.
