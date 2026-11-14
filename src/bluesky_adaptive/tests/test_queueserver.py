import itertools
import threading
from dataclasses import replace
from unittest.mock import MagicMock, call, patch

import pytest
from bluesky_queueserver_api import BPlan
from bluesky_queueserver_api.zmq import REManagerAPI

from bluesky_adaptive.protocols import CanRegisterSuggestions, Optimizer, QueueserverOptimizationProblem, TrialFaultAware
from bluesky_adaptive.queueserver import (
    OptimizationResult,
    QueueserverClient,
    QueueserverOptimizationRunner,
)

from .conftest import CheckpointableOptimizer


@pytest.fixture(scope="function")
def mock_optimization_problem():
    """Create a mock OptimizationProblem with necessary components."""
    mock_optimizer = MagicMock(spec=Optimizer)
    mock_optimizer.suggest.return_value = [
        {"_id": 0, "motor1": 5.0, "motor2": 3.0},
    ]

    mock_eval_func = MagicMock()
    mock_eval_func.return_value = [{"_id": 0, "objective": 1.0}]

    return QueueserverOptimizationProblem(
        optimizer=mock_optimizer,
        actuators=["motor1", "motor2"],
        sensors=["detector"],
        evaluation_function=mock_eval_func,
    )


@patch("bluesky_adaptive.queueserver.bluesky_queueserver_api.http.REManagerAPI")
def test_queueserver_client_check_environment_raises_when_not_ready(mock_re_manager):
    """Test check_environment raises RuntimeError when environment not open."""
    mock_re_manager.status.return_value = {"worker_environment_exists": False}
    client = QueueserverClient(mock_re_manager)

    with pytest.raises(RuntimeError, match="queueserver environment is not open"):
        client.check_environment()


@patch("bluesky_adaptive.queueserver.bluesky_queueserver_api.http.REManagerAPI")
def test_queueserver_client_construction_sends_no_requests(mock_re_manager):
    """Constructing a client leaves the shared queueserver state, including autostart, untouched."""
    QueueserverClient(mock_re_manager)

    assert mock_re_manager.method_calls == []


@patch("bluesky_adaptive.queueserver.bluesky_queueserver_api.http.REManagerAPI")
def test_queueserver_client_submit_plan_only_adds_item_and_returns_item_uid(mock_re_manager):
    """Test submit_plan adds the item without starting the queue or changing autostart, returning its item UID."""
    mock_re_manager.item_add.return_value = {"success": True, "item": {"item_uid": "item-1"}}
    client = QueueserverClient(mock_re_manager)
    mock_plan = MagicMock()

    assert client.submit_plan(mock_plan) == "item-1"

    assert mock_re_manager.method_calls == [call.item_add(mock_plan)]


@patch("bluesky_adaptive.queueserver.bluesky_queueserver_api.http.REManagerAPI")
def test_queueserver_client_submit_plan_propagates_item_add_rejection(mock_re_manager):
    """Test submit_plan propagates queueserver item_add rejection unchanged."""
    response = {
        "success": False,
        "msg": "Failed to add an item: plan rejected by RE Manager",
        "qsize": None,
        "item": None,
    }
    error = REManagerAPI.RequestFailedError(
        request={"method": "queue_item_add"},
        response=response,
    )
    mock_re_manager.item_add.side_effect = error
    client = QueueserverClient(mock_re_manager)
    plan = BPlan("count", ["det_forbidden"], num=1)

    with pytest.raises(REManagerAPI.RequestFailedError) as exc_info:
        client.submit_plan(plan)

    assert exc_info.value is error
    assert exc_info.value.response is response
    mock_re_manager.item_add.assert_called_once_with(plan)


def test_runner_run_validates_environment(mock_optimization_problem):
    """Test run() validates queueserver environment before starting."""
    mock_client = MagicMock(spec=QueueserverClient)
    mock_client.check_environment.side_effect = RuntimeError("not open")

    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )

    with pytest.raises(RuntimeError, match="not open"):
        runner.run(iterations=1)

    mock_client.check_environment.assert_called_once()


