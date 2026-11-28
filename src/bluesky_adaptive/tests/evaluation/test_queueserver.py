import importlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from bluesky.plans import count
from bluesky.run_engine import RunEngine

from bluesky_adaptive.evaluation import per_run, per_suggestion
from bluesky_adaptive.evaluation.documents import RunEventBuffer
from bluesky_adaptive.evaluation.queueserver import QueueItemRuns
from bluesky_adaptive.plans import default_acquire

from ..conftest import MovableSignal, ReadableSignal, re_manager


@pytest.fixture()
def RE():
    return RunEngine({})


@pytest.mark.parametrize("pending", [{"running": "item-2"}, {"queued": ["item-2"]}], ids=["running", "queued"])
def test_per_run_evaluates_the_queue_items_run_from_the_document_stream(RE, pending):
    buffer = RunEventBuffer()
    # Time while the item is queued or running does not count against the timeout.
    runs = QueueItemRuns(re_manager(**pending), timeout=0, poll_interval=0.01)
    RE.subscribe(buffer)
    RE.subscribe(runs)
    x1, det = MovableSignal("x1"), ReadableSignal("det")
    evaluation_function = per_run(per_suggestion(lambda data: {"objective": 10 * data["x1"]}, buffer.pop), runs.resolve)

    with ThreadPoolExecutor(max_workers=1) as executor:
        # The evaluation starts waiting before its item runs, as it does once the runner submits the plan.
        future = executor.submit(evaluation_function, "item-2", [])
        RE(default_acquire([{"x1": 1.0, "_id": "a"}], [x1], [det]), queue_server={"queue_item_uid": "item-1"})
        RE(default_acquire([{"x1": 2.0, "_id": "b"}], [x1], [det]), queue_server={"queue_item_uid": "item-2"})

        assert future.result(timeout=5) == [{"objective": 20.0, "_id": "b"}]


def test_queue_item_runs_selects_the_first_run_of_the_item_that_passes_run_filter(RE):
    det = ReadableSignal("det")

    def dark_then_light():
        yield from count([det], md={"dark_frame": True})
        yield from count([det])

    first_runs = QueueItemRuns(re_manager(running="item-1"))
    light_runs = QueueItemRuns(re_manager(running="item-1"), run_filter=lambda start: not start.get("dark_frame"))
    RE.subscribe(first_runs)
    RE.subscribe(light_runs)

    dark_uid, light_uid = RE(dark_then_light(), queue_server={"queue_item_uid": "item-1"})

    assert first_runs.resolve("item-1") == dark_uid
    assert light_runs.resolve("item-1") == light_uid


def test_queue_item_runs_ignores_runs_not_opened_by_a_queue_item(RE):
    runs = QueueItemRuns(re_manager(running="item-1"))
    RE.subscribe(runs)
    det = ReadableSignal("det")

    RE(count([det]))
    (uid,) = RE(count([det]), queue_server={"queue_item_uid": "item-1"})

    assert runs.resolve("item-1") == uid


def test_queue_item_runs_raises_when_the_run_fails(RE):
    runs = QueueItemRuns(re_manager(running="item-1"))
    RE.subscribe(runs)

    def fail(detectors, step, pos_cache):
        raise RuntimeError("detector fault")
        yield

    with pytest.raises(RuntimeError, match="detector fault"):
        RE(
            default_acquire([{"x1": 1.0, "_id": "a"}], [MovableSignal("x1")], [ReadableSignal("det")], per_step=fail),
            queue_server={"queue_item_uid": "item-1"},
        )

    with pytest.raises(RuntimeError, match="ended with status 'fail'"):
        runs.resolve("item-1")


def test_queue_item_runs_drops_items_whose_runs_started_earlier(RE):
    finished = {"exit_status": "completed", "run_uids": ["run"], "msg": ""}
    runs = QueueItemRuns(re_manager(history=[("item-1", finished), ("item-2", finished)]), timeout=0.01)
    RE.subscribe(runs)
    det = ReadableSignal("det")
    RE(count([det]), queue_server={"queue_item_uid": "item-1"})
    (uid_2,) = RE(count([det]), queue_server={"queue_item_uid": "item-2"})

    assert runs.resolve("item-2") == uid_2
    with pytest.raises(TimeoutError):
        runs.resolve("item-1")


def test_queue_item_runs_waits_for_a_stop_document_that_arrives_after_the_item_finished(RE):
    finished = {"exit_status": "completed", "run_uids": ["run"], "msg": ""}
    re_manager_api = re_manager(history=[("item-1", finished)])
    polled = threading.Event()
    history = re_manager_api.history_get.return_value
    re_manager_api.history_get.side_effect = lambda **kwargs: (polled.set(), history)[1]
    runs = QueueItemRuns(re_manager_api, timeout=5, poll_interval=0.01)
    RE.subscribe(runs)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runs.resolve, "item-1")
        # The RE Manager reports the item finished before its documents reach the dispatcher.
        assert polled.wait(timeout=5)
        (uid,) = RE(count([ReadableSignal("det")]), queue_server={"queue_item_uid": "item-1"})

        assert future.result(timeout=5) == uid


def test_queue_item_runs_raises_at_once_when_the_item_ends_without_a_run():
    # A failed item goes back in the queue under a new UID, and its original UID stays in the history.
    failed = {"exit_status": "failed", "run_uids": [], "msg": "plan raised"}
    runs = QueueItemRuns(re_manager(queued=["item-1-retry"], history=[("item-1", failed)]), timeout=60)

    with pytest.raises(RuntimeError, match="ended with status 'failed' without opening a run: plan raised"):
        runs.resolve("item-1")


def test_queue_item_runs_raises_when_the_re_manager_has_no_record_of_the_item():
    runs = QueueItemRuns(re_manager(), timeout=60)

    with pytest.raises(KeyError, match="removed from the queue"):
        runs.resolve("item-1")


def test_queue_item_runs_raises_run_filter_errors_for_the_item(RE):
    runs = QueueItemRuns(re_manager(running="item-1"), run_filter=lambda start: start["sample"] == "s1")
    RE.subscribe(runs)

    RE(count([ReadableSignal("det")]), queue_server={"queue_item_uid": "item-1"})

    with pytest.raises(RuntimeError, match="run_filter raised") as excinfo:
        runs.resolve("item-1")
    assert isinstance(excinfo.value.__cause__, KeyError)


@pytest.mark.parametrize("settings", [{"poll_interval": 0}, {"timeout": -1}])
def test_queue_item_runs_rejects_invalid_polling_settings(settings):
    with pytest.raises(ValueError):
        QueueItemRuns(re_manager(), **settings)


def test_queue_item_runs_import_names_the_queueserver_extra_when_the_api_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "bluesky_queueserver_api.http", None)
    monkeypatch.delitem(sys.modules, "bluesky_adaptive.evaluation.queueserver")

    with pytest.raises(ImportError, match=r"pip install bluesky-adaptive\[queueserver\]"):
        importlib.import_module("bluesky_adaptive.evaluation.queueserver")
