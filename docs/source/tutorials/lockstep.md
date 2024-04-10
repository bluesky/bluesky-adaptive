# Lockstep Agent Tutorial

This tutorial depends on starting the Bluesky stack in a pod as shown in [Getting Started Tutorial](getting-started.md).
The tutorial covers:
- [Lockstep Agent Tutorial](#lockstep-agent-tutorial)
  - [Start BSUI](#start-bsui)
  - [Explore the devices available](#explore-the-devices-available)
  - [Standard Plan](#standard-plan)
  - [Inspecting the data model](#inspecting-the-data-model)
  - [Building a Lockstep Agent](#building-a-lockstep-agent)
  - [Adding comlpexity with multiple signals](#adding-comlpexity-with-multiple-signals)


## Start BSUI
Firstly, we need to start an ipython session in the same environment as our experiment stack. 
Assuming you have cloned the bluesky-pods repository and started the pod as shown in the [Getting Started Tutorial](getting-started.md), we can start the BSUI by running the following command in the same terminal as the pod:

```bash
cd bluesky-pods/compose/acq-pod
bash launch_bluesky.sh
```

## Explore the devices available
```python
get_ipython().user_ns.keys()
```
Will list a set of python objects that are available in the current session.
Some of these are directly from the Bluesky tutorials, such as `RE`, `motor`, `random_walk`.
We can inspect the devices of interest by just typing their name in the ipython session.
```python
In [1]: RE
Out[1]: <bluesky.run_engine.RunEngine at 0xffff95c10200>

In [2]: motor
Out[2]: SynAxisNoPosition(prefix='', name='motor', read_attrs=['readback', 'setpoint'], configuration_attrs=['velocity', 'acceleration'])

In [3]: random_walk
Out[3]: RandomWalk(prefix='random_walk:', name='random_walk', read_attrs=['x'], configuration_attrs=['dt'])

In [4]: det
Out[5]: DetWithCountTime(prefix='', name='det', read_attrs=['intensity', 'count_time'], configuration_attrs=[])
```

## Standard Plan

We can start by using these devices and our `RunEngine` to run a standard plan.

```python
RE(scan([det], motor, -1, 1, 10))
```

This wasn't very interesting, since this detector only produces a fixed value.
Let's grab a random detector from Ophyd Simulated Devices with multiple signals.

```python
from ophyd.sim import ABDetector
ab_det = ABDetector(name="ab_det")
ab_det.read() # To get a feeling for the signals available
RE(scan([ab_det], motor, -1, 1, 10))
```
If you have X11 forwarding enabled, you should see a plot of the random data produced by the scan. (The "ab_det_a" data against the motor value since only the "a" signal is "hinted").

## Inspecting the data model
A lockstep agent needs to be aware of the keys that it will be receiving from the detector.
Without diving into the bluesky document model, we can infer this information from the data at storage time. 
Let's run a fresh scan inspect our most recent `BlueskyRun`. 

```python
RE(scan([ab_det], motor, -1, 1, 10))
run = db[-1]
run.primary.read()

> <xarray.Dataset> Size: 400B
> Dimensions:         (time: 10)
> Coordinates:
>   * time            (time) float64 80B 1.712e+09 1.712e+09 ... 1.712e+09
> Data variables:
>     motor           (time) float64 80B -1.0 -0.7778 -0.5556 ... 0.7778 1.0
>     motor_setpoint  (time) float64 80B -1.0 -0.7778 -0.5556 ... 0.7778 1.0
>     ab_det_a        (time) float64 80B 0.4206 0.2793 0.8471 ... 0.695 0.6514
>     ab_det_b        (time) float64 80B 0.1539 0.8972 0.4957 ... 0.3888 0.05702
```

Assuming our independent variable is our motor position, we can see it is keyed by `'motor'`.
Let's use the 'a' signal of our detector as our dependent variable, keyed by `'ab_det_a'`.

## Building a Lockstep Agent
Now we can build a reccomendation agent that will suggest the next motor position based on the most recent data.
For this initial application we will build a per-event agent that suggests either -1 or 1 based on the most recent data.
Since our detector is random, we will move to -1 if the most recent detector signal was less than 0.5, and to 1 otherwise.

```python
class Agent:
    def __init__(self):
        self.last_value = None
    
    def tell(self, x, y):
        self.last_value = y

    def tell_many(self, xs, ys):
        for x, y in zip(xs, ys):
            self.tell(x, y)

    def ask(self, batch_size=1):
        if self.last_value is None:
            return 0
        return [-1] if self.last_value < 0.5 else [1]
agent = Agent()
```

We then need to build this agent into an adaptive plan. We give this a timeout of five measurements, and run the plan.

```python
from bluesky_adaptive.per_event import adaptive_plan, recommender_factory
recommender, queue = recommender_factory(agent, independent_keys=["motor"], dependent_keys=["ab_det_a"], max_count=5)
plan = adaptive_plan([ab_det], {motor: 0.0}, to_recommender=recommender, from_recommender=queue)
RE(plan)
```

Your terminal output should be a LiveTable that shows the motor position and the 'a' signal of the detector.
If the previous value of 'a' was less than 0.5, the motor should move to -1 in the next step, and to 1 otherwise. 
The LivePlot should hold a series of zig-zags as the motor moves back and forth between -1 and 1.


## Adding comlpexity with multiple signals

This agent is very simple, and only uses the 'a' signal of the detector.
Let's add another independent variable, and another dependent variable to our agent.
First we can do a deterministic scan over the `motor` and `motor3` (using `motor3` for the Hinted ophyd object). 
We'll also make the `ab_det_b` signal hinted so it shows up in our plots and tables. 

```python
from bluesky.plans import grid_scan
from ophyd import Kind
ab_det.b.kind = Kind.hinted
RE(grid_scan([ab_det], motor, -1, 1, 5, motor3, -1, 1, 5))
```

This will produce a grid scan and randomly colored heat map in the LivePlot.
Now we can use an agent that expects the `motor` and `motor3` signals, and suggests the next `motor` and `motor3` position based on the `ab_det_a` and `ab_det_b` signals.
In this context x and y will be arrays shaped (2,), and the next suggestion will be an array shaped (2,).

```python
from numpy.random import uniform
class Agent2D:
    def __init__(self):
        self.last_values = None
    
    def tell(self, x, y):
        self.last_values = y

    def tell_many(self, xs, ys):
        for x, y in zip(xs, ys):
            self.tell(x, y)

    def ask(self, batch_size=1):
        if self.last_values is None:
            raise RuntimeError("Agent should be primed with first plan")   
        if self.last_values[0] > self.last_values[1]:
            next_point = [uniform(-1, 0), uniform(-1, 0)]
        else:
            next_point = [uniform(0, 1), uniform(0, 1)]
        return next_point
agent2d = Agent2D()
```

This agent will suggest a random point in the lower left quadrant if the last 'a' signal was greater than the last 'b' signal, and a random point in the upper right quadrant otherwise.
Building the reccomender and running the plan is almost the same as before, but with more specified keys.

```python
recommender, queue = recommender_factory(agent2d, independent_keys=["motor", "motor3"], dependent_keys=["ab_det_a", "ab_det_b"], max_count=20)
plan = adaptive_plan([ab_det], {motor: -1.0, motor3: -1.0}, to_recommender=recommender, from_recommender=queue)
RE(plan)
```

This should give a LiveTable with strictly positive or negative motor positions, and a LivePlot that shows the motor positions in the upper right and lower left quadrants. 

