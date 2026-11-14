from dataclasses import dataclass
from typing import cast
from unittest.mock import MagicMock, patch

import bluesky.plan_stubs as bps
import pytest
from bluesky.run_engine import RunEngine
from bluesky.utils import PlanHalt, RequestAbort, RequestStop, plan

from bluesky_adaptive.plan_stubs import list_scan_in_run
from bluesky_adaptive.plans import (
    acquire_baseline,
    default_acquire,
    optimize,
    optimize_in_run,
    optimize_step,
    sample_suggestions,
)
from bluesky_adaptive.protocols import (
    AcquisitionPlan,
    EvaluationFunction,
    OptimizationProblem,
    Optimizer,
    SupportsStoppingCriteria,
    TrialFaultAware,
)

from .conftest import AlwaysSuccessfulStatus, CheckpointableOptimizer, MovableSignal, ReadableSignal


@plan
def _test_acquisition_plan(suggestions, actuators, sensors, *args, **kwargs):
    """Acquisition plan that returns a predictable uid for testing."""
    yield from bps.null()
    return "test-uid-123"


@plan
def _tuple_acquisition_plan(suggestions, actuators, sensors, *args, **kwargs):
    """Acquisition plan that returns a tuple acquisition identifier."""
    yield from bps.null()
    return ("event-a", "event-b")


class _CustomAcquisitionIdentifier:
    def __hash__(self):
        return 1

    def __repr__(self):
        return "CustomAcquisitionIdentifier()"


_CUSTOM_ACQUISITION_IDENTIFIER = _CustomAcquisitionIdentifier()


@plan
def _custom_identifier_acquisition_plan(suggestions, actuators, sensors, *args, **kwargs):
    """Acquisition plan that returns a custom hashable acquisition identifier."""
    yield from bps.null()
    return _CUSTOM_ACQUISITION_IDENTIFIER


@dataclass
class _DataclassAcquisitionUID:
    run_uid: str
    item_uid: str | None
    plan_name: str
    metadata: dict[str, object]


_DATACLASS_ACQUISITION_UID = _DataclassAcquisitionUID(
    run_uid="run-123",
    item_uid=None,
    plan_name="count",
    metadata={"stream": "primary"},
)


@plan
def _dataclass_uid_acquisition_plan(suggestions, actuators, sensors, *args, **kwargs):
    """Acquisition plan that returns a dataclass UID."""
    yield from bps.null()
    return _DATACLASS_ACQUISITION_UID


class _ArrayRejectingAcquisitionUID:
    def __array__(self, dtype=None, copy=None):
        raise TypeError("Not array-like")

    def __repr__(self):
        return "ArrayRejectingAcquisitionUID()"


_ARRAY_REJECTING_ACQUISITION_UID = _ArrayRejectingAcquisitionUID()


@plan
def _array_rejecting_uid_acquisition_plan(suggestions, actuators, sensors, *args, **kwargs):
    """Acquisition plan that returns a UID that rejects NumPy coercion."""
    yield from bps.null()
    return _ARRAY_REJECTING_ACQUISITION_UID


class _UnhashableAcquisitionReference:
    __hash__ = None

    def __repr__(self):
        return "UnhashableAcquisitionReference()"


_UNHASHABLE_ACQUISITION_REFERENCE = _UnhashableAcquisitionReference()


@plan
def _unhashable_reference_acquisition_plan(suggestions, actuators, sensors, *args, **kwargs):
    """Acquisition plan that returns an unhashable acquisition UID."""
    yield from bps.null()
    return _UNHASHABLE_ACQUISITION_REFERENCE


class StageableReadable(ReadableSignal):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.stage_count = 0
        self.unstage_count = 0

    def stage(self):
        self.stage_count += 1
        return [self]

    def unstage(self):
        self.unstage_count += 1
        return [self]


def _collect_documents():
    """Return a callback and list that collect documents."""
    documents = []

    def callback(name, doc):
        documents.append((name, dict(doc)))

    return callback, documents


def _events_by_stream(documents):
    """Group event documents by descriptor stream name."""
    descriptors = {doc["uid"]: doc["name"] for name, doc in documents if name == "descriptor"}
    events = {}
    for name, doc in documents:
        if name == "event":
            events.setdefault(descriptors[doc["descriptor"]], []).append(doc)
    return events


def _as_list(value):
    return list(value) if hasattr(value, "__iter__") and not isinstance(value, str) else [value]


@pytest.fixture(scope="function")
def RE():
    return RunEngine({})


def test_optimize(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )

    callback, documents = _collect_documents()
    RE(optimize(optimization_problem, callbacks=[callback]))

    optimizer.suggest.assert_called_once_with(1)
    optimizer.ingest.assert_called_once_with([{"objective": 0.0, "_id": 0}])
    assert evaluation_function.call_count == 1

    assert [name for name, _ in documents] == ["start", "descriptor", "event", "stop"]
    start = documents[0][1]
    assert (start["plan_name"], start["iterations"], start["n_points"]) == ("optimize", 1, 1)
    assert documents[-1][1]["exit_status"] == "success"
    data = _events_by_stream(documents)["primary"][0]["data"]
    assert "adaptive_suggestion_id" in data
    assert "adaptive_acquisition_uid" in data
    assert "x1" in data
    assert "objective" in data
    assert data["x1"] == 0.0
    assert data["objective"] == 0.0
    assert data["adaptive_acquisition_uid"] and isinstance(data["adaptive_acquisition_uid"], str)


