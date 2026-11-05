from unittest.mock import MagicMock, patch

import pytest
from bluesky.callbacks.zmq import RemoteDispatcher
from bluesky_queueserver_api import BPlan
from bluesky_queueserver_api.comm_base import RequestFailedError
from bluesky_queueserver_api.zmq import REManagerAPI

from bluesky_adaptive.protocols import (
    CanRegisterSuggestions,
    Optimizer,
    QueueserverOptimizationProblem,
    TrialFaultAware,
)
from bluesky_adaptive.queueserver import (
    CORRELATION_UID_KEY,
    AcquisitionRequest,
    ConsumerCallback,
    IterationRecord,
    OptimizationResult,
    QueueItemResult,
    QueueserverClient,
    QueueserverOptimizationRunner,
)

from .conftest import CheckpointableOptimizer


class RegisteringOptimizer(Optimizer, CanRegisterSuggestions): ...


class FaultAwareOptimizer(Optimizer, TrialFaultAware): ...


@pytest.fixture
def mock_document_dispatcher():
    return MagicMock(spec=RemoteDispatcher)


@pytest.fixture
def mock_client():
    client = MagicMock(spec=QueueserverClient)
    client.submit_plan.return_value = "item-0"
    client.finished_item.return_value = None
    client.remove_item.return_value = False
    client.status.return_value = {"plan_history_uid": "history-0"}
    return client


@pytest.fixture
def mock_optimization_problem():
    optimizer = MagicMock(spec=Optimizer)
    optimizer.suggest.return_value = [{"_id": 0, "motor": 5.0}]
    evaluation = MagicMock(return_value=[{"_id": 0, "objective": 1.0}])
    return QueueserverOptimizationProblem(
        optimizer=optimizer,
        actuators=["motor"],
        sensors=["detector"],
        evaluation_function=evaluation,
        acquisition_plan="acquire",
    )


def _runner(problem, client, **kwargs):
    return QueueserverOptimizationRunner(problem, client, history_poll_interval=None, **kwargs)


def _complete(runner, run_uid="run-0", *, exit_status="success", reason=""):
    in_flight = runner._state.in_flight
    assert in_flight is not None
    runner._on_acquisition_complete(
        {"uid": run_uid, CORRELATION_UID_KEY: in_flight.correlation_uid},
        {"uid": f"stop-{run_uid}", "run_start": run_uid, "exit_status": exit_status, "reason": reason},
    )


def test_consumer_callback_fans_out_and_isolates_failing_subscriber():
    callback = ConsumerCallback()
    failing = MagicMock(side_effect=RuntimeError("broken subscriber"))
    succeeding = MagicMock()
    callback.add(failing)
    callback.add(succeeding)
    start = {"uid": "run-1", CORRELATION_UID_KEY: "correlation"}
    stop = {"uid": "stop-1", "run_start": "run-1"}

    callback.start(start)
    callback.stop(stop)

    failing.assert_called_once_with(start, stop)
    succeeding.assert_called_once_with(start, stop)
    assert callback._start_doc_cache == {}


def test_consumer_callback_rejects_duplicate_and_ignores_uncorrelated_runs():
    callback = ConsumerCallback()
    subscriber = MagicMock()
    callback.add(subscriber)
    with pytest.raises(ValueError, match="already subscribed"):
        callback.add(subscriber)

    callback.start({"uid": "run-1"})
    callback.stop({"uid": "stop-1", "run_start": "run-1"})
    callback.remove(subscriber)
    assert callback._subscribers == ()
    subscriber.assert_not_called()


def test_queueserver_client_submit_plan_returns_item_uid(mock_document_dispatcher):
    manager = MagicMock(spec=REManagerAPI)
    manager.item_add.return_value = {"success": True, "item": {"item_uid": "item-1"}}
    client = QueueserverClient(manager, mock_document_dispatcher)
    plan = BPlan("count", ["det"], num=1)

    assert client.submit_plan(plan) == "item-1"
    manager.item_add.assert_called_once_with(plan)


def test_queueserver_client_preserves_request_failed_error(mock_document_dispatcher):
    manager = MagicMock(spec=REManagerAPI)
    error = RequestFailedError(request={}, response={"msg": "rejected"})
    manager.item_add.side_effect = error
    client = QueueserverClient(manager, mock_document_dispatcher)

    with pytest.raises(RequestFailedError) as caught:
        client.submit_plan(BPlan("count", ["det"], num=1))

    assert caught.value is error


