import copy
import importlib
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import bluesky_adaptive.evaluation.tiled as tiled_evaluation
from bluesky_adaptive.evaluation.tiled import TiledQueueItemRuns, TiledRunPoller

from ..conftest import re_manager


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


def run_metadata(item_uid, uid, start_time, *, exit_status="success", reason="", **start):
    """Mimic the metadata TiledWriter gives a run opened by a queue item: "start", and "stop" once it stopped."""
    metadata = {"start": {"uid": uid, "time": start_time, "queue_server": {"queue_item_uid": item_uid}, **start}}
    if exit_status is not None:
        metadata["stop"] = {"run_start": uid, "exit_status": exit_status, "reason": reason}
    return metadata


class TiledRuns(dict):
    """Mimic a Tiled container of run metadata keyed by run UID."""

    def search(self, query):
        """Return snapshots of the runs whose metadata at the query's dotted key equals its value."""

        def lookup(metadata):
            for key in query.key.split("."):
                metadata = metadata.get(key, {})
            return metadata

        return {uid: SimpleNamespace(metadata=copy.deepcopy(md)) for uid, md in self.items() if lookup(md) == query.value}


def test_tiled_queue_item_runs_selects_the_first_run_in_start_order_that_passes_run_filter():
    # Tiled lists the item's light run before its dark run, which started first.
    client = TiledRuns(
        light=run_metadata("item-1", "light", 2.0),
        dark=run_metadata("item-1", "dark", 1.0, dark_frame=True),
        other=run_metadata("item-2", "other", 0.0),
    )
    finished = {"exit_status": "completed", "run_uids": ["dark", "light"], "msg": ""}
    re_manager_api = re_manager(history=[("item-1", finished)])
    light_runs = TiledQueueItemRuns(re_manager_api, client, run_filter=lambda start: not start.get("dark_frame"))

    assert TiledQueueItemRuns(re_manager_api, client).resolve("item-1") == "dark"
    assert light_runs.resolve("item-1") == "light"


@pytest.mark.parametrize("pending", [{"running": "item-1"}, {"queued": ["item-1"]}], ids=["running", "queued"])
def test_tiled_queue_item_runs_waits_without_a_time_limit_while_the_item_is_pending(monkeypatch, pending):
    client = TiledRuns()
    # Between polls the writer catches up: the item's dark run reaches Tiled, then its light run.
    writes = iter([run_metadata("item-1", "dark", 1.0, dark_frame=True), run_metadata("item-1", "light", 2.0)])

    def write(seconds):
        metadata = next(writes)
        client[metadata["start"]["uid"]] = metadata

    monkeypatch.setattr(tiled_evaluation, "time", SimpleNamespace(monotonic=time.monotonic, sleep=write))
    filtered = []

    def run_filter(start):
        filtered.append(start["uid"])
        return not start.get("dark_frame")

    # Time while the item is queued or running does not count against the timeout.
    runs = TiledQueueItemRuns(re_manager(**pending), client, run_filter=run_filter, timeout=0)

    assert runs.resolve("item-1") == "light"
    assert filtered == ["dark", "light"]


def test_tiled_queue_item_runs_waits_for_a_stop_document_that_arrives_after_the_item_finished(monkeypatch):
    client = TiledRuns(run=run_metadata("item-1", "run", 1.0, exit_status=None))
    finished = {"exit_status": "completed", "run_uids": ["run"], "msg": ""}
    re_manager_api = re_manager(history=[("item-1", finished)])

    def write_stop(seconds):
        # The RE Manager reports the item finished before TiledWriter writes its run's stop document.
        assert re_manager_api.history_get.called
        client["run"] = run_metadata("item-1", "run", 1.0)

    monkeypatch.setattr(tiled_evaluation, "time", SimpleNamespace(monotonic=time.monotonic, sleep=write_stop))

    assert TiledQueueItemRuns(re_manager_api, client, timeout=5).resolve("item-1") == "run"


def test_tiled_queue_item_runs_waits_for_runs_of_the_finished_item_that_have_not_reached_tiled(monkeypatch):
    client = TiledRuns(dark=run_metadata("item-1", "dark", 1.0, dark_frame=True))
    finished = {"exit_status": "completed", "run_uids": ["dark", "light"], "msg": ""}

    def write_light(seconds):
        client["light"] = run_metadata("item-1", "light", 2.0)

    monkeypatch.setattr(tiled_evaluation, "time", SimpleNamespace(monotonic=time.monotonic, sleep=write_light))
    runs = TiledQueueItemRuns(
        re_manager(history=[("item-1", finished)]), client, run_filter=lambda start: not start.get("dark_frame"), timeout=5
    )

    assert runs.resolve("item-1") == "light"