def test_optimize_accepts_unhashable_acquisition_reference(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    captured = []

    def evaluation_function(uid: _UnhashableAcquisitionReference, suggestions: list[dict]) -> list[dict]:
        captured.append(uid)
        return [{"objective": 1.0, "_id": suggestions[0]["_id"]}]

    typed_evaluation_function = cast(EvaluationFunction[_UnhashableAcquisitionReference], evaluation_function)
    typed_acquisition_plan = cast(AcquisitionPlan[_UnhashableAcquisitionReference], _unhashable_reference_acquisition_plan)
    optimization_problem: OptimizationProblem[_UnhashableAcquisitionReference] = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=typed_evaluation_function,
        acquisition_plan=typed_acquisition_plan,
    )

    with pytest.raises(TypeError):
        hash(_UNHASHABLE_ACQUISITION_REFERENCE)

    callback, documents = _collect_documents()
    RE(optimize(optimization_problem, callbacks=[callback]))

    assert captured == [_UNHASHABLE_ACQUISITION_REFERENCE]
    optimizer.ingest.assert_called_once_with([{"objective": 1.0, "_id": 0}])
    events = _events_by_stream(documents)["primary"]
    assert events[0]["data"]["adaptive_acquisition_uid"] == repr(_UNHASHABLE_ACQUISITION_REFERENCE)


def test_optimization_failure(RE):
    class Alpha(Optimizer, TrialFaultAware): ...

    suggestion = [{"x1": 0.0, "_id": 0}]
    optimizer = MagicMock(spec=Alpha)
    optimizer.suggest.return_value = suggestion
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": 0}])
    aquisition_function = MagicMock(spec=AcquisitionPlan, side_effect=RuntimeError())
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
        acquisition_plan=aquisition_function,
    )

    callback, documents = _collect_documents()
    with pytest.raises(RuntimeError):
        RE(optimize(optimization_problem, callbacks=[callback]))

    optimizer.register_failures.assert_called_once_with(suggestion)
    assert evaluation_function.call_count == 0
    assert documents[-1][0] == "stop"
    assert documents[-1][1]["exit_status"] == "fail"


def test_evaluation_failure_registers_suggestions(RE):
    class Alpha(Optimizer, TrialFaultAware): ...

    suggestions = [{"x1": 0.0, "_id": 0}]
    optimizer = MagicMock(spec=Alpha)
    optimizer.suggest.return_value = suggestions
    evaluation_function = MagicMock(spec=EvaluationFunction, side_effect=RuntimeError("evaluation failed"))
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
        acquisition_plan=_test_acquisition_plan,
    )

    with pytest.raises(RuntimeError, match="evaluation failed"):
        RE(optimize_step(optimization_problem))

    optimizer.register_failures.assert_called_once_with(suggestions)
    optimizer.ingest.assert_not_called()


def test_optimize_multiple(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )

    callback, documents = _collect_documents()
    RE(optimize(optimization_problem, iterations=5, callbacks=[callback]))

    optimizer.suggest.assert_called_with(1)
    optimizer.ingest.assert_called_with([{"objective": 0.0, "_id": 0}])
    assert optimizer.suggest.call_count == 5
    assert optimizer.ingest.call_count == 5
    assert evaluation_function.call_count == 5

    events = _events_by_stream(documents)["primary"]
    assert len(events) == 5
    for event in events:
        data = event["data"]
        assert "adaptive_suggestion_id" in data
        assert "adaptive_acquisition_uid" in data
        assert "x1" in data
        assert "objective" in data
        assert data["x1"] == 0.0
        assert data["objective"] == 0.0


def test_optimize_multiple_with_n_points(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}, {"x1": 0.1, "_id": 1}]
    evaluation_function = MagicMock(
        spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": 0}, {"objective": 0.1, "_id": 1}]
    )
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )
    callback, documents = _collect_documents()
    RE(optimize(optimization_problem, iterations=5, n_points=2, callbacks=[callback]))
    optimizer.suggest.assert_called_with(2)
    optimizer.ingest.assert_called_with([{"objective": 0.0, "_id": 0}, {"objective": 0.1, "_id": 1}])
    assert optimizer.suggest.call_count == 5
    assert optimizer.ingest.call_count == 5
    assert evaluation_function.call_count == 5

    events = _events_by_stream(documents)["primary"]
    assert len(events) == 10
    for ind, event in enumerate(events):
        ard = ind % 2
        data = event["data"]
        assert "adaptive_suggestion_id" in data
        assert "adaptive_acquisition_uid" in data
        assert "x1" in data
        assert "objective" in data

        assert data["adaptive_suggestion_id"] == ["0", "1"][ard]
        assert data["x1"] == [0.0, 0.1][ard]
        assert data["objective"] == [0.0, 0.1][ard]


def test_optimize_complex_case(RE):
    """Test with multi-suggest, multi-parameter, multi-objective, multi-readable case."""

    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [
        {"x1": 0.0, "x2": 0.0, "x3": 0.0, "_id": 0},
        {"x1": 0.1, "x2": 0.2, "x3": 0.3, "_id": 1},
    ]
    evaluation_function = MagicMock(
        spec=EvaluationFunction,
        return_value=[
            {"objective1": 0.0, "objective2": 0.1, "_id": 0},
            {"objective1": 0.1, "objective2": 0.2, "_id": 1},
        ],
    )
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[
            MovableSignal("x1", initial_value=-1.0),
            MovableSignal("x2", initial_value=-1.0),
            MovableSignal("x3", initial_value=-1.0),
        ],
        sensors=[ReadableSignal("readable1"), ReadableSignal("readable2")],
        evaluation_function=evaluation_function,
    )

    callback, documents = _collect_documents()
    uids = RE(optimize(optimization_problem, iterations=2, n_points=2, callbacks=[callback]))

    optimizer.suggest.assert_called_with(2)
    optimizer.ingest.assert_called_with(
        [
            {"objective1": 0.0, "objective2": 0.1, "_id": 0},
            {"objective1": 0.1, "objective2": 0.2, "_id": 1},
        ]
    )
    assert optimizer.suggest.call_count == 2
    assert optimizer.ingest.call_count == 2
    assert evaluation_function.call_count == 2

    events = _events_by_stream(documents)["primary"]
    assert len(events) == 4
    for ind, event in enumerate(events):
        ard = ind % 2
        data = event["data"]
        assert "adaptive_suggestion_id" in data
        assert "adaptive_acquisition_uid" in data
        assert "x1" in data
        assert "x2" in data
        assert "x3" in data
        assert "objective1" in data
        assert "objective2" in data
        assert data["x1"] == [0.0, 0.1][ard]
        assert data["x2"] == [0.0, 0.2][ard]
        assert data["x3"] == [0.0, 0.3][ard]
        assert data["objective1"] == [0.0, 0.1][ard]
        assert data["objective2"] == [0.1, 0.2][ard]
        assert data["adaptive_suggestion_id"] == ["0", "1"][ard]
        assert data["adaptive_acquisition_uid"] in uids  # uids not ordered


