from unittest.mock import MagicMock

import pytest
from bluesky import SupplementalData
from bluesky.plan_stubs import one_nd_step
from bluesky.run_engine import RunEngine

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
        evaluation_function=per_suggestion(buffer.pop)(lambda data: {"objective": 10 * data["x1"]}),
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
