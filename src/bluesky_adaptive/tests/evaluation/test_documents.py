from unittest.mock import MagicMock

import pytest
from bluesky import SupplementalData
from bluesky.plan_stubs import one_nd_step, pause
from bluesky.plans import count
from bluesky.run_engine import RunEngine
from bluesky.utils import RunEngineInterrupted

from bluesky_adaptive.evaluation import per_suggestion
from bluesky_adaptive.evaluation.documents import RunEventBuffer
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


def test_run_event_buffer_ignores_runs_without_an_acquisition_order(RE):
    buffer = RunEventBuffer()
    RE.subscribe(buffer)
    # Plans other than default_acquire, e.g. bluesky.plans.count, do not record the acquisition order.
    (uid,) = RE(count([ReadableSignal("det")]))

    with pytest.raises(KeyError):
        buffer.pop(uid)


def test_run_event_buffer_rejects_a_run_that_failed_partway(RE):
    buffer = RunEventBuffer()
    RE.subscribe(buffer)
    uids = []
    RE.subscribe(lambda name, doc: uids.append(doc["uid"]), "start")
    steps = []

    def fail_after_first_event(detectors, step, pos_cache):
        if steps:
            raise OSError("detector timed out")
        steps.append(step)
        yield from one_nd_step(detectors, step, pos_cache)

    suggestions = [{"x1": 1.0, "_id": 0}, {"x1": 2.0, "_id": 1}]
    with pytest.raises(OSError):
        RE(default_acquire(suggestions, [MovableSignal("x1")], [ReadableSignal("det")], per_step=fail_after_first_event))

    with pytest.raises(RuntimeError, match="'fail'"):
        buffer.pop(uids[0])


def test_run_event_buffer_rejects_an_aborted_run(RE):
    buffer = RunEventBuffer()
    RE.subscribe(buffer)
    uids = []
    RE.subscribe(lambda name, doc: uids.append(doc["uid"]), "start")

    def pause_after_reading(detectors, step, pos_cache):
        yield from one_nd_step(detectors, step, pos_cache)
        yield from pause()

    # The run records an event for its only suggestion before it is paused and aborted, so the counts match.
    with pytest.raises(RunEngineInterrupted):
        RE(
            default_acquire(
                [{"x1": 1.0, "_id": 0}], [MovableSignal("x1")], [ReadableSignal("det")], per_step=pause_after_reading
            )
        )
    RE.abort("operator stopped the run")

    with pytest.raises(RuntimeError, match="'abort'"):
        buffer.pop(uids[0])