def test_runner_run_submits_suggestions_to_queueserver():
    """Test run() gets suggestions from optimizer and submits plan to queueserver."""
    mock_client = MagicMock(spec=QueueserverClient)
    mock_optimization_problem = QueueserverOptimizationProblem(
        optimizer=MagicMock(),
        actuators=["motor1"],
        sensors=["det"],
        evaluation_function=MagicMock(),
        acquisition_plan="my_acquire",
    )
    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )
    assert runner.optimization_problem == mock_optimization_problem

    future = runner.run(iterations=1, num_points=1)

    # Verify optimizer.suggest was called
    mock_optimization_problem.optimizer.suggest.assert_called_once_with(1)

    # Verify plan was submitted
    mock_client.submit_plan.assert_called_once()
    submitted_plan = mock_client.submit_plan.call_args[0][0]
    assert submitted_plan.name == "my_acquire"

    future.result(timeout=5)


def test_runner_leaves_acquisition_order_metadata_to_plan(mock_optimization_problem):
    """Let the plan that executes acquisition record its actual order."""
    mock_client = MagicMock(spec=QueueserverClient)
    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )

    runner.run(iterations=1, num_points=1)
    submitted_plan = mock_client.submit_plan.call_args[0][0]
    assert submitted_plan.kwargs["md"]["blop_suggestions"] == mock_optimization_problem.optimizer.suggest.return_value
    assert "blop_acquisition_order" not in submitted_plan.kwargs["md"]


def test_runner_run_passes_acquisition_plan_kwargs_to_bplan():
    """Test that acquisition_plan_kwargs are forwarded to the submitted BPlan."""
    mock_client = MagicMock(spec=QueueserverClient)
    mock_optimization_problem = QueueserverOptimizationProblem(
        optimizer=MagicMock(),
        actuators=["motor1"],
        sensors=["det"],
        evaluation_function=MagicMock(),
        acquisition_plan="my_acquire",
        acquisition_plan_kwargs={"exposure_time": 0.5, "num_frames": 10},
    )
    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )

    runner.run(iterations=1, num_points=1)

    submitted_plan = mock_client.submit_plan.call_args[0][0]
    assert submitted_plan.kwargs["exposure_time"] == 0.5
    assert submitted_plan.kwargs["num_frames"] == 10


def test_runner_run_twice_fails():
    """Test 2 calls to run() fails."""
    release = threading.Event()

    def evaluate(**kwargs):
        release.wait(timeout=5)
        return []

    mock_client = MagicMock(spec=QueueserverClient)
    mock_optimization_problem = QueueserverOptimizationProblem(
        optimizer=MagicMock(),
        actuators=["motor1"],
        sensors=["det"],
        evaluation_function=MagicMock(side_effect=evaluate),
        acquisition_plan="my_acquire",
    )
    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )
    assert runner.optimization_problem == mock_optimization_problem

    first_future = runner.run(iterations=1, num_points=1)

    with pytest.raises(RuntimeError, match="already running"):
        runner.run(iterations=1, num_points=1)

    with pytest.raises(RuntimeError, match="already running"):
        suggestions = [{"motor1": 5}]
        runner.submit_suggestions(suggestions)

    release.set()
    first_future.result(timeout=5)


def test_runner_stop_waits_for_in_flight_evaluation(mock_optimization_problem):
    """stop() submits nothing further and resolves the future once the in-flight evaluation is ingested."""
    evaluating = threading.Event()
    release = threading.Event()

    def evaluate(**kwargs):
        evaluating.set()
        assert release.wait(timeout=5)
        return [{"_id": 0, "objective": 1.0}]

    problem = replace(mock_optimization_problem, evaluation_function=MagicMock(side_effect=evaluate))
    runner, mock_client, future = _start_runner(problem, iterations=10)

    assert evaluating.wait(timeout=5)
    runner.stop()
    assert not future.done()
    release.set()
    result = future.result(timeout=5)

    assert result.iterations_completed == 1
    mock_client.submit_plan.assert_called_once()
    problem.optimizer.ingest.assert_called_once()


def test_runner_submit_suggestions_to_queueserver():
    """Test run() gets suggestions from optimizer and submits plan to queueserver."""
    mock_client = MagicMock(spec=QueueserverClient)

    class CustomOptimizer(Optimizer, CanRegisterSuggestions): ...

    mock_optimization_problem = QueueserverOptimizationProblem(
        optimizer=MagicMock(spec=CustomOptimizer),
        actuators=["motor1"],
        sensors=["det"],
        evaluation_function=MagicMock(),
        acquisition_plan="my_acquire",
    )
    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )

    suggestions = [{"motor1": 5}]
    future = runner.submit_suggestions(suggestions)

    # Verify optimizer.suggest was NOT called
    mock_optimization_problem.optimizer.suggest.assert_not_called()
    mock_optimization_problem.optimizer.register_suggestions.assert_called_once_with(suggestions)

    # Verify plan was submitted
    mock_client.submit_plan.assert_called_once()
    submitted_plan = mock_client.submit_plan.call_args[0][0]
    assert submitted_plan.name == "my_acquire"

    future.result(timeout=5)


