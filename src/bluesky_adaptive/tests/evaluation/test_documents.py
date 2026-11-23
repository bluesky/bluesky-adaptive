from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from bluesky import SupplementalData
from bluesky.plan_stubs import one_nd_step
from bluesky.plans import count
from bluesky.run_engine import RunEngine

from bluesky_adaptive.evaluation import per_run, per_suggestion
from bluesky_adaptive.evaluation.documents import QueueItemRuns, RunEventBuffer
from bluesky_adaptive.plans import default_acquire, optimize
from bluesky_adaptive.protocols import OptimizationProblem, Optimizer

from ..conftest import MovableSignal, ReadableSignal


@pytest.fixture()
def RE():
    return RunEngine({})


def test_per_suggestion_scores_each_suggestion_from_its_primary_event(RE):
    buffer = RunEventBuffer()
    RE.subscribe(buffer)
    # Baseline readings add a second stream to every run, interleaved with the primary events.
    RE.preprocessors.append(SupplementalData(baseline=[ReadableSignal("temperature")]))
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 5.0, "_id": "far"}, {"x1": 0.5, "_id": "near"}]
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=0.0)],
        sensors=[ReadableSignal("det")],
        evaluation_function=per_suggestion(lambda data: {"objective": 10 * data["x1"]}, buffer.pop),
    )

    RE(optimize(problem, iterations=2, n_points=2))

    # Routing acquires "near" first from x1=0.0, then "far" first from x1=5.0.
    assert [call.args[0] for call in optimizer.ingest.call_args_list] == [
        [{"objective": 5.0, "_id": "near"}, {"objective": 50.0, "_id": "far"}],
        [{"objective": 50.0, "_id": "far"}, {"objective": 5.0, "_id": "near"}],
    ]


def test_run_event_buffer_pop_consumes_the_run(RE):
    buffer = RunEventBuffer()
    RE.subscribe(buffer)
    (uid,) = RE(default_acquire([{"x1": 1.0, "_id": 0}], [MovableSignal("x1")], [ReadableSignal("det")]))

    assert buffer.pop(uid) == {0: {"x1": 1.0, "det": 0.0}}
    with pytest.raises(KeyError):
        buffer.pop(uid)


def test_run_event_buffer_rejects_more_than_one_event_per_suggestion(RE):
    buffer = RunEventBuffer()
    RE.subscribe(buffer)

    def read_twice(detectors, step, pos_cache):
        yield from one_nd_step(detectors, step, pos_cache)
        yield from one_nd_step(detectors, step, pos_cache)

    (uid,) = RE(
        default_acquire([{"x1": 1.0, "_id": 0}], [MovableSignal("x1")], [ReadableSignal("det")], per_step=read_twice)
    )

    with pytest.raises(ValueError):
        buffer.pop(uid)


def test_run_event_buffer_pop_drops_runs_that_started_earlier(RE):
    buffer = RunEventBuffer()
    RE.subscribe(buffer)
    x1, det = MovableSignal("x1"), ReadableSignal("det")
    (first,) = RE(default_acquire([{"x1": 1.0, "_id": "a"}], [x1], [det]))
    (second,) = RE(default_acquire([{"x1": 2.0, "_id": "b"}], [x1], [det]))

    assert buffer.pop(second) == {"b": {"x1": 2.0, "det": 0.0}}
    with pytest.raises(KeyError):
        buffer.pop(first)


def test_run_event_buffer_buffers_only_runs_of_its_optimization(RE):
    buffer = RunEventBuffer(optimization_uid="opt-1")
    RE.subscribe(buffer)
    x1, det = MovableSignal("x1"), ReadableSignal("det")
    (own_uid,) = RE(
        default_acquire([{"x1": 1.0, "_id": "a"}], [x1], [det], md={"bluesky-adaptive": {"optimization_uid": "opt-1"}})
    )
    (other_uid,) = RE(
        default_acquire([{"x1": 1.0, "_id": "a"}], [x1], [det], md={"bluesky-adaptive": {"optimization_uid": "other"}})
    )

    # Nothing was popped yet, so only the optimization UID keeps the later run out of the buffer.
    with pytest.raises(KeyError):
        buffer.pop(other_uid)
    assert buffer.pop(own_uid) == {"a": {"x1": 1.0, "det": 0.0}}


def test_per_run_evaluates_the_queue_items_run_from_the_document_stream(RE):
    buffer = RunEventBuffer()
    runs = QueueItemRuns(timeout=5)
    RE.subscribe(buffer)
    RE.subscribe(runs)
    x1, det = MovableSignal("x1"), ReadableSignal("det")
    evaluation_function = per_run(per_suggestion(lambda data: {"objective": 10 * data["x1"]}, buffer.pop), runs.run_uid)

    with ThreadPoolExecutor(max_workers=1) as executor:
        # The evaluation starts waiting before its item runs, as it does once the runner submits the plan.
        future = executor.submit(evaluation_function, "item-2", [])
        RE(default_acquire([{"x1": 1.0, "_id": "a"}], [x1], [det]), queue_server={"queue_item_uid": "item-1"})
        RE(default_acquire([{"x1": 2.0, "_id": "b"}], [x1], [det]), queue_server={"queue_item_uid": "item-2"})

        assert future.result(timeout=5) == [{"objective": 20.0, "_id": "b"}]


def test_queue_item_runs_selects_the_first_run_of_its_optimization_that_passes_run_filter(RE):
    det = ReadableSignal("det")

    def three_runs():
        yield from count([det], md={"bluesky-adaptive": {"optimization_uid": "other"}})
        yield from count([det], md={"bluesky-adaptive": {"optimization_uid": "opt-1"}, "dark_frame": True})
        yield from count([det], md={"bluesky-adaptive": {"optimization_uid": "opt-1"}})

    any_runs = QueueItemRuns(timeout=5)
    own_runs = QueueItemRuns(timeout=5, optimization_uid="opt-1")
    light_runs = QueueItemRuns(timeout=5, optimization_uid="opt-1", run_filter=lambda start: not start.get("dark_frame"))
    for runs in (any_runs, own_runs, light_runs):
        RE.subscribe(runs)

    other_uid, dark_uid, light_uid = RE(three_runs(), queue_server={"queue_item_uid": "item-1"})

    assert any_runs.run_uid("item-1") == other_uid
    assert own_runs.run_uid("item-1") == dark_uid
    assert light_runs.run_uid("item-1") == light_uid


def test_queue_item_runs_raises_when_the_run_fails(RE):
    runs = QueueItemRuns(timeout=5)
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
        runs.run_uid("item-1")


def test_queue_item_runs_drops_items_whose_runs_started_earlier(RE):
    runs = QueueItemRuns(timeout=0.01)
    RE.subscribe(runs)
    det = ReadableSignal("det")
    RE(count([det]), queue_server={"queue_item_uid": "item-1"})
    (uid_2,) = RE(count([det]), queue_server={"queue_item_uid": "item-2"})

    assert runs.run_uid("item-2") == uid_2
    with pytest.raises(TimeoutError):
        runs.run_uid("item-1")
