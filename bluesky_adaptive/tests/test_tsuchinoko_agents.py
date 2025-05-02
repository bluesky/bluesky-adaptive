import numpy as np
import numpy as np
import pytest
import torch
from PIL import Image
from bluesky.plan_stubs import checkpoint, mov, trigger_and_read
from loguru import logger
from numpy.typing import ArrayLike
from ophyd import Device
from ophyd.sim import SynAxis, SynSignal, Cpt
from pathlib import Path
from pytest import fixture
from pytest_lazyfixture import lazy_fixture
from scipy import ndimage, datasets
from threading import Thread
from tsuchinoko.adaptive.gpCAM_in_process import GPCAMInProcessEngine
from tsuchinoko.adaptive.random_in_process import RandomInProcess
from tsuchinoko.core import CoreState, ZMQCore
from tsuchinoko.execution.bluesky_in_process import BlueskyInProcessEngine
from tsuchinoko.execution.simple import SimpleEngine
from tsuchinoko.execution.threaded_in_process import ThreadedInProcessEngine
from tsuchinoko.utils.runengine import get_run_engine
from typing import Tuple, Union
from xarray import Dataset

from bluesky_adaptive.agents.tsuchinoko import GPCamTsuchinokoAgent
from bluesky_adaptive.utils.offline import OfflineAgent
from ..typing import BlueskyRunLike


# Disable logging to console when running tests
# NOTE: it seems there is a bug between loguru and pytest; pytest tries to log to a tempfile, but closes it when finished
# NOTE: if loguru has a backlog of messages
# logger.remove()


@fixture
def image_data():
    # Load data from scipy to be used as a luminosity map
    image = datasets.face(gray=True)
    return image


@fixture
def image_func(image_data):
    # Bilinear sampling will be used to effectively smooth pixel edges in source data
    def bilinear_sample(pos):
        return pos, ndimage.map_coordinates(image_data, [[pos[1]], [pos[0]]], order=1)[0], 1, {}

    return bilinear_sample


@fixture
def simple_execution_engine(image_func):
    execution = SimpleEngine(measure_func=image_func)
    return execution


@fixture
def gpcam_engine(image_data):
    # Define a gpCAM adaptive engine with initial parameters
    adaptive = GPCAMInProcessEngine(dimensionality=2,
                                    parameter_bounds=[(0, image_data.shape[1]),
                                                      (0, image_data.shape[0])],
                                    hyperparameters=[255, 100, 100],
                                    hyperparameter_bounds=[(0, 1e5),
                                                           (0, 1e5),
                                                           (0, 1e5)])
    return adaptive


@fixture
def threaded_execution_engine(image_func):
    execution = ThreadedInProcessEngine(image_func)
    yield execution
    execution.exiting = True
    execution.measure_thread.join()

@fixture
def core(gpcam_engine, threaded_execution_engine):
    adaptive_engine, execution_engine = gpcam_engine, threaded_execution_engine
    logger.info('starting setup')
    core = ZMQCore()
    core.set_adaptive_engine(adaptive_engine)
    core.set_execution_engine(execution_engine)
    server_thread = Thread(target=core.main)
    server_thread.start()
    core.state = CoreState.Starting
    logger.info('setup complete')

    yield core

    core.exit()
    server_thread.join()
    logger.info('teardown complete')


class GPTestAgent(GPCamTsuchinokoAgent, OfflineAgent):
    def measurement_plan(self, point: ArrayLike) -> Tuple[str, list, dict]:
        return self.measurement_plan_name, [1.5], dict()

    def unpack_run(self, run: BlueskyRunLike) -> Tuple[Union[float, ArrayLike], Union[float, ArrayLike]]:
        self.counter += 1
        return self.counter, np.random.rand(10)


def test_gp_agent(catalog, core):
    # Test ingest, suggest, and report; uses Tiled functionality
    bounds = np.array([[0, 1], [0, 1]])
    agent = GPTestAgent(bounds=bounds, tiled_data_node=catalog, tiled_agent_node=catalog)
    agent.start()
    agent_uid = agent._compose_run_bundle.start_doc["uid"]
    for i in range(5):
        uid = f"uid{i}"
        x = np.random.rand(2)
        y = 1 - np.sum(np.sin(x)) + np.random.rand() * 0.01
        doc = agent.ingest(x, y)
        doc["exp_uid"] = uid
        agent._write_event("ingest", doc)
        agent.known_uid_cache.append(uid)
    agent.generate_report()
    doc, query = agent.suggest()
    agent._write_event("suggest", doc[0])
    doc, query = agent.suggest()
    agent._write_event("suggest", doc[0])
    agent.stop()
    assert len(query) == 1
    run = catalog[agent_uid]
    xr = run.suggest.read()
    assert xr["candidate"].shape == (2, 1, 2)
    assert xr["acquisition_value"].shape == (2,)
    xr = run.report.read()
    assert xr["STATEDICT-beta"].shape == (1,)
    xr = run.ingest.read()
    assert isinstance(xr, Dataset)


def test_remodel_from_report(catalog):
    bounds = torch.Tensor([(0, 1), (0, 1)])
    agent = GPTestAgent(bounds=bounds, tiled_data_node=catalog, tiled_agent_node=catalog)
    agent.start()
    agent_uid = agent._compose_run_bundle.start_doc["uid"]
    for i in range(5):
        uid = f"uid{i}"
        x = np.random.rand(2)
        y = 1 - np.sum(np.sin(x)) + np.random.rand() * 0.01
        doc = agent.ingest(x, y)
        doc["exp_uid"] = uid
        agent._write_event("ingest", doc)
        agent.known_uid_cache.append(uid)
    agent.generate_report()
    agent.stop()

    new_agent = GPTestAgent(
        bounds=bounds,
        partial_acq_function=lambda gp: UpperConfidenceBound(gp, beta=1.5),
    )
    acqf, gp = new_agent.remodel_from_report(catalog[agent_uid])
    try:
        param = gp.get_parameter("covar_module.base_kernel.raw_lengthscale")
        assert (param == agent.surrogate_model.get_parameter("covar_module.base_kernel.raw_lengthscale")).all()
    except AttributeError:
        param = gp.get_parameter("covar_module.raw_lengthscale")
        assert (param == agent.surrogate_model.get_parameter("covar_module.raw_lengthscale")).all()
    assert acqf.beta == 0.1
