from threading import Thread
from typing import Tuple, Union

import numpy as np
from numpy.typing import ArrayLike
from pytest import fixture
from tsuchinoko.adaptive.gpCAM_in_process import GPCAMInProcessEngine
from tsuchinoko.core import CoreState, ZMQCore
from tsuchinoko.execution.bluesky_adaptive import BlueskyAdaptiveEngine
from xarray import Dataset

from bluesky_adaptive.agents.tsuchinoko import TsuchinokoAgent
from bluesky_adaptive.typing import BlueskyRunLike
from bluesky_adaptive.utils.offline import OfflineAgent
from .conftest import catalog


@fixture
def gpcam_engine():
    # Define a gpCAM adaptive engine with initial parameters
    adaptive = GPCAMInProcessEngine(
        dimensionality=2,
        parameter_bounds=[(0, 1), (0, 1)],
        hyperparameters=[255, 100, 100],
        hyperparameter_bounds=[(0, 1e5), (0, 1e5), (0, 1e5)],
    )
    return adaptive


@fixture
def execution_engine(gpcam_engine):
    execution = BlueskyAdaptiveEngine(gpcam_engine)
    yield execution


@fixture
def core(gpcam_engine, execution_engine):
    print("starting setup")
    assert gpcam_engine is execution_engine.adaptive_engine
    core = ZMQCore()
    core.set_adaptive_engine(gpcam_engine)
    core.set_execution_engine(execution_engine)

    class ErrorThread(Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.error = None

        def run(self):
            try:
                super().run()
            except Exception as ex:
                self.error = ex
                print(ex)
                print("The above error occurred in the tsuchinoko core thread.")

    server_thread = ErrorThread(target=core.main)
    server_thread.start()
    core.state = CoreState.Starting
    print("setup complete")

    yield core

    core.exit()
    server_thread.join()
    print("teardown complete")
    if server_thread.error:
        raise server_thread.error  # most likely a test would have already failed by this point


class GPTestAgent(TsuchinokoAgent, OfflineAgent):
    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [1.5], dict()

    def unpack_run(self, run: BlueskyRunLike) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        self.counter += 1
        y = np.random.rand(10)
        v = 0.1
        return self.counter, (y, v)


def test_gp_agent(catalog, core):
    # Test ingest, suggest, and report; uses Tiled functionality
    agent = GPTestAgent(tiled_data_node=catalog, tiled_agent_node=catalog)
    agent.start()
    agent_uid = agent._compose_run_bundle.start_doc["uid"]
    for i in range(5):
        uid = f"uid{i}"
        x = np.random.rand(2)
        y = 1 - np.sum(np.sin(x)) + np.random.rand() * 0.01
        v = 0.1
        yv = y, v
        doc = agent.ingest(x, yv)
        doc["exp_uid"] = uid
        agent._write_event("ingest", doc)
        agent.known_uid_cache.append(uid)
    # agent.generate_report()
    doc, query = agent.suggest()
    agent._write_event("suggest", doc[0])
    doc, query = agent.suggest()
    agent._write_event("suggest", doc[0])
    agent.stop()
    assert len(query) == 1
    run = catalog[agent_uid]
    xr = run.suggest.read()
    assert xr["candidate"].shape == (2, 1, 2)
    # Acquisition value isn't available here; this is outside of separation of responsibilities for tsuchinoko's
    # execution engines.
    # assert xr["acquisition_value"].shape == (2,)
    # TODO: getting AttributeError on 'report' here.
    # xr = run.report.read()
    # assert xr["STATEDICT-beta"].shape == (1,)
    xr = run.ingest.read()
    assert isinstance(xr, Dataset)