@pytest.mark.parametrize("checkpoint_interval", [0, 1, 2, 3])
def test_optimize_with_checkpoint_every_iteration(RE, checkpoint_interval):
    optimizer = MagicMock(spec=CheckpointableOptimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )

    with patch.object(optimizer, "checkpoint", wraps=optimizer.checkpoint) as mock_checkpoint:
        RE(optimize(optimization_problem, iterations=5, n_points=2, checkpoint_interval=checkpoint_interval))
        if checkpoint_interval == 0:
            assert mock_checkpoint.call_count == 0
        else:
            assert mock_checkpoint.call_count == 5 // checkpoint_interval


def test_optimize_with_non_checkpointable_optimizer(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )
    with pytest.raises(ValueError, match="optimizer is not checkpointable"):
        RE(optimize(optimization_problem, iterations=5, n_points=2, checkpoint_interval=1))


def test_optimize_in_run_defaults_to_ordered_suggestion_ids(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 10.0, "_id": "far"}, {"x1": 1.0, "_id": "near"}]
    movable = MovableSignal("x1", initial_value=0.0)
    readable = ReadableSignal("objective")
    captured = []

    callback, documents = _collect_documents()
    RE.subscribe(callback)

    def evaluation_function(uid: tuple[str, ...], suggestions: list[dict]) -> list[dict]:
        captured.append((uid, suggestions))
        events_by_stream = _events_by_stream(documents)
        primary_values = [event["data"]["x1"] for event in events_by_stream["primary"]]
        assert uid == ("near", "far")
        assert suggestions == [{"x1": 10.0, "_id": "far"}, {"x1": 1.0, "_id": "near"}]
        assert primary_values == [1.0, 10.0]
        return [{"objective": float(index), "_id": suggestion_id} for index, suggestion_id in enumerate(uid)]

    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[movable],
        sensors=[readable],
        evaluation_function=evaluation_function,
    )

    try:
        RE(optimize_in_run(optimization_problem, n_points=2))
    finally:
        RE.unsubscribe(callback)

    assert captured == [(("near", "far"), [{"x1": 10.0, "_id": "far"}, {"x1": 1.0, "_id": "near"}])]
    optimizer.ingest.assert_called_once_with([{"objective": 0.0, "_id": "near"}, {"objective": 1.0, "_id": "far"}])
    start_docs = [doc for name, doc in documents if name == "start"]
    assert len(start_docs) == 1
    assert start_docs[0]["run_key"] == "optimize_in_run"
    events_by_stream = _events_by_stream(documents)
    assert len(events_by_stream["primary"]) == 2
    assert len(events_by_stream["optimization"]) == 2
    optimization_data = events_by_stream["optimization"][0]["data"]
    assert optimization_data["suggestion_ids"] == "far"
    assert optimization_data["acquisition_uid"][0] == "near"


def test_list_scan_in_run_allows_custom_per_step_streams(RE):
    def per_step(detectors, step, pos_cache):
        yield from bps.one_nd_step(detectors, step, pos_cache)
        yield from bps.trigger_and_read(detectors, name="monitor")
        yield from bps.trigger_and_read(detectors, name="monitor")

    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}, {"x1": 1.0, "_id": 1}]
    readable = ReadableSignal("objective")

    def evaluation_function(uid: tuple[int, ...], suggestions: list[dict]) -> list[dict]:
        assert uid == (0, 1)
        assert suggestions == [{"x1": 0.0, "_id": 0}, {"x1": 1.0, "_id": 1}]
        return [{"objective": 0.0, "_id": 0}, {"objective": 1.0, "_id": 1}]

    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[readable],
        evaluation_function=evaluation_function,
        acquisition_plan=list_scan_in_run,
    )
    callback, documents = _collect_documents()
    RE.subscribe(callback)
    try:
        RE(optimize_in_run(optimization_problem, n_points=2, per_step=per_step))
    finally:
        RE.unsubscribe(callback)

    optimizer.ingest.assert_called_once_with([{"objective": 0.0, "_id": 0}, {"objective": 1.0, "_id": 1}])
    events_by_stream = _events_by_stream(documents)
    assert len(events_by_stream["primary"]) == 2
    assert len(events_by_stream["monitor"]) == 4
    assert len(events_by_stream["optimization"]) == 2


def test_list_scan_in_run_accepts_no_sensors(RE):
    movable = MovableSignal("x1", initial_value=-1.0)
    acquisition_ids = []

    @plan
    def in_run():
        yield from bps.open_run()
        acquisition_ids.append((yield from list_scan_in_run([{"x1": 0.0, "_id": "point"}], [movable], sensors=None)))
        yield from bps.close_run()

    RE(in_run())

    assert acquisition_ids == [("point",)]
    assert movable._value == 0.0