def test_runner_submit_suggestions_register_fails():
    """Test run() gets suggestions from optimizer and submits plan to queueserver."""
    mock_client = MagicMock(spec=QueueserverClient)

    mock_optimization_problem = QueueserverOptimizationProblem(
        optimizer=MagicMock(spec=Optimizer),
        actuators=["motor1"],
        sensors=["det"],
        evaluation_function=MagicMock(),
        acquisition_plan="my_acquire",
    )
    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )

    suggestions = [{"motor1": 5}]
    with pytest.raises(ValueError, match="'_id'"):
        runner.submit_suggestions(suggestions)

    # Verify optimizer.suggest was NOT called
    mock_optimization_problem.optimizer.suggest.assert_not_called()


def test_runner_submit_suggestions_twice_fails():
    """Test 2 calls to submit_suggestions() fails."""
    release = threading.Event()

    def evaluate(**kwargs):
        release.wait(timeout=5)
        return []

    mock_client = MagicMock(spec=QueueserverClient)

    class CustomOptimizer(Optimizer, CanRegisterSuggestions): ...

    mock_optimization_problem = QueueserverOptimizationProblem(
        optimizer=MagicMock(spec=CustomOptimizer),
        actuators=["motor1"],
        sensors=["det"],
        evaluation_function=MagicMock(side_effect=evaluate),
        acquisition_plan="my_acquire",
    )
    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )

    suggestions = [{"motor1": 5}]
    first_future = runner.submit_suggestions(suggestions)

    with pytest.raises(RuntimeError, match="already running"):
        runner.submit_suggestions(suggestions)

    with pytest.raises(RuntimeError, match="already running"):
        runner.run(iterations=1)

    release.set()
    first_future.result(timeout=5)


def _start_runner(problem, iterations=3, num_points=1, checkpoint_interval=None):
    """Start a runner whose client returns queue item UIDs "item-0", "item-1", ..."""
    mock_client = MagicMock(spec=QueueserverClient)
    mock_client.submit_plan.side_effect = (f"item-{i}" for i in itertools.count())
    runner = QueueserverOptimizationRunner(optimization_problem=problem, queueserver_client=mock_client)
    future = runner.run(iterations=iterations, num_points=num_points, checkpoint_interval=checkpoint_interval)
    return runner, mock_client, future


def test_runner_run_full_cycle(mock_optimization_problem):
    """run() evaluates each submitted queue item UID, ingests its outcomes, and resubmits until done."""
    problem = mock_optimization_problem
    problem.optimizer.suggest.return_value = [
        {"_id": 0, "motor1": 5.0, "motor2": 3.0},
        {"_id": 1, "motor1": 6.0, "motor2": 4.0},
    ]
    outcomes = [{"_id": 0, "objective": 1.0}, {"_id": 1, "objective": 2.0}]
    problem.evaluation_function.return_value = outcomes

    runner, mock_client, future = _start_runner(problem, iterations=3, num_points=2)
    result = future.result(timeout=5)

    assert mock_client.submit_plan.call_count == 3
    assert [c.kwargs["uid"] for c in problem.evaluation_function.call_args_list] == ["item-0", "item-1", "item-2"]
    assert problem.optimizer.ingest.call_args_list == [call(outcomes)] * 3
    assert result == OptimizationResult(iterations_completed=3, num_points=2, uids=("item-0", "item-1", "item-2"))


def test_runner_private_method_calls_before_run(mock_optimization_problem):
    mock_client = MagicMock(spec=QueueserverClient)
    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )

    with pytest.raises(RuntimeError, match="run()"):
        runner._build_plan([{}])


def test_runner_error_in_evaluation_function_sets_future_exception(mock_optimization_problem):
    """Exception in evaluation_function stops the loop and stores the error in the future."""
    error = ValueError("bad data")
    mock_optimization_problem.evaluation_function.side_effect = error

    runner, mock_client, future = _start_runner(mock_optimization_problem)

    assert future.exception(timeout=5) is error


