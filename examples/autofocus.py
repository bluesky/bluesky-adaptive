import functools
from collections.abc import Sequence, Mapping

import numpy as np
from bluesky.run_engine import RunEngine
from bluesky.utils import MsgGenerator
import bluesky.plans as bp
import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from bluesky.utils import short_uid
from ophyd_async.core import init_devices
from ophyd_async.sim import SimPointDetector, SimMotor, PatternGenerator
from tiled.client import simple
from bluesky_tiled_plugins.writing.tiled_writer import TiledWriter

from bluesky_adaptive.ax import RangeDOF, Objective, Agent
from bluesky_adaptive.plans import default_acquire
from bluesky_adaptive.protocols import EvaluationFunction


RE = RunEngine({})
tiled_client = simple()
tw = TiledWriter(tiled_client)
RE.subscribe(tw)

with init_devices():
    pitch = SimMotor()
    normal = SimMotor()
    vertical = SimMotor()
    horizontal = SimMotor()
    i0 = SimPointDetector(PatternGenerator())
    it = SimPointDetector(PatternGenerator())


class ProfileEvaluation(EvaluationFunction):
    def __init__(self, tiled_client) -> None:
        self.tiled_client = tiled_client

    def _compute_neg_log_peak_intensity(self, *args):
        return -0.5

    def _compute_fwhm(self, *args):
        return 0.2

    def __call__(self, uid: str, suggestions: Sequence[Mapping]) -> Sequence[Mapping]:
        # Should poll w/ retries in case there is a data race here
        run = self.tiled_client[uid]
        acquisition_order = run.metadata["start"]["blop_acquisition_order"]
        i0_array = run["primary/i0-channel-1-value"].read()
        it_array = run["primary/it-channel-1-value"].read()

        outcomes = []
        for index, suggestion_id in enumerate(acquisition_order):
            i0_vslice = i0_array[index * 10 : index * 10 + 10]
            it_vslice = it_array[index * 10 : index * 10 + 10]
            i0_hslice = i0_array[index * 10 + 10 : index * 10 + 20]
            it_hslice = it_array[index * 10 + 10 : index * 10 + 20]
            outcomes.append(
                {
                    "_id": suggestion_id,
                    "neg_log_peak_intensity": self._compute_neg_log_peak_intensity(
                        i0_hslice, i0_vslice, it_hslice, it_vslice
                    ),
                    "fwhm": self._compute_fwhm(i0_hslice, i0_vslice, it_hslice, it_vslice),
                }
            )

        return outcomes


def main_plan() -> MsgGenerator[None]:

    pitch_ref = yield from bps.rd(pitch.user_readback)
    normal_ref = yield from bps.rd(normal.user_readback)
    vertical_ref = yield from bps.rd(vertical.user_readback)
    horizontal_ref = yield from bps.rd(horizontal.user_readback)

    dofs = [
        RangeDOF(actuator=pitch,  bounds=(pitch_ref - 0.05, pitch_ref + 0.05), parameter_type="float"),
        RangeDOF(actuator=normal, bounds=(normal_ref - 50, normal_ref + 50), parameter_type="float"),
    ]
    sensors = [
        i0,
        it,
    ]
    objectives = [
        Objective(name="neg_log_peak_intensity", minimize=True),
        Objective(name="fwhm", minimize=True),
    ]
    first_trial = True

    def _per_step(detectors, step, pos_cache):
        pitch0 = yield from bps.rd(pitch.user_readback)
        normal0 = yield from bps.rd(normal.user_readback)
        pitch1 = step[pitch]
        normal1 = step[normal]

        def _dof_move():
            yield from bps.checkpoint()
            grp = short_uid("set")
            for motor, pos in step.items():
                if pos == pos_cache[motor]:
                    # This step does not move this motor.
                    continue
                yield from bps.abs_set(motor, pos, group=grp)
                pos_cache[motor] = pos
            yield from bps.wait(group=grp)

        def _scan_window(pitch0, normal0, pitch1, normal1):
            nonlocal first_trial
            if first_trial:
                first_trial = False
                return (vertical_ref - 5, vertical_ref + 5), (horizontal_ref - 5, horizontal_ref + 5)
            return (1, 20), (5, 40)

        def _scan_move():
            v0 = yield from bps.rd(vertical.user_readback)
            h0 = yield from bps.rd(horizontal.user_readback)
            window = _scan_window(pitch0, normal0, pitch1, normal1)

            vrange = np.linspace(*window[0], 10)
            hrange = np.linspace(*window[1], 10)
            vrange = list(np.concatenate((vrange, np.repeat(v0, 10)), axis=0))
            hrange = list(np.concatenate((np.repeat(h0, 10), hrange), axis=0))
            yield from bpp.stub_wrapper(bp.list_scan(detectors, vertical, vrange, horizontal, hrange))

        yield from _dof_move()
        yield from _scan_move()

    acquisition_plan = functools.partial(default_acquire, per_step=_per_step)

    agent = Agent(
        sensors=sensors,
        dofs=dofs,
        objectives=objectives,
        evaluation_function=ProfileEvaluation(tiled_client),
        acquisition_plan=acquisition_plan,
    )
    agent.ax_client.configure_generation_strategy(
        initialization_budget=10,
    )

    yield from agent.optimize(1, n_points=10)
    yield from agent.optimize(5)