def test_list_scan_in_run_ignores_non_readable_sensors(RE, caplog):
    class NamedOnlySensor:
        name = "not-readable"

    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    evaluation_function = MagicMock(return_value=[{"objective": 0.0, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[NamedOnlySensor()],
        evaluation_function=evaluation_function,
    )

    RE(optimize_in_run(optimization_problem))

    assert "Some sensors are not readable and will be ignored" in caplog.text
    optimizer.ingest.assert_called_once_with([{"objective": 0.0, "_id": 0}])


def test_list_scan_in_run_routes_without_actuator_readback(RE):
    class MoveOnlySignal:
        def __init__(self, name, initial_value):
            self.name = name
            self.value = initial_value

        @property
        def parent(self):
            return None

        def set(self, value):
            self.value = value
            return AlwaysSuccessfulStatus()

    def per_step(detectors, step, pos_cache):
        yield from bps.move_per_step(step, pos_cache)

    suggestions = [{"x1": 10.0, "_id": "far"}, {"x1": 1.0, "_id": "near"}]
    movable = MoveOnlySignal("x1", initial_value=0.0)
    acquisition_ids = []

    @plan
    def in_run():
        yield from bps.open_run()
        acquisition_ids.append((yield from list_scan_in_run(suggestions, [movable], [], per_step=per_step)))
        yield from bps.close_run()

    RE(in_run())

    assert set(acquisition_ids[0]) == {"far", "near"}
    suggestion_by_id = {suggestion["_id"]: suggestion for suggestion in suggestions}
    assert movable.value == suggestion_by_id[acquisition_ids[0][-1]]["x1"]


def test_list_scan_in_run_preserves_stage_lifecycle(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    readable = StageableReadable("objective")
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[readable],
        evaluation_function=MagicMock(return_value=[{"objective": 0.0, "_id": 0}]),
    )

    RE(optimize_in_run(optimization_problem))

    assert readable.stage_count == 1
    assert readable.unstage_count == 1


def test_optimize_in_run_rejects_child_run_control(RE):
    @plan
    def acquisition_plan(suggestions, actuators, sensors, *args, **kwargs):
        yield from bps.open_run(md={"run_key": "child"})
        yield from bps.trigger_and_read(sensors or [])
        yield from bps.close_run()
        return "child-run"

    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    evaluation_function = MagicMock(return_value=[{"objective": 0.0, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
        acquisition_plan=acquisition_plan,
    )

    with pytest.raises(ValueError, match="must not issue 'open_run'"):
        RE(optimize_in_run(optimization_problem))

    evaluation_function.assert_not_called()
    optimizer.ingest.assert_not_called()


def test_optimize_in_run_rejects_unhashable_suggestion_ids(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": ["not-hashable"]}]
    evaluation_function = MagicMock(return_value=[{"objective": 0.0, "_id": ["not-hashable"]}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )

    with pytest.raises(TypeError, match="hashable '_id'"):
        RE(optimize_in_run(optimization_problem))

    evaluation_function.assert_not_called()
    optimizer.ingest.assert_not_called()


def test_optimize_in_run_rejects_duplicate_suggestion_ids(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": "same"}, {"x1": 1.0, "_id": "same"}]
    evaluation_function = MagicMock(return_value=[{"objective": 0.0, "_id": "same"}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )

    with pytest.raises(ValueError, match="unique '_id'"):
        RE(optimize_in_run(optimization_problem, n_points=2))

    evaluation_function.assert_not_called()
    optimizer.ingest.assert_not_called()


def test_optimize_in_run_registers_failures(RE):
    class FaultAwareOptimizer(Optimizer, TrialFaultAware): ...

    suggestions = [{"x1": 0.0, "_id": 0}]
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    optimizer.suggest.return_value = suggestions
    evaluation_function = MagicMock(side_effect=RuntimeError("evaluation failed"))
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
        acquisition_plan=_test_acquisition_plan,
    )

    with pytest.raises(RuntimeError, match="evaluation failed"):
        RE(optimize_in_run(optimization_problem))

    optimizer.register_failures.assert_called_once_with(suggestions)
    optimizer.ingest.assert_not_called()


def test_optimize_step_rejects_suggestion_without_id(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0}]
    evaluation_function = MagicMock(return_value=[{"objective": 0.0, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
        acquisition_plan=_test_acquisition_plan,
    )

    with pytest.raises(ValueError, match="All suggestions must contain an '_id' key"):
        RE(optimize_step(optimization_problem))

    evaluation_function.assert_not_called()
    optimizer.ingest.assert_not_called()


@pytest.mark.parametrize(
    ("outcomes", "error_type", "message"),
    [
        ([{"objective": 0.0}, {"objective": 1.0, "_id": "b"}], ValueError, "All outcomes must contain an '_id' key"),
        (
            [{"objective": 0.0, "_id": []}, {"objective": 1.0, "_id": "b"}],
            TypeError,
            "hashable '_id'",
        ),
        (
            [{"objective": 0.0, "_id": "a"}, {"objective": 1.0, "_id": "a"}],
            ValueError,
            "unique '_id'",
        ),
        (
            [{"objective": 0.0, "_id": "a"}, {"objective": 1.0, "_id": "c"}],
            ValueError,
            "same IDs",
        ),
    ],
)
def test_optimize_step_rejects_invalid_outcome_ids(RE, outcomes, error_type, message):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": "a"}, {"x1": 1.0, "_id": "b"}]
    evaluation_function = MagicMock(return_value=outcomes)
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
        acquisition_plan=_test_acquisition_plan,
    )

    with pytest.raises(error_type, match=message):
        RE(optimize_step(optimization_problem, n_points=2))

    evaluation_function.assert_called_once()
    optimizer.ingest.assert_not_called()


def test_optimize_in_run_stops_early(RE):
    class StoppingOptimizer(Optimizer, SupportsStoppingCriteria): ...

    optimizer = MagicMock(spec=StoppingOptimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    optimizer.should_stop.side_effect = [(False, None), (True, "converged")]
    evaluation_function = MagicMock(return_value=[{"objective": 0.0, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
        acquisition_plan=_test_acquisition_plan,
    )

    RE(optimize_in_run(optimization_problem, iterations=5))

    assert optimizer.suggest.call_count == 2
    assert optimizer.ingest.call_count == 2
    assert optimizer.should_stop.call_count == 2


def test_optimize_in_run_checkpoints(RE):
    optimizer = MagicMock(spec=CheckpointableOptimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(return_value=[{"objective": 0.0, "_id": 0}]),
        acquisition_plan=_test_acquisition_plan,
    )

    RE(optimize_in_run(optimization_problem, iterations=3, checkpoint_interval=2))

    assert optimizer.checkpoint.call_count == 1


def test_optimize_step_default(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )

    RE(optimize_step(optimization_problem))

    optimizer.suggest.assert_called_once_with(1)
    optimizer.ingest.assert_called_once_with([{"objective": 0.0, "_id": 0}])
    assert evaluation_function.call_count == 1


def test_optimize_event_document_structure(RE):
    """Validate a record-run event document in detail."""
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.5, "_id": 0}]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 1.25, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
        acquisition_plan=_test_acquisition_plan,
    )

    callback, documents = _collect_documents()
    RE(optimize(optimization_problem, callbacks=[callback]))

    events = _events_by_stream(documents)["primary"]
    assert len(events) == 1
    data = events[0]["data"]
    assert "adaptive_suggestion_id" in data
    assert "adaptive_acquisition_uid" in data
    assert "x1" in data
    assert "objective" in data

    # Validate predictable values from custom acquisition plan
    assert data["adaptive_acquisition_uid"] == "test-uid-123"
    assert data["adaptive_iteration"] == 0
    assert data["x1"] == 0.5
    assert data["objective"] == 1.25
    assert data["adaptive_suggestion_id"] == "0"


def test_optimize_with_tuple_acquisition_identifier(RE):
    """Pass tuple acquisition identifiers unchanged to evaluation and record them as their repr."""
    suggestion = {"x1": 0.5, "_id": 0}
    outcome = {"objective": 1.25, "_id": 0}
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [suggestion]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[outcome])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
        acquisition_plan=_tuple_acquisition_plan,
    )

    callback, documents = _collect_documents()
    RE(optimize(optimization_problem, callbacks=[callback]))

    acquisition_identifier = ("event-a", "event-b")
    evaluation_function.assert_called_once_with(acquisition_identifier, [suggestion])
    events = _events_by_stream(documents)["primary"]
    assert len(events) == 1
    assert events[0]["data"]["adaptive_acquisition_uid"] == repr(acquisition_identifier)


def test_optimize_with_custom_hashable_acquisition_identifier_uses_repr(RE):
    """Store repr for hashable acquisition identifiers that are not native array-like values."""
    suggestion = {"x1": 0.5, "_id": 0}
    outcome = {"objective": 1.25, "_id": 0}
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [suggestion]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[outcome])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
        acquisition_plan=_custom_identifier_acquisition_plan,
    )

    callback, documents = _collect_documents()
    RE(optimize(optimization_problem, callbacks=[callback]))

    evaluation_function.assert_called_once_with(_CUSTOM_ACQUISITION_IDENTIFIER, [suggestion])
    events = _events_by_stream(documents)["primary"]
    assert len(events) == 1
    assert events[0]["data"]["adaptive_acquisition_uid"] == repr(_CUSTOM_ACQUISITION_IDENTIFIER)


def test_optimize_with_dataclass_acquisition_uid_uses_repr(RE):
    """Store a dataclass acquisition UID via its repr."""
    suggestion = {"x1": 0.5, "_id": 0}
    outcome = {"objective": 1.25, "_id": 0}
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [suggestion]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[outcome])
    typed_evaluation_function = cast(EvaluationFunction[_DataclassAcquisitionUID], evaluation_function)
    typed_acquisition_plan = cast(AcquisitionPlan[_DataclassAcquisitionUID], _dataclass_uid_acquisition_plan)
    optimization_problem: OptimizationProblem[_DataclassAcquisitionUID] = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=typed_evaluation_function,
        acquisition_plan=typed_acquisition_plan,
    )

    callback, documents = _collect_documents()
    RE(optimize(optimization_problem, callbacks=[callback]))

    evaluation_function.assert_called_once_with(_DATACLASS_ACQUISITION_UID, [suggestion])
    events = _events_by_stream(documents)["primary"]
    assert len(events) == 1
    assert events[0]["data"]["adaptive_acquisition_uid"] == repr(_DATACLASS_ACQUISITION_UID)


def test_optimize_with_array_rejecting_uid_uses_repr(RE):
    """Fall back to repr when a UID rejects NumPy array coercion."""
    suggestion = {"x1": 0.5, "_id": 0}
    outcome = {"objective": 1.25, "_id": 0}
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [suggestion]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[outcome])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
        acquisition_plan=_array_rejecting_uid_acquisition_plan,
    )

    callback, documents = _collect_documents()
    RE(optimize(optimization_problem, callbacks=[callback]))

    evaluation_function.assert_called_once_with(_ARRAY_REJECTING_ACQUISITION_UID, [suggestion])
    events = _events_by_stream(documents)["primary"]
    assert events[0]["data"]["adaptive_acquisition_uid"] == "ArrayRejectingAcquisitionUID()"