def test_tiled_queue_item_runs_raises_when_the_re_manager_has_no_record_of_the_item():
    runs = TiledQueueItemRuns(re_manager(), TiledRuns(), timeout=60)

    with pytest.raises(KeyError, match="removed from the queue"):
        runs.resolve("item-1")


def test_tiled_queue_item_runs_raises_at_once_when_the_item_ends_without_a_run():
    # A failed item goes back in the queue under a new UID, and its original UID stays in the history.
    failed = {"exit_status": "failed", "run_uids": [], "msg": "plan raised"}
    runs = TiledQueueItemRuns(re_manager(queued=["item-1-retry"], history=[("item-1", failed)]), TiledRuns(), timeout=60)

    with pytest.raises(RuntimeError, match="ended with status 'failed' without opening a run: plan raised"):
        runs.resolve("item-1")


def test_tiled_queue_item_runs_raises_run_filter_errors_for_the_item():
    client = TiledRuns(run=run_metadata("item-1", "run", 1.0))
    runs = TiledQueueItemRuns(re_manager(running="item-1"), client, run_filter=lambda start: start["sample"] == "s1")

    with pytest.raises(RuntimeError, match="run_filter raised") as excinfo:
        runs.resolve("item-1")
    assert isinstance(excinfo.value.__cause__, KeyError)


def test_tiled_queue_item_runs_raises_at_once_when_no_run_of_the_finished_item_passes_run_filter():
    client = TiledRuns(dark=run_metadata("item-1", "dark", 1.0, dark_frame=True))
    finished = {"exit_status": "completed", "run_uids": ["dark"], "msg": ""}
    runs = TiledQueueItemRuns(
        re_manager(history=[("item-1", finished)]), client, run_filter=lambda start: not start.get("dark_frame"), timeout=60
    )

    with pytest.raises(RuntimeError, match="none of its runs"):
        runs.resolve("item-1")


def test_tiled_queue_item_runs_raises_when_the_run_fails():
    client = TiledRuns(run=run_metadata("item-1", "run", 1.0, exit_status="fail", reason="detector fault"))
    runs = TiledQueueItemRuns(re_manager(running="item-1"), client)

    with pytest.raises(RuntimeError, match="ended with status 'fail': detector fault"):
        runs.resolve("item-1")


def test_tiled_queue_item_runs_times_out_while_the_finished_items_run_has_no_stop_document():
    client = TiledRuns(run=run_metadata("item-1", "run", 1.0, exit_status=None))
    finished = {"exit_status": "completed", "run_uids": ["run"], "msg": ""}
    runs = TiledQueueItemRuns(re_manager(history=[("item-1", finished)]), client, timeout=0, poll_interval=0.01)

    with pytest.raises(TimeoutError):
        runs.resolve("item-1")


@pytest.mark.parametrize("settings", [{"poll_interval": 0}, {"timeout": -1}])
def test_tiled_queue_item_runs_rejects_invalid_polling_settings(settings):
    with pytest.raises(ValueError):
        TiledQueueItemRuns(re_manager(), TiledRuns(), **settings)


def test_tiled_queue_item_runs_resolves_without_the_queueserver_api(monkeypatch):
    # TiledRunPoller users may install only the tiled extra, so the module must import without the queueserver API.
    package = sys.modules["bluesky_adaptive.evaluation"]
    for name in ("tiled", "_queue_items"):
        # The re-import rebinds the package attribute; setattr restores it afterwards.
        monkeypatch.setattr(package, name, sys.modules[f"bluesky_adaptive.evaluation.{name}"])
        monkeypatch.delitem(sys.modules, f"bluesky_adaptive.evaluation.{name}")
    monkeypatch.setitem(sys.modules, "bluesky_queueserver_api", None)

    module = importlib.import_module("bluesky_adaptive.evaluation.tiled")

    finished = {"exit_status": "completed", "run_uids": ["run"], "msg": ""}
    runs = module.TiledQueueItemRuns(
        re_manager(history=[("item-1", finished)]), TiledRuns(run=run_metadata("item-1", "run", 1.0))
    )
    assert runs.resolve("item-1") == "run"
