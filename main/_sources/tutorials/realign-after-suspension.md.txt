---
jupytext:
  text_representation:
    extension: .md
    format_name: myst
    format_version: 0.13
    jupytext_version: 1.17.3
kernelspec:
  display_name: dev
  language: python
  name: python3
---

# Realign before resuming a suspended run

A Bluesky suspender can pause a measurement when the beam becomes unavailable.
Use its `post_plan` to run a short optimization after beam recovery and before the original plan resumes.
This example uses simulated devices and an alignment error with a known minimum.

```{code-cell} ipython3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import numpy as np
from bluesky import RunEngine
from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from bluesky.suspenders import SuspendFloor
from ophyd import Signal
from ophyd.sim import SynAxis, SynSignal

from bluesky_adaptive.scipy import Objective, RangeDOF, Scipy

RE = RunEngine({})
alignment = SynAxis(name="alignment")
beam_available = Signal(name="beam_available", value=1)
error = SynSignal(func=lambda: (alignment.position - 0.75) ** 2, name="alignment_error")
```

## Define the recovery plan

The acquisition plan moves to one proposed position and reads the simulated alignment error.
It returns the reading directly, so the evaluation function can associate it with the suggestion without a data server or an additional measurement run.

```{code-cell} ipython3
def measure_error(suggestions, actuators, sensors):
    (point,) = suggestions
    (motor,) = actuators
    (detector,) = sensors
    yield from bps.mv(motor, point[motor.name])
    yield from bps.trigger(detector, wait=True)
    return (yield from bps.rd(detector))


def realign():
    measured = []

    def evaluate_error(value, suggestions):
        (point,) = suggestions
        measured.append((value, point[alignment.name]))
        return [{"_id": point["_id"], "alignment_error": value}]

    agent = Scipy.Agent(
        sensors=[error],
        dofs=[RangeDOF(actuator=alignment, bounds=(-2, 2), parameter_type="float")],
        objectives=[Objective(name="alignment_error", minimize=True)],
        evaluation_function=evaluate_error,
        acquisition_plan=measure_error,
    )
    yield from agent.optimize(iterations=20)
    _, best_position = min(measured)
    yield from bps.mv(alignment, best_position)
```

Pass the plan function to `post_plan` so each suspension gets a fresh optimization.
The suspender waits for the beam monitor to recover before it calls `realign`.
In this example, the `pre_plan` only tells the simulated beam monitor that the engine has suspended.

```{code-cell} ipython3
suspended = Event()


def acknowledge_suspension():
    suspended.set()
    yield from bps.null()


suspender = SuspendFloor(
    beam_available,
    0.5,
    pre_plan=acknowledge_suspension,
    post_plan=realign,
)


def restore_simulated_beam():
    try:
        if not suspended.wait(timeout=10):
            raise RuntimeError("The simulated suspension did not start")
    finally:
        beam_available.put(1)
```

## Suspend and resume a measurement

The first event captures the original motor position.
A checkpoint after that read lets the RunEngine resume without replaying it.
The plan then trips the simulated monitor, and a separate thread restores it once the engine has suspended.

```{code-cell} ipython3
@bpp.run_decorator()
def measurement():
    yield from bps.trigger_and_read([alignment])
    yield from bps.checkpoint()
    beam_available.put(0)
    yield from bps.sleep(0.05)
    yield from bps.trigger_and_read([alignment])


positions = []
subscription = RE.subscribe(lambda name, doc: positions.append(doc["data"]["alignment"]), "event")
RE.install_suspender(suspender)
try:
    with ThreadPoolExecutor(max_workers=1) as pool:
        recovery = pool.submit(restore_simulated_beam)
        RE(measurement())
        recovery.result()
finally:
    RE.remove_suspender(suspender)
    RE.unsubscribe(subscription)

np.testing.assert_allclose(positions, [0, 0.75], atol=1e-3)
print(f"Alignment moved from {positions[0]:.2f} to {positions[1]:.2f} before the measurement resumed.")
```

For a real experiment, let the facility's beam monitor drive the suspender and choose actuator bounds suitable for the equipment.