@pytest.mark.parametrize("response", [{"success": False, "msg": "nope"}, {"success": True, "item": {}}])
def test_queueserver_client_submit_plan_rejects_unsuccessful_or_uidless_response(mock_document_dispatcher, response):
    manager = MagicMock(spec=REManagerAPI)
    manager.item_add.return_value = response
    client = QueueserverClient(manager, mock_document_dispatcher)

    with pytest.raises(RuntimeError, match="rejected plan"):
        client.submit_plan(BPlan("count", ["det"], num=1))


def test_queueserver_client_subscribe_starts_listener_once_and_close_stops_it(mock_document_dispatcher):
    manager = MagicMock(spec=REManagerAPI)
    client = QueueserverClient(manager, mock_document_dispatcher)
    first, second = MagicMock(), MagicMock()

    with patch("bluesky_adaptive.queueserver.threading.Thread") as thread:
        client.subscribe(first)
        client.subscribe(second)
        client.close()

    mock_document_dispatcher.subscribe.assert_called_once_with(client._consumer_callback)
    thread.assert_called_once_with(target=mock_document_dispatcher.start, name="qserver-document-consumer", daemon=True)
    thread.return_value.start.assert_called_once()
    mock_document_dispatcher.stop.assert_called_once()


def test_queueserver_client_finished_item_and_remove_item(mock_document_dispatcher):
    manager = MagicMock(spec=REManagerAPI)
    manager.history_get.return_value = {
        "items": [{"item_uid": "item-1", "result": {"exit_status": "completed", "run_uids": ["run-1"]}}]
    }
    client = QueueserverClient(manager, mock_document_dispatcher)

    assert client.finished_item("item-1") == QueueItemResult("item-1", "completed", ("run-1",), "")
    manager.item_remove.side_effect = RequestFailedError(request={}, response={})
    assert not client.remove_item("item-1")


def test_runner_subscribes_on_init_and_unsubscribes_on_close(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)

    mock_client.subscribe.assert_called_once_with(runner._on_acquisition_complete)
    runner.close()
    mock_client.unsubscribe.assert_called_once_with(runner._on_acquisition_complete)


def test_runner_submits_plan_with_in_flight_metadata(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)
    future = runner.run(iterations=1, num_points=1)

    plan = mock_client.submit_plan.call_args.args[0]
    assert plan.name == "acquire"
    assert plan.kwargs["md"]["blop_suggestions"] == mock_optimization_problem.optimizer.suggest.return_value
    assert plan.kwargs["md"][CORRELATION_UID_KEY] == runner._state.in_flight.correlation_uid
    assert not future.done()


def test_runner_ignores_other_correlated_runs(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)
    future = runner.run()

    runner._on_acquisition_complete(
        {"uid": "other", CORRELATION_UID_KEY: "someone-else"},
        {"uid": "stop-other", "run_start": "other", "exit_status": "success"},
    )

    assert not future.done()
    mock_optimization_problem.evaluation_function.assert_not_called()
    mock_optimization_problem.optimizer.ingest.assert_not_called()


def test_runner_hooks_run_in_submission_evaluation_ingest_iteration_checkpoint_order(mock_client):
    optimizer = MagicMock(spec=CheckpointableOptimizer)
    optimizer.suggest.return_value = [{"_id": 0, "motor": 5.0}]
    order = []
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer,
        actuators=["motor"],
        sensors=["detector"],
        evaluation_function=MagicMock(return_value=[{"_id": 0, "objective": 1.0}]),
    )
    optimizer.ingest.side_effect = lambda outcomes: order.append("ingest")
    optimizer.checkpoint.side_effect = lambda: order.append("checkpoint")
    runner = _runner(
        problem,
        mock_client,
        on_submit=lambda request: order.append(("submit", request)),
        on_evaluation=lambda record: order.append(("evaluation", record)),
        on_iteration=lambda record: order.append(("iteration", record)),
    )

    future = runner.run(checkpoint_interval=1)
    _complete(runner)

    assert future.result().uids == ("run-0",)
    assert isinstance(order[0][1], AcquisitionRequest)
    assert isinstance(order[1][1], IterationRecord)
    assert order[2:] == ["ingest", ("iteration", order[1][1]), "checkpoint"]


