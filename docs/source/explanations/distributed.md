# Asynchronous Agents

Asynchronous and decoupled feedback mechanisms play a critical role in expanding the capabilities of experimental orchestration beyond the confines of synchronous, lock-step operations.
These mechanisms allow for a more flexible, efficient, and intelligent approach to experimental planning and execution, accommodating the complex dynamics of scientific research.

## The Need for Asynchrony

Traditional synchronous coordination between data collection and computational analysis imposes a rigid structure on experiments, where each step must wait for computational feedback before proceeding.
This can introduce significant delays, especially if the computational analysis is complex or if the experimental setup requires quick adaptation to changing conditions.
Asynchronous agents break away from this constraint by independently monitoring the data stream and making recommendations or decisions based on predefined criteria.
This model supports continuous data collection without the need for constant pauses, allowing experiments to proceed at their natural pace while still benefiting from intelligent guidance.
It further supports the need for multiple agents to operate concurrently, each focusing on different aspects of the experiment and providing complementary insights.

## Watchdog Agents

One implementation of asynchronous feedback is the use of "watchdog" agents.
These agents continuously assess the quality of the data being collected and make recommendations based on the observed conditions.
For example, if sufficient data has been collected on a sample, the agent might recommend concluding the current data collection phase to avoid unnecessary beamtime use or sample exposure.
Conversely, if the data appears to be of poor quality, the agent might recommend aborting the plan to refocus efforts on more promising directions.

This approach provides a safety net for experiments, ensuring that resources are used efficiently and that data quality is maintained, without slowing down the overall process.

## Passive Agents

Passive agents in the context of experimental orchestration provide a distinct and crucial function, differing from their active counterparts by not directly influencing the course of an experiment.
Instead, they focus on processing and analyzing data as it's collected, offering insights, visualizations, and interpretations that can guide human decision-making or inform future active agent strategies.
By operating in the background, passive agents enrich the experimental ecosystem with valuable insights without interrupting the experimental workflow, thus enabling a more comprehensive and nuanced approach to scientific inquiry.


## Flexible Step Sizes and Human-AI Collaboration

Another aspect of asynchronous feedback involves dynamically adjusting experimental parameters based on ongoing analysis.
For example, an agent might adjust the step size of a scan in real-time based on how "interesting" the current data appears.
This allows for more granular data collection in areas of high interest while speeding through less informative regions, optimizing the scientific yield of the experiment.

Furthermore, this model supports the integration of human insights with automated analysis.
Researchers can input their own observations and adjustments alongside the recommendations of AI agents, fostering a collaborative environment where human expertise and computational power work in concert.

## Distributed Agents and Adjudicators

In complex experimental setups involving multiple agents, including both computational models and human researchers, managing the flow of recommendations and decisions becomes a significant challenge.
Distributed agents, communicating through platforms like Kafka, allow for a more scalable and flexible approach to experiment management.
Adjudicators act as meta-agents, filtering and prioritizing the recommendations from various sources to ensure that the experimental queue remains aligned with overarching goals.
This distributed model supports a wide range of experimental strategies, from closely monitored single-agent operations to complex, multi-agent collaborative efforts.
It accommodates the diverse needs and pace of modern scientific research, enabling a more responsive and intelligent approach to experiment orchestration.

## Distributed Agents Required Stack

For an asynchronous agent or adjudicator to function effectively within the Bluesky Adaptive framework, several key dependencies are included.
These are all areas of active development with ongoing efforts to reduce the complexity of integration and improve the overall user experience.

- **Message Bus (Kafka)**: Utilized for distributed messaging, a message bus (specifically Kafka) facilitates the asynchronous communication between agents, adjudicators, and the `RunEngine`.
It enables the publishing and subscribing of experiment-related messages, allowing agents to react to experiment progress and make decisions independently of the experiment's real-time flow.

- **Tiled**: Serves as a data access and management layer, allowing agents to efficiently query and retrieve experimental data.
Tiled's API supports high-performance access to large datasets, which is essential for agents that analyze data to make recommendations or generate insights.

- **Bluesky-queueserver**: Acts as the manager of the `RunEngine` as a service, managing experiment queues and execution.
It allows for the scheduling and prioritization of experiment plans submitted by agents or adjudicators, ensuring that the experiment progresses according to the optimized strategy identified by the autonomous system.

- **Bluesky-kafka**: Provides integration between Bluesky components and Kafka, streamlining the configuration and management of Kafka producers and consumers within the experimental framework. This ensures that messages related to experiment control and data analysis are efficiently routed between the system's components.
