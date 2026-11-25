import logging
from logging.handlers import QueueHandler
from queue import Queue
from threading import Event
from unittest.mock import MagicMock

import pytest
from bluesky import plan_stubs as bps
from bluesky.run_engine import RunEngine
from bluesky.utils import RunEngineInterrupted

from bluesky_adaptive._compat import call_in_thread
from bluesky_adaptive.plan_stubs import navigate_to_best
from bluesky_adaptive.protocols import Optimizer

from .conftest import MovableSignal


@pytest.fixture()
def RE():
    return RunEngine({})


@pytest.fixture
def fallback(monkeypatch):
    monkeypatch.delattr(bps, "call_in_thread", raising=False)
    return call_in_thread


def test_thread_plan_preview_and_callback_errors(RE, fallback):
    callback = MagicMock(side_effect=StopIteration)
    assert [msg.command for msg in fallback(callback, f=1)] == ["wait_for"]
    callback.assert_not_called()
    with pytest.raises(RuntimeError, match="raised StopIteration") as exc:
        RE(fallback(callback, f=1))
    assert isinstance(exc.value.__cause__, StopIteration)
    callback.assert_called_once_with(f=1)


@pytest.mark.parametrize("rewindable", [True, False])
def test_thread_plan_resumes_the_original_evaluation(RE, fallback, rewindable):
    RE.rewindable = rewindable
    release = Event()
    results = []

    def evaluate():
        RE.request_pause(defer=False)
        assert release.wait(5)
        return 7

    callback = MagicMock(side_effect=evaluate)

    def evaluate_plan():
        results.append((yield from fallback(callback)))

    try:
        with pytest.raises(RunEngineInterrupted):
            RE(evaluate_plan())
        RE.msg_hook = lambda msg: release.set() if msg.command == "wait_for" else None
        RE.resume()
    finally:
        release.set()
    assert results == [7]
    callback.assert_called_once_with()


def test_thread_plan_reports_worker_failure_after_abort(RE, fallback):
    release = Event()
    records = Queue()
    handler = QueueHandler(records)
    logger = logging.getLogger("bluesky_adaptive._compat")

    def evaluate():
        RE.request_pause(defer=False)
        assert release.wait(5)
        raise ValueError("poll failed")

    logger.addHandler(handler)
    try:
        with pytest.raises(RunEngineInterrupted):
            RE(fallback(evaluate))
        RE.abort()
        release.set()
        record = records.get(timeout=5)
    finally:
        release.set()
        logger.removeHandler(handler)
    assert record.levelno == logging.ERROR
    assert "ValueError: poll failed" in record.getMessage()


def test_navigate_single_objective(RE):
    """navigate_to_best moves actuators to the single best point."""
    optimizer = MagicMock(spec=Optimizer)
    optimizer.get_best_points.return_value = [(123, {"x1": 1.5, "x2": 2.5}, {"objective": 10.0})]
    x1 = MovableSignal("x1", initial_value=0.0)
    x2 = MovableSignal("x2", initial_value=0.0)

    RE(navigate_to_best([x1, x2], optimizer))

    assert x1._value == 1.5
    assert x2._value == 2.5


def test_navigate_explicit_parameterization(RE):
    """navigate_to_best with explicit parameterization skips optimizer query."""
    x1 = MovableSignal("x1", initial_value=0.0)

    RE(navigate_to_best([x1], parameterization={"x1": 3.0}))

    assert x1._value == 3.0


def test_navigate_raises_on_multiple_pareto_points(RE):
    """navigate_to_best raises ValueError when optimizer returns multiple Pareto points."""
    optimizer = MagicMock(spec=Optimizer)
    optimizer.get_best_points.return_value = [
        (1, {"x1": 1.0}, {"obj_a": 5.0, "obj_b": 2.0}),
        (5, {"x1": 2.0}, {"obj_a": 3.0, "obj_b": 4.0}),
        (100, {"x1": 3.0}, {"obj_a": 1.0, "obj_b": 6.0}),
    ]
    x1 = MovableSignal("x1", initial_value=0.0)

    with pytest.raises(ValueError, match="3 Pareto-optimal points"):
        RE(navigate_to_best([x1], optimizer))


def test_navigate_raises_on_missing_arguments(RE):
    """navigate_to_best raises TypeError if both parameterization and optimizer are None."""
    x1 = MovableSignal("x1", initial_value=0.0)

    with pytest.raises(TypeError, match="parameterization or use an optimizer"):
        RE(navigate_to_best([x1]))


def test_navigate_ignores_unknown_params(RE):
    """Parameterization keys not matching actuator names are ignored."""
    optimizer = MagicMock(spec=Optimizer)
    optimizer.get_best_points.return_value = [(1, {"x1": 5.0, "unknown_param": 99.0}, {"objective": 1.0})]
    x1 = MovableSignal("x1", initial_value=0.0)

    RE(navigate_to_best([x1], optimizer))

    assert x1._value == 5.0