def test_runner_error_calls_register_failures_when_optimizer_supports_it():
    """register_failures is called on TrialFaultAware optimizers when an error occurs."""

    class FaultAwareOptimizer(Optimizer, TrialFaultAware): ...

    mock_optimization_problem = QueueserverOptimizationProblem(
        optimizer=MagicMock(spec=FaultAwareOptimizer),
        actuators=["motor1", "motor2"],
        sensors=["detector"],
        evaluation_function=MagicMock(side_effect=RuntimeError("boom")),
    )
    mock_optimization_problem.optimizer.suggest.return_value = [{"_id": 0, "motor1": 5.0, "motor2": 3.0}]

    runner, mock_client, future = _start_runner(mock_optimization_problem)

    assert future.exception(timeout=5) is not None
    mock_optimization_problem.optimizer.register_failures.assert_called_once()


def test_runner_register_failures_raises_original_error_preserved_in_future():
    """If register_failures() itself raises, the original acquisition error is still in the future."""

    class FaultAwareOptimizer(Optimizer, TrialFaultAware): ...

    acquisition_error = RuntimeError("evaluation failed")
    register_error = RuntimeError("register_failures exploded")

    mock_optimization_problem = QueueserverOptimizationProblem(
        optimizer=MagicMock(spec=FaultAwareOptimizer),
        actuators=["motor1"],
        sensors=["detector"],
        evaluation_function=MagicMock(side_effect=acquisition_error),
    )
    mock_optimization_problem.optimizer.suggest.return_value = [{"_id": 0, "motor1": 5.0}]
    mock_optimization_problem.optimizer.register_failures.side_effect = register_error

    runner, mock_client, future = _start_runner(mock_optimization_problem, iterations=1)

    # The original acquisition error is what the caller sees
    assert future.exception(timeout=5) is acquisition_error
    mock_optimization_problem.optimizer.register_failures.assert_called_once()


def test_runner_error_does_not_call_register_failures_when_optimizer_lacks_support(mock_optimization_problem):
    """register_failures is NOT called on optimizers that don't implement TrialFaultAware."""
    mock_optimization_problem.evaluation_function.side_effect = RuntimeError("boom")
    assert not isinstance(mock_optimization_problem.optimizer, TrialFaultAware)

    runner, mock_client, future = _start_runner(mock_optimization_problem)

    assert future.exception(timeout=5) is not None


def test_runner_error_in_ingest_sets_future_exception(mock_optimization_problem):
    """Exception in optimizer.ingest stops the loop and stores the error in the future."""
    error = RuntimeError("ingest failed")
    mock_optimization_problem.optimizer.ingest.side_effect = error

    runner, mock_client, future = _start_runner(mock_optimization_problem)

    assert future.exception(timeout=5) is error


@pytest.mark.parametrize(
    "outcomes",
    [[{"_id": 0, "objective": 1.0}, {"_id": 99, "objective": 2.0}], [{"_id": 0, "objective": 1.0}]],
    ids=["unknown-id", "missing-id"],
)
def test_runner_rejects_outcomes_that_do_not_match_suggestions(outcomes):
    """Mismatched outcomes fail the run and are not ingested."""

    class FaultAwareOptimizer(Optimizer, TrialFaultAware): ...

    suggestions = [{"_id": 0, "motor1": 5.0}, {"_id": 1, "motor1": 6.0}]
    problem = QueueserverOptimizationProblem(
        optimizer=MagicMock(spec=FaultAwareOptimizer),
        actuators=["motor1"],
        sensors=["detector"],
        evaluation_function=MagicMock(return_value=outcomes),
    )
    problem.optimizer.suggest.return_value = suggestions

    runner, mock_client, future = _start_runner(problem)

    with pytest.raises(ValueError, match="must contain the same IDs"):
        future.result(timeout=5)
    problem.optimizer.ingest.assert_not_called()
    problem.optimizer.register_failures.assert_called_once_with(suggestions)
    mock_client.submit_plan.assert_called_once()


@pytest.mark.parametrize(
    "suggestions",
    [[{"motor1": 5.0}], [{"_id": 0, "motor1": 5.0}, {"_id": 0, "motor1": 6.0}]],
    ids=["missing-id", "duplicate-id"],
)
def test_runner_run_does_not_submit_invalid_suggestions(mock_optimization_problem, suggestions):
    """run() does not submit invalid suggestions."""
    mock_optimization_problem.optimizer.suggest.return_value = suggestions
    mock_client = MagicMock(spec=QueueserverClient)
    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )

    with pytest.raises(ValueError, match="'_id'"):
        runner.run(iterations=1)

    mock_client.submit_plan.assert_not_called()


