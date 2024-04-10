# Axes of Adaptive Behavior in Experimental Orchestration

We conceptualize adaptive behavior in experimental orchestration along three distinct axes: rate, degree of signal abstraction, and processing motif.
This classification not only simplifies the understanding of adaptiveness in experiments but also guides the development of tailored adaptive tooling based on specific experimental needs and constraints.

The **rate** at which decisions are made dictates the temporal resolution of adaptiveness in response to data acquisition.
This axis encompasses the entire spectrum from sub-millisecond adjustments necessary in control systems to longer-term decisions that might span hours or days, such as guiding subsequent chemical syntheses based on prior experimental outcomes.
The timescale of feedback is intricately linked to both the computational overhead of processing data and the intrinsic time constraints of the experimental system under investigation.
For instance, a control loop maintaining beam alignment might operate on a sub-millisecond timescale, leveraging fast computations to make real-time adjustments. Conversely, adaptive decisions informing which samples to synthesize next can afford to be deliberated over longer periods, given the relatively slow pace of chemical synthesis.
This decision making rate, ties into the *Bluesky* [document model](https://blueskyproject.io/event-model) with  agents enabled to operate  on a per-event or per-run basis.

The **degree of signal abstraction** refers to the level of scientific interpretation applied to the data being used for feedback.
This axis differentiates between engineering values, such as current or shutter states, which do not require deep scientific context to interpret, and scientific signals that are deeply entwined with the specifics of the experiment being conducted.
For example, a PID loop would be concerned with a setpoint and readback value of an engineering signal, whereas a Bayesian optimization over measurement conditions would need to process those engineering signals into state variables such as temperature or composition.
This distinction is crucial in designing adaptive strategies that are both effective and relevant to the goals of the experiment.

The **processing motif** addresses the nature of the communication between the planning logic and the decision-making engine: whether decisions are made in a synchronous ("in-the-loop") manner or asynchronously.
Synchronous decisions are blocking, necessitating pausing data acquisition to await guidance.
They necessitate pausing data acquisition to await guidance, which, while ensuring that each step is informed by the latest analysis, imposes stringent demands on the speed of decision-making.
This model is uses adaptive plans and factory functions to coordinate queues and callbacks that operate in lockstep with the `RunEngine`.
Asynchronous communication, on the other hand, allows experiments to proceed uninterrupted, with adaptive adjustments being made at opportune moments.
This model deploys agents as services that subscribe to message busses, access databases, and publish suggestions to message busses and/or the `bluesky-queueserver` (which manages the `RunEngine` as a service through the `RunEngineManager`).

Bluesky-adaptive's conceptualization of these axes facilitates a granular approach to integrating adaptiveness into scientific experiments.
By delineating the phase space of adaptiveness along these dimensions, it empowers researchers to tailor their adaptive strategies to the unique requirements and constraints of their experimental setups.
Below is a figure that shows the three conceptual axes of adaptive in experimental orchestration, using color to show processing motif.
Several approaches and application areas for agents are shown as examples, indicating where they may sit on the axes.

![The three conceptual axes of adaptive in experimental orchestration, using color to show processing motif. Several approaches and application areas for agents are shown as examples, indicating where they may sit on the axes.](../images/AxesofAdaptive.pdf)