def test_optimize_step_custom_acquisition_plan(RE):
    acquisition_plan = MagicMock(spec=AcquisitionPlan)
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": 0}])
    movable = MovableSignal("x1", initial_value=-1.0)
    readable = ReadableSignal("objective")
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[movable],
        sensors=[readable],
        evaluation_function=evaluation_function,
        acquisition_plan=acquisition_plan,
    )

    RE(optimize_step(optimization_problem))
    optimizer.suggest.assert_called_once_with(1)
    acquisition_plan.assert_called_once_with(
        [{"x1": 0.0, "_id": 0}],
        [movable],
        [readable],
    )
    optimizer.ingest.assert_called_once_with([{"objective": 0.0, "_id": 0}])
    assert evaluation_function.call_count == 1


def test_default_acquire_single_movable_readable(RE):
    """Test with single movable, position, and readable."""
    movable = MovableSignal("x1", initial_value=-1.0)
    readable = ReadableSignal("objective")
    with patch.object(readable, "read", wraps=readable.read) as mock_read:
        RE(
            default_acquire(
                [{"x1": 0.0, "_id": 0}],
                [movable],
                [readable],
            )
        )
        assert mock_read.call_count == 1

    assert movable.read()["x1"]["value"] == 0.0


def test_default_acquire_merges_metadata(RE):
    """Test with additional run metadata."""
    movable = MovableSignal("x1", initial_value=-1.0)
    readable = ReadableSignal("objective")
    suggestions = [{"x1": 0.0, "_id": 0}]
    start_docs = []

    def callback(name, doc):
        if name == "start":
            start_docs.append(doc)

    RE.subscribe(callback)
    try:
        RE(
            default_acquire(
                suggestions,
                [movable],
                [readable],
                md={"sample": "test-sample"},
            )
        )
    finally:
        RE.unsubscribe(callback)

    assert len(start_docs) == 1
    assert start_docs[0]["sample"] == "test-sample"
    assert start_docs[0]["blop_suggestions"] == suggestions
    assert start_docs[0]["run_key"] == "default_acquire"