def test_runner_reports_ingested_iteration_before_a_failing_checkpoint(mock_client):
    class FaultAwareCheckpointableOptimizer(CheckpointableOptimizer, TrialFaultAware): ...

    error = OSError("disk full")
    optimizer = MagicMock(spec=FaultAwareCheckpointableOptimizer)
    optimizer.suggest.return_value = [{"_id": 0, "motor": 5.0}]
    optimizer.checkpoint.side_effect = error
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer,
        actuators=["motor"],
        sensors=["detector"],
        evaluation_function=MagicMock(return_value=[{"_id": 0, "objective": 1.0}]),
    )
    iterations = []
    runner = _runner(problem, mock_client, on_iteration=iterations.append)

    future = runner.run(iterations=3, checkpoint_interval=1)
    _complete(runner)

    assert [record.run_uid for record in iterations] == ["run-0"]
    assert future.exception() is error
    optimizer.register_failures.assert_not_called()
    assert mock_client.submit_plan.call_count == 1


def test_runner_evaluation_hook_failure_registers_failure_without_ingesting(mock_client):
    error = RuntimeError("save failed")
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    optimizer.suggest.return_value = [{"_id": 0, "motor": 5.0}]
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer, actuators=["motor"], sensors=["detector"], evaluation_function=MagicMock(return_value=[])
    )
    runner = _runner(problem, mock_client, on_evaluation=MagicMock(side_effect=error))
    future = runner.run()
    _complete(runner)

    assert future.exception() is error
    optimizer.ingest.assert_not_called()
    optimizer.register_failures.assert_called_once_with(optimizer.suggest.return_value)


def test_runner_iteration_hook_failure_does_not_register_failure(mock_client):
    error = RuntimeError("post-ingest failed")
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    optimizer.suggest.return_value = [{"_id": 0, "motor": 5.0}]
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer, actuators=["motor"], sensors=["detector"], evaluation_function=MagicMock(return_value=[])
    )
    runner = _runner(problem, mock_client, on_iteration=MagicMock(side_effect=error))
    future = runner.run()
    _complete(runner)

    assert future.exception() is error
    optimizer.ingest.assert_called_once()
    optimizer.register_failures.assert_not_called()


def test_runner_rejected_submission_registers_failures_and_reraises(mock_client):
    error = RuntimeError("rejected")
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    suggestions = [{"_id": 0, "motor": 5.0}]
    optimizer.suggest.return_value = suggestions
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer, actuators=["motor"], sensors=["detector"], evaluation_function=MagicMock()
    )
    mock_client.submit_plan.side_effect = error
    runner = _runner(problem, mock_client)

    with pytest.raises(RuntimeError, match="rejected"):
        runner.run()

    assert runner._state.future.exception() is error
    optimizer.register_failures.assert_called_once_with(suggestions)


def test_runner_register_failures_error_is_note_on_original_error(mock_client):
    acquisition_error = RuntimeError("evaluation failed")
    register_error = RuntimeError("register failed")
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    optimizer.suggest.return_value = [{"_id": 0, "motor": 5.0}]
    optimizer.register_failures.side_effect = register_error
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer,
        actuators=["motor"],
        sensors=["detector"],
        evaluation_function=MagicMock(side_effect=acquisition_error),
    )
    runner = _runner(problem, mock_client)
    future = runner.run()
    _complete(runner)

    assert future.exception() is acquisition_error
    assert "register failed" in acquisition_error.__notes__[0]


def test_submit_suggestions_only_registers_unidentified_suggestions(mock_client):
    optimizer = MagicMock(spec=RegisteringOptimizer)
    registered = {"_id": "new", "motor": 2.0}
    optimizer.register_suggestions.return_value = [registered]
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer, actuators=["motor"], sensors=["detector"], evaluation_function=MagicMock()
    )
    known = {"_id": "known", "motor": 1.0}
    runner = _runner(problem, mock_client)

    runner.submit_suggestions([known, {"motor": 2.0}])

    optimizer.register_suggestions.assert_called_once_with([{"motor": 2.0}])
    assert mock_client.submit_plan.call_args.args[0].args[0] == [known, registered]