def test_runner_invalid_next_suggestions_fail_future_without_failing_ingested_trials():
    """Invalid next suggestions fail the run without failing ingested trials."""

    class FaultAwareOptimizer(Optimizer, TrialFaultAware): ...

    problem = QueueserverOptimizationProblem(
        optimizer=MagicMock(spec=FaultAwareOptimizer),
        actuators=["motor1"],
        sensors=["detector"],
        evaluation_function=MagicMock(return_value=[{"_id": 0, "objective": 1.0}]),
    )
    problem.optimizer.suggest.side_effect = [[{"_id": 0, "motor1": 5.0}], [{"motor1": 6.0}]]

    runner, mock_client, future = _start_runner(problem, iterations=2)

    with pytest.raises(ValueError, match="'_id'"):
        future.result(timeout=5)
    problem.optimizer.ingest.assert_called_once()
    problem.optimizer.register_failures.assert_not_called()
    mock_client.submit_plan.assert_called_once()


def test_runner_run_submit_error_fails_future_and_reraises(mock_optimization_problem):
    """An exception from submit_plan in run() fails the future and re-raises."""
    error = RuntimeError("connection refused")
    mock_client = MagicMock(spec=QueueserverClient)
    mock_client.submit_plan.side_effect = error

    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )

    with pytest.raises(RuntimeError, match="connection refused"):
        runner.run(iterations=1, num_points=1)

    future = runner._current_future
    assert future is not None
    assert future.done()
    assert future.exception() is error


def test_runner_submit_suggestions_submit_error_fails_future_and_reraises(mock_optimization_problem):
    """An exception from submit_plan in submit_suggestions() fails the future and re-raises."""
    error = RuntimeError("connection refused")
    mock_client = MagicMock(spec=QueueserverClient)
    mock_client.submit_plan.side_effect = error

    runner = QueueserverOptimizationRunner(
        optimization_problem=mock_optimization_problem,
        queueserver_client=mock_client,
    )

    suggestions = [{"_id": 0, "motor1": 1.0}]
    with pytest.raises(RuntimeError, match="connection refused"):
        runner.submit_suggestions(suggestions)

    future = runner._current_future
    assert future is not None
    assert future.done()
    assert future.exception() is error


def test_runner_checkpoints(mock_optimization_problem):
    """A checkpoint is taken for each iteration."""

    mock_optimizer = MagicMock(spec=CheckpointableOptimizer)
    mock_optimizer.suggest.return_value = [
        {"_id": 0, "motor1": 5.0, "motor2": 3.0},
    ]
    mock_optimization_problem = QueueserverOptimizationProblem(
        optimizer=mock_optimizer,
        actuators=mock_optimization_problem.actuators,
        sensors=mock_optimization_problem.sensors,
        evaluation_function=mock_optimization_problem.evaluation_function,
    )
    runner, mock_client, future = _start_runner(mock_optimization_problem, checkpoint_interval=1)
    future.result(timeout=5)
    assert mock_optimizer.checkpoint.call_count == 3


def test_runner_skip_checkpoints(mock_optimization_problem):
    """No checkpoint is taken for each iteration because no interval configured."""

    mock_optimizer = MagicMock(spec=CheckpointableOptimizer)
    mock_optimizer.suggest.return_value = [
        {"_id": 0, "motor1": 5.0, "motor2": 3.0},
    ]
    mock_optimization_problem = QueueserverOptimizationProblem(
        optimizer=mock_optimizer,
        actuators=mock_optimization_problem.actuators,
        sensors=mock_optimization_problem.sensors,
        evaluation_function=mock_optimization_problem.evaluation_function,
    )
    runner, mock_client, future = _start_runner(mock_optimization_problem, checkpoint_interval=None)
    future.result(timeout=5)
    mock_optimizer.checkpoint.assert_not_called()


def test_runner_raises_not_checkpointable(mock_optimization_problem):
    """No checkpoint is taken for each iteration because optimize does not support it."""
    runner, mock_client, future = _start_runner(mock_optimization_problem, checkpoint_interval=1)
    with pytest.raises(ValueError, match="optimizer is not checkpointable"):
        future.result(timeout=5)