def test_default_acquire_records_acquisition_order(RE):
    """Record IDs in scan order rather than incoming suggestion order."""
    movable = MovableSignal("x1", initial_value=-1.0)
    readable = ReadableSignal("objective")
    suggestions = [{"x1": 10.0, "_id": "far"}, {"x1": 0.0, "_id": "near"}]
    start_docs = []

    def callback(name, doc):
        if name == "start":
            start_docs.append(doc)

    RE.subscribe(callback)
    try:
        RE(default_acquire(suggestions, [movable], [readable]))
    finally:
        RE.unsubscribe(callback)

    assert len(start_docs) == 1
    assert start_docs[0]["blop_suggestions"] == [suggestions[1], suggestions[0]]
    assert start_docs[0]["blop_acquisition_order"] == ["near", "far"]


def test_default_acquire_multiple_movables_readables(RE):
    """Test with multiple movables, positions, and readables."""
    movable1 = MovableSignal("x1", initial_value=-1.0)
    movable2 = MovableSignal("x2", initial_value=-1.0)
    readable1 = ReadableSignal("objective1")
    readable2 = ReadableSignal("objective2")

    with (
        patch.object(movable1, "set", wraps=movable1.set) as mock_set1,
        patch.object(movable2, "set", wraps=movable2.set) as mock_set2,
        patch.object(readable1, "read", wraps=readable1.read) as mock_read1,
        patch.object(readable2, "read", wraps=readable2.read) as mock_read2,
    ):
        RE(
            default_acquire(
                [{"x1": 0.0, "x2": 0.0, "_id": 0}, {"x1": 0.1, "x2": 0.1, "_id": 1}],
                [movable1, movable2],
                [readable1, readable2],
            )
        )

        # Verify movables were set in correct order
        assert mock_set1.call_count == 2
        assert mock_set2.call_count == 2
        assert mock_set1.call_args_list[0][0][0] == 0.0  # First call
        assert mock_set2.call_args_list[0][0][0] == 0.0
        assert mock_set1.call_args_list[1][0][0] == 0.1  # Second call
        assert mock_set2.call_args_list[1][0][0] == 0.1

        # Verify reads happened twice
        assert mock_read1.call_count == 2
        assert mock_read2.call_count == 2

    # Verify final positions
    assert movable1.read()["x1"]["value"] == 0.1
    assert movable2.read()["x2"]["value"] == 0.1


def test_default_acquire_checkpoint_removal(RE):
    movable = MovableSignal("x1", initial_value=-1.0)
    readable = ReadableSignal("objective")
    commands = []

    RE.msg_hook = lambda msg: commands.append(msg.command)
    RE(default_acquire([{"x1": 0.0, "_id": 0}, {"x1": 0.1, "_id": 1}], [movable], [readable]))

    assert "checkpoint" not in commands


def test_acquire_baseline(RE):
    """Test acquiring a baseline reading from suggested parameterizations."""
    optimizer = MagicMock(spec=Optimizer)
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": "baseline"}])

    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1", initial_value=-1.0)],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )

    callback, documents = _collect_documents()
    RE(acquire_baseline(optimization_problem, parameterization={"x1": 0.0}, callbacks=[callback]))

    # No suggestions are made since this is a baseline reading
    assert optimizer.suggest.call_count == 0

    optimizer.ingest.assert_called_once_with([{"objective": 0.0, "_id": "baseline", "x1": 0.0}])
    assert evaluation_function.call_count == 1
    assert documents[0][1]["plan_name"] == "acquire_baseline"
    (event,) = _events_by_stream(documents)["primary"]
    assert {key: event["data"][key] for key in ("adaptive_suggestion_id", "adaptive_iteration", "x1", "objective")} == {
        "adaptive_suggestion_id": "baseline",
        "adaptive_iteration": -1,
        "x1": 0.0,
        "objective": 0.0,
    }


def test_acquire_baseline_from_current(RE):
    """Test acquiring a baseline reading from the current movable positions."""
    optimizer = MagicMock(spec=Optimizer)
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": "baseline"}])
    movable = MovableSignal("x1", initial_value=-1.0)

    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[movable],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )

    with (
        patch.object(movable, "set", wraps=movable.set) as mock_set,
        patch.object(movable, "read", wraps=movable.read) as mock_read,
    ):
        RE(acquire_baseline(optimization_problem))

        # Ensure the movable was read twice (once for the baseline, once during the acquisition)
        assert mock_read.call_count == 2
        # Ensure the movable was set once to the current value
        assert mock_set.call_count == 1
        assert mock_set.call_args_list[0][0][0] == -1.0

    # No suggestions are made since this is a baseline reading from the current movable positions
    assert optimizer.suggest.call_count == 0

    optimizer.ingest.assert_called_once_with([{"objective": 0.0, "_id": "baseline", "x1": -1.0}])
    assert evaluation_function.call_count == 1


def test_optimize_without_iteration_limit_stops_at_criterion(RE):
    """Run until the configured stopping criterion is met when no iteration limit is set."""

    class StoppingOptimizer(Optimizer, SupportsStoppingCriteria): ...

    optimizer = MagicMock(spec=StoppingOptimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    optimizer.should_stop.side_effect = [
        (False, None),
        (False, None),
        (True, "converged"),
    ]
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": 0}]),
    )

    RE(optimize(optimization_problem, iterations=None))

    assert optimizer.suggest.call_count == 3
    assert optimizer.should_stop.call_count == 3


def test_optimize_without_iteration_limit_requires_stopping_criteria(RE):
    """Reject an unbounded optimization that has no stopping criterion."""
    optimizer = MagicMock(spec=Optimizer)
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(spec=EvaluationFunction),
    )

    with pytest.raises(ValueError, match="iterations=None requires an optimizer that implements SupportsStoppingCriteria"):
        RE(optimize(optimization_problem, iterations=None))

    optimizer.suggest.assert_not_called()


