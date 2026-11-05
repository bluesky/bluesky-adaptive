from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from bluesky_adaptive.queueserver import CORRELATION_UID_KEY
from bluesky_adaptive.server.catalog import Catalog, ProblemTemplate
from bluesky_adaptive.server.errors import ConflictError, InvalidRequestError, NotFoundError, ServerError, UnavailableError
from bluesky_adaptive.server.manager import OptimizationManager
from bluesky_adaptive.server.models import (
    AgentConfig,
    AgentSpec,
    ExecutionStatus,
    IngestPoint,
    OptimizationRecord,
    OptimizationStatus,
    TrialRecord,
    TrialStatus,
)
from bluesky_adaptive.server.repository import InMemoryOptimizationRepository

from .conftest import ProblemParams, make_problem


def _agent(manager: OptimizationManager, name: str = "agent", **params: int):
    return manager.create_agent(AgentConfig(name=name, problem="fake", params=params or {"x": 2}))


def test_agent_validation_creation_update_and_deletion(manager, build_calls):
    invalid = pytest.raises(
        InvalidRequestError, manager.create_agent, AgentConfig(name="bad", problem="fake", params={"x": -1})
    )
    assert invalid.value.detail[0]["loc"] == ["x"]
    with pytest.raises(InvalidRequestError):
        manager.validate_agent(AgentSpec(problem="unknown"))

    agent = _agent(manager, x=2)
    assert agent.params == {"x": 2}
    assert len(build_calls) == 1
    with pytest.raises(ConflictError):
        _agent(manager)
    updated = manager.update_agent("agent", AgentSpec(problem="fake", params={"x": 3}))
    assert updated.created_at == agent.created_at
    manager.delete_agent("agent")
    with pytest.raises(NotFoundError):
        manager.get_agent("agent")


def test_start_stop_and_manual_operations(manager, build_calls):
    _agent(manager)
    optimization = manager.start_optimization("agent")
    assert len(build_calls) == 2
    with pytest.raises(ConflictError):
        manager.start_optimization("agent")

    suggested = manager.suggest(optimization.id, 2)
    completed = manager.evaluate(optimization.id, "run-1", [trial.suggestion_id for trial in suggested])
    assert [trial.status for trial in completed] == [TrialStatus.COMPLETED, TrialStatus.COMPLETED]
    assert all(trial.acquisition_uid == "run-1" for trial in completed)
    assert manager.best_points(optimization.id)[0].outcomes == {"score": 1}
    external = manager.ingest(optimization.id, [IngestPoint(parameters={"x": 10}, outcomes={"score": 10})])
    assert external[0].suggestion_id is None
    assert external[0].status is TrialStatus.COMPLETED

    stopped = manager.stop(optimization.id)
    assert stopped.status is OptimizationStatus.STOPPED
    assert len(build_calls) == 2
    manager.start_optimization("agent")
    assert len(build_calls) == 3


def test_invalid_evaluation_does_not_ingest(manager):
    _agent(manager)
    optimization = manager.start_optimization("agent")
    suggested = manager.suggest(optimization.id, 1)
    session = manager._sessions[optimization.id]
    session.problem = session.problem.__class__(
        optimizer=session.problem.optimizer,
        actuators=[],
        sensors=[],
        evaluation_function=lambda **kwargs: [{"_id": 999, "score": 1}],
    )
    with pytest.raises(ServerError):
        manager.evaluate(optimization.id, "run", [suggested[0].suggestion_id])
    assert session.problem.optimizer.ingested == []
    with pytest.raises(InvalidRequestError):
        manager.evaluate(optimization.id, "run", [999])
    assert manager.stop(optimization.id).status is OptimizationStatus.STOPPED
    assert manager.list_trials(optimization.id)[0].status is TrialStatus.ABANDONED


def test_stop_waits_for_a_manual_operation_in_progress(manager):
    _agent(manager)
    optimization = manager.start_optimization("agent")
    suggested = manager.suggest(optimization.id, 1)
    session = manager._sessions[optimization.id]
    entered, release = threading.Event(), threading.Event()

    def slow_evaluation(uid, suggestions):
        entered.set()
        release.wait(timeout=5)
        return [{"_id": suggestion["_id"], "score": 1} for suggestion in suggestions]

    session.problem = session.problem.__class__(
        optimizer=session.problem.optimizer, actuators=[], sensors=[], evaluation_function=slow_evaluation
    )
    results = {}
    evaluating = threading.Thread(
        target=lambda: results.setdefault("evaluate", manager.evaluate(optimization.id, "run", None))
    )
    evaluating.start()
    assert entered.wait(timeout=5)
    stopping = threading.Thread(target=lambda: results.setdefault("stop", manager.stop(optimization.id)))
    stopping.start()
    stopping.join(timeout=0.1)
    assert stopping.is_alive()  # blocked behind the evaluation, not ending the optimization underneath it
    release.set()
    evaluating.join(timeout=5)
    stopping.join(timeout=5)

    assert [trial.status for trial in results["evaluate"]] == [TrialStatus.COMPLETED]
    assert results["stop"].status is OptimizationStatus.STOPPED
    assert [trial.status for trial in manager.list_trials(optimization.id)] == [TrialStatus.COMPLETED]
    assert suggested[0].suggestion_id == results["evaluate"][0].suggestion_id


