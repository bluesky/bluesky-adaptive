import importlib
import sys
import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from bluesky_adaptive.evaluation.tiled import TiledRunPoller


def tiled_run(acquisition_order: list[str], x1: list[float], *, stopped: bool = True) -> MagicMock:
    """Mimic the BlueskyRun client that Tiled serves for a default_acquire run."""
    run = MagicMock()
    run.metadata = {"start": {"bluesky-adaptive": {"acquisition_order": acquisition_order}}}
    if stopped:
        run.metadata["stop"] = {"exit_status": "success"}
    primary = run["primary"]
    primary.metadata = {"data_keys": {"x1": {"source": "x1", "dtype": "number", "shape": []}}}
    primary["x1"].read.return_value = np.array(x1)
    return run


def test_tiled_run_poller_waits_for_the_run_to_stop(monkeypatch):
    client = {}
    # Between polls the writer catches up: the run appears mid-acquisition, then stops.
    writes = iter([tiled_run(["near", "far"], [0.5], stopped=False), tiled_run(["near", "far"], [0.5, 5.0])])
    monkeypatch.setattr(time, "sleep", lambda seconds: client.update(uid=next(writes)))

    assert TiledRunPoller(client)("uid") == {"near": {"x1": 0.5}, "far": {"x1": 5.0}}


def test_tiled_run_poller_times_out_while_the_run_is_open():
    poller = TiledRunPoller({"uid": tiled_run(["near"], [0.5], stopped=False)}, poll_interval=0.01, timeout=0.05)

    with pytest.raises(TimeoutError):
        poller("uid")


def test_tiled_run_poller_rejects_rows_that_do_not_match_the_acquisition_order():
    poller = TiledRunPoller({"uid": tiled_run(["near", "far"], [0.5])})

    with pytest.raises(ValueError):
        poller("uid")


def test_tiled_run_poller_rejects_runs_without_an_acquisition_order():
    run = tiled_run(["near"], [0.5])
    # Plans other than default_acquire, e.g. bluesky.plans.scan, do not record the acquisition order.
    run.metadata["start"] = {"plan_name": "scan"}

    with pytest.raises(KeyError):
        TiledRunPoller({"uid": run})("uid")


@pytest.mark.parametrize("settings", [{"poll_interval": 0}, {"timeout": -1}])
def test_tiled_run_poller_rejects_invalid_polling_settings(settings):
    with pytest.raises(ValueError):
        TiledRunPoller({}, **settings)


def test_tiled_run_poller_import_names_the_tiled_extra_when_tiled_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "tiled.client.container", None)
    monkeypatch.delitem(sys.modules, "bluesky_adaptive.evaluation.tiled")

    with pytest.raises(ImportError, match=r"pip install bluesky-adaptive\[tiled\]"):
        importlib.import_module("bluesky_adaptive.evaluation.tiled")