def test_optimize_max_number_of_iterations_before_stop(RE):
    """Tests that the optimization stops at a set number of iterations"""

    class StoppingOptimizer(Optimizer, SupportsStoppingCriteria): ...

    optimizer = MagicMock(spec=StoppingOptimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]

    # Set up the optimizer to stop after 2 iterations
    optimizer.should_stop.side_effect = [
        (False, None),
        (True, "converged"),
    ]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )

    RE(optimize(optimization_problem, iterations=5))

    assert optimizer.suggest.call_count == 2
    assert optimizer.should_stop.call_count == 2


def test_optimize_stop_condition_not_hit(RE):
    """Tests that optimization stops before stop condition is met"""

    class StoppingOptimizer(Optimizer, SupportsStoppingCriteria): ...

    optimizer = MagicMock(spec=StoppingOptimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]

    # Allow for 3 iterations
    optimizer.should_stop.side_effect = [(False, None), (False, None), (False, None)]
    evaluation_function = MagicMock(spec=EvaluationFunction, return_value=[{"objective": 0.0, "_id": 0}])
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )

    # We only are running for 2 iterations, so stop condition should not be met
    RE(optimize(optimization_problem, iterations=2))

    assert optimizer.suggest.call_count == 2
    assert optimizer.should_stop.call_count == 2


def test_optimize_stops_when_change_is_within_tolerance(RE):
    """Tests that the optimization stops when the change in objective value is within a specified tolerance."""

    class ToleranceStopOptimizer(Optimizer, SupportsStoppingCriteria):
        def __init__(self, tolerance: float):
            self.tolerance = tolerance
            self._last_value: float | None = None
            self._previous_value: float | None = None

        def suggest(self, num_points: int | None = None) -> list[dict]:
            return [{"x1": 0.0, "_id": 0}]

        def ingest(self, points: list[dict]) -> None:
            self._previous_value = self._last_value
            self._last_value = points[0]["objective"]

        def should_stop(self) -> tuple[bool, str | None]:
            if self._previous_value is None or self._last_value is None:
                return (False, None)

            if abs(self._last_value - self._previous_value) <= self.tolerance:
                return (True, "objective change within tolerance")

            return (False, None)

    # Stop optimization when the change is within 0.1
    optimizer = ToleranceStopOptimizer(tolerance=0.1)
    evaluation_function = MagicMock(
        spec=EvaluationFunction,
        side_effect=[
            [{"objective": 0.5, "_id": 0}],
            [{"objective": 0.55, "_id": 0}],
        ],
    )
    optimization_problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=evaluation_function,
    )

    callback, documents = _collect_documents()
    RE(optimize(optimization_problem, iterations=5, callbacks=[callback]))

    assert evaluation_function.call_count == 2
    assert len(_events_by_stream(documents)["primary"]) == 2


def test_optimize_emits_checkpoints_per_iteration(RE):
    optimization_problem = OptimizationProblem(
        MagicMock(spec=Optimizer),
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(spec=EvaluationFunction),
    )
    commands = []

    RE.msg_hook = lambda msg: commands.append(msg.command)
    RE(optimize(optimization_problem, iterations=5))

    assert commands.count("checkpoint") == 5


@pytest.mark.parametrize(
    ("run_plan", "plan_name"),
    [
        (lambda problem, callback: optimize(problem, callbacks=[callback]), "optimize"),
        (lambda problem, callback: optimize_in_run(problem), "optimize_in_run"),
        (
            lambda problem, callback: sample_suggestions(problem, [{"x1": 1.0, "_id": 0}], callbacks=[callback]),
            "sample_suggestions",
        ),
        (lambda problem, callback: acquire_baseline(problem, {"x1": 1.0}, callbacks=[callback]), "acquire_baseline"),
    ],
    ids=["optimize", "optimize_in_run", "sample_suggestions", "acquire_baseline"],
)
def test_optimization_runs_record_the_problem_optimization_uid(RE, run_plan, plan_name):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(
            side_effect=lambda uid, suggestions: [{"objective": 0.0, "_id": s["_id"]} for s in suggestions]
        ),
    )
    callback, documents = _collect_documents()
    RE.subscribe(callback)

    RE(run_plan(problem, callback))

    start = next(doc for name, doc in documents if name == "start" and doc["plan_name"] == plan_name)
    assert start["bluesky-adaptive"]["optimization_uid"] == problem.optimization_uid


def test_optimization_runs_duplicate_released_metadata_at_top_level(RE):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(return_value=[{"objective": 0.0, "_id": 0}]),
    )
    callback, documents = _collect_documents()

    RE(optimize(problem, iterations=1, callbacks=[callback]))

    start = next(doc for name, doc in documents if name == "start" and doc["plan_name"] == "optimize")
    assert start["sensors"] == ["objective"]
    assert start["actuators"] == ["x1"]
    for key in ("evaluation_function", "acquisition_plan", "optimizer", "sensors", "actuators"):
        assert start["bluesky-adaptive"][key] == start[key]


class FaultAwareOptimizer(Optimizer, TrialFaultAware): ...


def test_optimize_failed_acquisition_stops_record(RE):
    calls = 0

    @plan
    def acquisition_plan(suggestions, actuators, sensors, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("detector offline")
        yield from bps.null()
        return "acquisition-uid"

    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.side_effect = lambda _: [{"x1": 0.0, "_id": optimizer.suggest.call_count - 1}]
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(
            side_effect=lambda uid, suggestions: [{"objective": 0.0, "_id": suggestions[0]["_id"]}]
        ),
        acquisition_plan=acquisition_plan,
    )
    callback, documents = _collect_documents()

    with pytest.raises(RuntimeError, match="detector offline"):
        RE(optimize(problem, iterations=3, callbacks=[callback]))

    stop = documents[-1][1]
    assert stop["exit_status"] == "fail"
    assert stop["reason"] == "Iteration 1 with suggestion IDs [1] failed: RuntimeError: detector offline"
    assert len(_events_by_stream(documents)["primary"]) == 1
    optimizer.ingest.assert_called_once()