def test_queueserver_run_lifecycle_and_stop(qmanager, queueserver):
    qmanager.create_agent(AgentConfig(name="queue", problem="queue", params={"x": 1}))
    optimization = qmanager.start_optimization("queue")
    qmanager.run(optimization.id, iterations=2, num_points=1)
    callback = queueserver.subscribe.call_args.args[0]
    assert queueserver.submit_plan.call_count == 1
    first_plan = queueserver.submit_plan.call_args.args[0]
    first_correlation = first_plan.kwargs["md"][CORRELATION_UID_KEY]
    callback({"uid": "run-1", CORRELATION_UID_KEY: first_correlation}, {"run_start": "run-1", "exit_status": "success"})
    second_plan = queueserver.submit_plan.call_args.args[0]
    second_correlation = second_plan.kwargs["md"][CORRELATION_UID_KEY]
    callback({"uid": "run-2", CORRELATION_UID_KEY: second_correlation}, {"run_start": "run-2", "exit_status": "success"})

    record = qmanager.get_optimization(optimization.id)
    assert record.status is OptimizationStatus.ACTIVE
    assert record.executions[0].status is ExecutionStatus.COMPLETED
    assert record.executions[0].iterations_completed == 2
    assert [run.run_uid for run in qmanager.list_runs(optimization.id)] == ["run-1", "run-2"]
    assert all(trial.status is TrialStatus.COMPLETED for trial in qmanager.list_trials(optimization.id))
    assert {event.type for event in qmanager.events._history} == {"optimization", "execution", "trial"}

    qmanager.run(optimization.id, iterations=2, num_points=1)
    with pytest.raises(ConflictError):
        qmanager.suggest(optimization.id, 1)
    correlation = queueserver.submit_plan.call_args.args[0].kwargs["md"][CORRELATION_UID_KEY]
    assert qmanager.stop(optimization.id).status is OptimizationStatus.STOPPING
    callback({"uid": "run-3", CORRELATION_UID_KEY: correlation}, {"run_start": "run-3", "exit_status": "success"})
    assert qmanager.get_optimization(optimization.id).status is OptimizationStatus.STOPPED
    assert queueserver.submit_plan.call_count == 3


def test_queueserver_failure_and_acquire_existing_trial(qmanager, queueserver):
    qmanager.create_agent(AgentConfig(name="queue", problem="queue", params={"x": 1}))
    optimization = qmanager.start_optimization("queue")
    suggested = qmanager.suggest(optimization.id, 1)
    qmanager.acquire(optimization.id, suggestion_ids=[suggested[0].suggestion_id])
    callback = queueserver.subscribe.call_args.args[0]
    correlation = queueserver.submit_plan.call_args.args[0].kwargs["md"][CORRELATION_UID_KEY]
    callback({"uid": "bad", CORRELATION_UID_KEY: correlation}, {"run_start": "bad", "exit_status": "fail"})
    record = qmanager.get_optimization(optimization.id)
    assert record.status is OptimizationStatus.ACTIVE
    assert record.executions[-1].status is ExecutionStatus.FAILED
    assert qmanager.list_trials(optimization.id)[0].status is TrialStatus.FAILED


def test_queueserver_restrictions_and_lifecycle(manager, qmanager):
    _agent(manager)
    optimization = manager.start_optimization("agent")
    with pytest.raises(UnavailableError):
        manager.run(optimization.id, 1, 1)

    qmanager.create_agent(AgentConfig(name="queue", problem="queue", params={"x": 1}))
    queue_optimization = qmanager.start_optimization("queue")
    qmanager.shutdown()
    assert qmanager.get_optimization(queue_optimization.id).status is OptimizationStatus.INTERRUPTED

    repository = InMemoryOptimizationRepository()
    old = OptimizationRecord(
        id="old",
        agent="agent",
        config=AgentConfig(name="agent", problem="fake", params={"x": 1}),
        status=OptimizationStatus.RUNNING,
        created_at=datetime.now(UTC),
    )
    repository.save_optimization(old)
    repository.save_trials(
        "old",
        [
            TrialRecord(
                number=0,
                suggestion_id=1,
                status=TrialStatus.SUGGESTED,
                parameters={"x": 1},
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        ],
    )
    recovered = OptimizationManager(Catalog([ProblemTemplate("fake", ProblemParams, make_problem)]), repository)
    recovered.startup()
    assert recovered.get_optimization("old").status is OptimizationStatus.INTERRUPTED
    assert recovered.list_trials("old")[0].status is TrialStatus.ABANDONED