def test_stop_removes_unclaimed_queue_item_and_resolves(mock_client):
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    suggestions = [{"_id": 0, "motor": 5.0}]
    optimizer.suggest.return_value = suggestions
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer, actuators=["motor"], sensors=["detector"], evaluation_function=MagicMock()
    )
    mock_client.remove_item.return_value = True
    runner = _runner(problem, mock_client)
    future = runner.run(iterations=2)

    runner.stop()

    assert future.result() == OptimizationResult(0, 1, ())
    mock_client.remove_item.assert_called_once_with("item-0")
    optimizer.register_failures.assert_called_once_with(suggestions)


def test_stop_waits_for_ingest_without_submitting_another_plan(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)
    future = runner.run(iterations=2)

    runner.stop()
    assert not future.done()
    _complete(runner)

    assert future.result() == OptimizationResult(1, 1, ("run-0",))
    assert mock_client.submit_plan.call_count == 1


def test_watchdog_fails_item_that_opened_no_run(mock_client):
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    suggestions = [{"_id": 0, "motor": 5.0}]
    optimizer.suggest.return_value = suggestions
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer, actuators=["motor"], sensors=["detector"], evaluation_function=MagicMock()
    )
    runner = _runner(problem, mock_client)
    future = runner.run()
    mock_client.finished_item.return_value = QueueItemResult("item-0", "failed", (), "bad plan")

    runner._check_in_flight(runner._state)

    assert "without opening a run" in str(future.exception())
    optimizer.register_failures.assert_called_once_with(suggestions)


def test_watchdog_fails_after_stop_document_timeout(mock_client):
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    optimizer.suggest.return_value = [{"_id": 0, "motor": 5.0}]
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer, actuators=["motor"], sensors=["detector"], evaluation_function=MagicMock()
    )
    runner = _runner(problem, mock_client, document_timeout=2.0)
    future = runner.run()
    mock_client.finished_item.return_value = QueueItemResult("item-0", "completed", ("run-0",), "")

    with patch("bluesky_adaptive.queueserver.time.monotonic", side_effect=[10.0, 13.0]):
        runner._check_in_flight(runner._state)
        runner._check_in_flight(runner._state)

    assert "no stop document" in str(future.exception())


def test_watchdog_only_fetches_history_after_history_uid_changes(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)
    runner.run()
    mock_client.status.return_value = {"plan_history_uid": "same"}

    runner._check_in_flight(runner._state)
    runner._check_in_flight(runner._state)

    mock_client.finished_item.assert_called_once_with("item-0")


def test_queueserver_client_check_environment_raises_when_not_ready(mock_document_dispatcher):
    manager = MagicMock(spec=REManagerAPI)
    manager.status.return_value = {"worker_environment_exists": False}
    client = QueueserverClient(manager, mock_document_dispatcher)
    with pytest.raises(RuntimeError, match="environment is not open"):
        client.check_environment()


@pytest.mark.parametrize("autostart", [True, False])
def test_queueserver_client_configures_requested_autostart(mock_document_dispatcher, autostart):
    manager = MagicMock(spec=REManagerAPI)
    QueueserverClient(manager, mock_document_dispatcher, autostart=autostart)
    manager.queue_autostart.assert_called_once_with(autostart)


def test_runner_run_validates_environment(mock_optimization_problem, mock_client):
    mock_client.check_environment.side_effect = RuntimeError("not open")
    runner = _runner(mock_optimization_problem, mock_client)
    with pytest.raises(RuntimeError, match="not open"):
        runner.run()
    mock_client.check_environment.assert_called_once_with()


def test_runner_leaves_acquisition_order_metadata_to_plan(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)
    runner.run()
    assert "blop_acquisition_order" not in mock_client.submit_plan.call_args.args[0].kwargs["md"]


def test_runner_passes_acquisition_plan_kwargs_to_bplan(mock_client):
    problem = QueueserverOptimizationProblem(
        optimizer=MagicMock(spec=Optimizer),
        actuators=["motor"],
        sensors=["detector"],
        evaluation_function=MagicMock(),
        acquisition_plan_kwargs={"exposure_time": 0.5, "num_frames": 10},
    )
    problem.optimizer.suggest.return_value = [{"_id": 0, "motor": 5.0}]
    runner = _runner(problem, mock_client)
    runner.run()
    plan = mock_client.submit_plan.call_args.args[0]
    assert plan.kwargs["exposure_time"] == 0.5
    assert plan.kwargs["num_frames"] == 10