def test_optimize_invalid_outcomes_mark_trials_failed(RE):
    suggestions = [{"x1": 0.0, "_id": 0}]
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    optimizer.suggest.return_value = suggestions
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(return_value=[{"objective": 0.0, "_id": 1}]),
        acquisition_plan=_test_acquisition_plan,
    )
    callback, documents = _collect_documents()

    with pytest.raises(ValueError, match="same IDs"):
        RE(optimize(problem, callbacks=[callback]))

    optimizer.register_failures.assert_called_once_with(suggestions)
    optimizer.ingest.assert_not_called()
    assert [name for name, _ in documents] == ["start", "stop"]


def test_optimize_recording_failure_marks_trials_failed(RE):
    suggestions = [{"x1": 0.0, "_id": 0}]
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    optimizer.suggest.return_value = suggestions
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(return_value=[{"objective": 0.0, "_id": 0}]),
        acquisition_plan=_test_acquisition_plan,
    )
    documents = []

    def callback(name, doc):
        documents.append((name, dict(doc)))
        if name == "event":
            raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        RE(optimize(problem, callbacks=[callback]))

    optimizer.register_failures.assert_called_once_with(suggestions)
    optimizer.ingest.assert_not_called()
    assert documents[-1][0] == "stop"
    assert documents[-1][1]["exit_status"] == "fail"


def test_optimize_stopping_criterion_records_stopping_step(RE):
    class StoppingOptimizer(Optimizer, SupportsStoppingCriteria): ...

    optimizer = MagicMock(spec=StoppingOptimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    optimizer.should_stop.side_effect = [(False, None), (True, "converged")]
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(return_value=[{"objective": 0.0, "_id": 0}]),
    )
    callback, documents = _collect_documents()

    RE(optimize(problem, iterations=5, callbacks=[callback]))

    stop = documents[-1][1]
    assert stop["exit_status"] == "success"
    assert stop["reason"] == "Stopping criterion met at iteration 1: converged"
    assert len(_events_by_stream(documents)["primary"]) == 2


@pytest.mark.parametrize(
    ("exception", "exit_status", "reason"),
    [
        (RequestStop(), "success", "Stop requested"),
        (RequestAbort(), "abort", "Aborted: RequestAbort"),
        # PlanHalt is a GeneratorExit subclass: `yield from` closes the delegated plan, so optimize sees GeneratorExit.
        (PlanHalt(), "abort", "Aborted: GeneratorExit"),
    ],
    ids=["stop", "abort", "halt"],
)
def test_optimize_interrupts_stop_record(exception, exit_status, reason):
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"x1": 0.0, "_id": 0}]
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(return_value=[{"objective": 0.0, "_id": 0}]),
        acquisition_plan=_test_acquisition_plan,
    )
    callback, documents = _collect_documents()
    generator = iter(optimize(problem, iterations=2, callbacks=[callback]))

    next(generator)
    with pytest.raises(type(exception)):
        generator.throw(exception)

    stop = documents[-1][1]
    assert (stop["exit_status"], stop["reason"]) == (exit_status, reason)
    optimizer.ingest.assert_not_called()


def test_sample_suggestions_records_points(RE):
    optimizer = MagicMock(spec=Optimizer)
    suggestions = [{"x1": 0.0, "_id": 0}, {"x1": 1.0, "_id": 1}]
    outcomes = [{"objective": 0.0, "_id": 0}, {"objective": 1.0, "_id": 1}]
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(return_value=outcomes),
        acquisition_plan=_test_acquisition_plan,
    )
    callback, documents = _collect_documents()

    RE(sample_suggestions(problem, suggestions, callbacks=[callback]))

    assert [name for name, _ in documents] == ["start", "descriptor", "event", "event", "stop"]
    assert documents[0][1]["plan_name"] == "sample_suggestions"
    assert [event["data"]["adaptive_iteration"] for event in _events_by_stream(documents)["primary"]] == [-1, -1]
    optimizer.ingest.assert_called_once_with(outcomes)


@pytest.mark.parametrize(
    ("suggestions", "outcomes", "message"),
    [
        ([{"x1": 0.0, "_id": 0}, {"x1": 1.0, "_id": 0}], [{"objective": 0.0, "_id": 0}], "unique '_id'"),
        ([{"x1": 0.0, "_id": 0}], [{"objective": 0.0, "_id": 1}], "same IDs"),
    ],
    ids=["duplicate-suggestion-ids", "mismatched-outcome-ids"],
)
def test_sample_suggestions_validates_suggestions_and_outcomes(RE, suggestions, outcomes, message):
    optimizer = MagicMock(spec=Optimizer)
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(return_value=outcomes),
        acquisition_plan=_test_acquisition_plan,
    )

    with pytest.raises(ValueError, match=message):
        RE(sample_suggestions(problem, suggestions))

    optimizer.ingest.assert_not_called()


def test_acquire_baseline_rejects_mismatched_outcomes(RE):
    optimizer = MagicMock(spec=Optimizer)
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(return_value=[{"objective": 0.0, "_id": "other"}]),
    )

    with pytest.raises(ValueError, match="same IDs"):
        RE(acquire_baseline(problem, {"x1": 0.0}))

    optimizer.ingest.assert_not_called()


def test_acquire_baseline_failure_does_not_register_failures(RE):
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    problem = OptimizationProblem(
        optimizer=optimizer,
        actuators=[MovableSignal("x1")],
        sensors=[ReadableSignal("objective")],
        evaluation_function=MagicMock(side_effect=RuntimeError("evaluation failed")),
    )
    callback, documents = _collect_documents()

    with pytest.raises(RuntimeError, match="evaluation failed"):
        RE(acquire_baseline(problem, {"x1": 0.0}, callbacks=[callback]))

    optimizer.register_failures.assert_not_called()
    assert [name for name, _ in documents] == ["start", "stop"]
    assert documents[-1][1]["exit_status"] == "fail"