def test_runner_run_and_submit_suggestions_reject_second_in_flight_request(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)
    runner.run()
    with pytest.raises(RuntimeError, match="already running"):
        runner.run()
    with pytest.raises(RuntimeError, match="already running"):
        runner.submit_suggestions([{"_id": "manual", "motor": 1.0}])


def test_runner_submit_suggestions_registers_and_submits(mock_client):
    optimizer = MagicMock(spec=RegisteringOptimizer)
    optimizer.register_suggestions.return_value = [{"_id": "registered", "motor": 5.0}]
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer, actuators=["motor"], sensors=["detector"], evaluation_function=MagicMock()
    )
    runner = _runner(problem, mock_client)
    future = runner.submit_suggestions([{"motor": 5.0}])
    optimizer.suggest.assert_not_called()
    optimizer.register_suggestions.assert_called_once_with([{"motor": 5.0}])
    assert mock_client.submit_plan.call_args.args[0].args[0] == [{"_id": "registered", "motor": 5.0}]
    assert not future.done()


def test_runner_submit_suggestions_requires_registered_ids_without_registrar(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)
    with pytest.raises(ValueError, match="'_id'"):
        runner.submit_suggestions([{"motor": 5.0}])
    mock_optimization_problem.optimizer.suggest.assert_not_called()


def test_runner_submit_suggestions_rejects_second_request(mock_client):
    optimizer = MagicMock(spec=RegisteringOptimizer)
    optimizer.register_suggestions.return_value = [{"_id": "registered", "motor": 5.0}]
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer, actuators=["motor"], sensors=["detector"], evaluation_function=MagicMock()
    )
    runner = _runner(problem, mock_client)
    runner.submit_suggestions([{"motor": 5.0}])
    with pytest.raises(RuntimeError, match="already running"):
        runner.submit_suggestions([{"motor": 5.0}])
    with pytest.raises(RuntimeError, match="already running"):
        runner.run()


def test_runner_run_full_cycle(mock_optimization_problem, mock_client):
    mock_optimization_problem.optimizer.suggest.return_value = [{"_id": 0, "motor": 5.0}, {"_id": 1, "motor": 6.0}]
    mock_optimization_problem.evaluation_function.return_value = [{"_id": 0, "objective": 1.0}, {"_id": 1, "objective": 2.0}]
    runner = _runner(mock_optimization_problem, mock_client)
    future = runner.run(iterations=3, num_points=2)
    for index in range(3):
        _complete(runner, f"run-{index}")
    assert future.result() == OptimizationResult(3, 2, ("run-0", "run-1", "run-2"))
    assert mock_client.submit_plan.call_count == 3
    assert mock_optimization_problem.optimizer.suggest.call_count == 3
    assert mock_optimization_problem.optimizer.ingest.call_count == 3
    assert mock_optimization_problem.evaluation_function.call_count == 3


def test_runner_evaluation_and_ingest_errors_set_future_exception(mock_optimization_problem, mock_client):
    for method, error in [("evaluation_function", ValueError("bad data")), ("ingest", RuntimeError("ingest failed"))]:
        if method == "evaluation_function":
            mock_optimization_problem.evaluation_function.side_effect = error
        else:
            mock_optimization_problem.optimizer.ingest.side_effect = error
        runner = _runner(mock_optimization_problem, mock_client)
        future = runner.run()
        _complete(runner)
        assert future.exception() is error
        mock_optimization_problem.evaluation_function.reset_mock(side_effect=True)
        mock_optimization_problem.optimizer.ingest.reset_mock(side_effect=True)


def test_runner_evaluation_error_registers_failures_only_for_fault_aware(mock_client):
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    suggestions = [{"_id": 0, "motor": 5.0}]
    optimizer.suggest.return_value = suggestions
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer,
        actuators=["motor"],
        sensors=["detector"],
        evaluation_function=MagicMock(side_effect=RuntimeError("boom")),
    )
    runner = _runner(problem, mock_client)
    future = runner.run()
    _complete(runner)
    assert future.exception() is not None
    optimizer.register_failures.assert_called_once_with(suggestions)


def test_runner_success_resolves_to_optimization_result(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)
    future = runner.run()
    _complete(runner)
    assert future.result() == OptimizationResult(1, 1, ("run-0",))


@pytest.mark.parametrize("exit_status", ["fail", "abort"])
def test_runner_plan_failure_sets_exception_and_registers_failure(mock_client, exit_status):
    optimizer = MagicMock(spec=FaultAwareOptimizer)
    suggestions = [{"_id": 0, "motor": 5.0}]
    optimizer.suggest.return_value = suggestions
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer, actuators=["motor"], sensors=["detector"], evaluation_function=MagicMock()
    )
    runner = _runner(problem, mock_client)
    future = runner.run()
    _complete(runner, exit_status=exit_status, reason="hardware fault")
    assert exit_status in str(future.exception())
    assert "hardware fault" in str(future.exception())
    optimizer.register_failures.assert_called_once_with(suggestions)


def test_runner_stop_after_final_callback_does_not_raise(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)
    future = runner.run()
    _complete(runner)
    runner.stop()
    assert future.done()


def test_runner_subscribe_error_reraises(mock_optimization_problem, mock_client):
    mock_client.subscribe.side_effect = RuntimeError("connection refused")
    with pytest.raises(RuntimeError, match="connection refused"):
        _runner(mock_optimization_problem, mock_client)


@pytest.mark.parametrize("method", ["run", "submit_suggestions"])
def test_runner_submission_error_fails_future_and_reraises(mock_optimization_problem, mock_client, method):
    error = RuntimeError("connection refused")
    mock_client.submit_plan.side_effect = error
    runner = _runner(mock_optimization_problem, mock_client)
    with pytest.raises(RuntimeError, match="connection refused"):
        if method == "run":
            runner.run()
        else:
            runner.submit_suggestions([{"_id": 0, "motor": 1.0}])
    assert runner._state.future.exception() is error


@pytest.mark.parametrize("checkpoint_interval, expected", [(1, 3), (None, 0)])
def test_runner_checkpoint_behavior(mock_optimization_problem, mock_client, checkpoint_interval, expected):
    optimizer = MagicMock(spec=CheckpointableOptimizer)
    optimizer.suggest.return_value = [{"_id": 0, "motor": 5.0}]
    problem = QueueserverOptimizationProblem(
        optimizer=optimizer, actuators=["motor"], sensors=["detector"], evaluation_function=MagicMock(return_value=[])
    )
    runner = _runner(problem, mock_client)
    future = runner.run(iterations=3, checkpoint_interval=checkpoint_interval)
    for index in range(3):
        _complete(runner, f"run-{index}")
    assert future.done()
    assert optimizer.checkpoint.call_count == expected


def test_runner_fails_when_checkpoint_interval_requires_unsupported_optimizer(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)
    future = runner.run(checkpoint_interval=1)
    _complete(runner)
    assert "not checkpointable" in str(future.exception())


def test_stop_without_in_flight_resolves_partial_result(mock_optimization_problem, mock_client):
    runner = _runner(mock_optimization_problem, mock_client)
    future = runner.run(iterations=2)
    runner._state.in_flight = None
    runner._state.uids.append("already-ingested")
    runner.stop()
    assert future.result() == OptimizationResult(1, 1, ("already-ingested",))


@pytest.mark.parametrize("exit_status", ["fail", "abort"])
def test_runner_plan_failure_does_not_register_without_fault_support(mock_optimization_problem, mock_client, exit_status):
    runner = _runner(mock_optimization_problem, mock_client)
    future = runner.run()
    _complete(runner, exit_status=exit_status, reason="hardware fault")
    assert isinstance(future.exception(), RuntimeError)
    assert not isinstance(mock_optimization_problem.optimizer, TrialFaultAware)


def test_runner_evaluation_error_does_not_register_without_fault_support(mock_optimization_problem, mock_client):
    mock_optimization_problem.evaluation_function.side_effect = RuntimeError("boom")
    runner = _runner(mock_optimization_problem, mock_client)
    future = runner.run()
    _complete(runner)
    assert future.exception() is not None
    assert not isinstance(mock_optimization_problem.optimizer, TrialFaultAware)
