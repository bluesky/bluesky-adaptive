import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from bluesky_queueserver_api.comm_base import RequestFailedError
from bluesky_queueserver_api.zmq import REManagerAPI

from bluesky_adaptive.protocols import QueueserverOptimizationProblem
from bluesky_adaptive.queueserver import (
    OptimizationResult,
    QueueserverClient,
    QueueserverOptimizationRunner,
)
from bluesky_adaptive.runs import CORRELATION_UID_KEY, RunRouter

from .conftest import FakeFeed, wait_until

# Fakes


class FakeQueueserver:
    """A mock REManagerAPI whose queue and history the test drives."""

    def __init__(self, feed: FakeFeed):
        self.feed = feed
        self.plans = []
        self.history = []
        self.on_item_add = None
        self.rm = MagicMock(spec=REManagerAPI)
        self.rm.status.return_value = {"worker_environment_exists": True}
        self.rm.item_add.side_effect = self._item_add
        self.rm.history_get.side_effect = lambda: {"items": list(self.history)}
        self.rm.item_remove.side_effect = RequestFailedError({}, {"success": False, "msg": "item is running"})

    def _item_add(self, plan):
        self.plans.append(plan)
        item_uid = f"item-{len(self.plans)}"
        if self.on_item_add is not None:
            self.on_item_add(plan)
        return {"success": True, "item": {"item_uid": item_uid}}

    def md(self, index=-1):
        return self.plans[index].kwargs["md"]

    def finish(self, run_uid, exit_status="success", reason=None, index=-1):
        """Emit the completed run of a submitted plan through the feed."""
        stop = {"uid": f"{run_uid}-stop", "run_start": run_uid, "exit_status": exit_status}
        if reason is not None:
            stop["reason"] = reason
        return self.feed.emit({"uid": run_uid, CORRELATION_UID_KEY: self.md(index)[CORRELATION_UID_KEY]}, stop)

    def wait_submitted(self, count):
        wait_until(lambda: len(self.plans) >= count)


class RecordingOptimizer:
    def __init__(self):
        self.next_id = 0
        self.suggested = []
        self.ingested = []

    def suggest(self, num_points=None):
        points = []
        for _ in range(num_points or 1):
            points.append({"_id": self.next_id, "x": float(self.next_id)})
            self.next_id += 1
        self.suggested.append(points)
        return points

    def ingest(self, points):
        self.ingested.append([dict(point) for point in points])

    def get_best_points(self):
        return []


class FaultAwareOptimizer(RecordingOptimizer):
    def __init__(self):
        super().__init__()
        self.failed = []

    def register_failures(self, suggestions):
        self.failed.append([dict(suggestion) for suggestion in suggestions])


class ContextOptimizer(RecordingOptimizer):
    def __init__(self):
        super().__init__()
        self.contexts = []

    def suggest_in_context(self, context, num_points=None):
        self.contexts.append(dict(context))
        return self.suggest(num_points)


class RegisteringOptimizer(RecordingOptimizer):
    def __init__(self):
        super().__init__()
        self.registered = []

    def register_suggestions(self, suggestions):
        self.registered.append([dict(suggestion) for suggestion in suggestions])
        out = []
        for suggestion in suggestions:
            out.append({**suggestion, "_id": 100 + self.next_id})
            self.next_id += 1
        return out


class CheckpointingOptimizer(RecordingOptimizer):
    def __init__(self):
        super().__init__()
        self.checkpoints = []

    def checkpoint(self):
        self.checkpoints.append(len(self.ingested))


def evaluate(uid, suggestions):
    return [{"_id": suggestion["_id"], "y": 2 * suggestion["x"]} for suggestion in suggestions]


@pytest.fixture
def feed():
    return FakeFeed()


@pytest.fixture
def router(feed):
    router = RunRouter(feed)
    router.start()
    yield router
    router.close()


@pytest.fixture
def qs(feed):
    return FakeQueueserver(feed)


@pytest.fixture
def make_runner(qs, router):
    runners = []

    def make(optimizer=None, evaluation_function=evaluate, context_function=None, **kwargs):
        problem = QueueserverOptimizationProblem(
            optimizer=optimizer if optimizer is not None else RecordingOptimizer(),
            actuators=["motor"],
            sensors=["det"],
            evaluation_function=evaluation_function,
            acquisition_plan="adaptive_acquire",
            acquisition_plan_kwargs=kwargs.pop("acquisition_plan_kwargs", None),
            context_function=context_function,
        )
        kwargs.setdefault("name", "runner-a")
        runner = QueueserverOptimizationRunner(problem, QueueserverClient(qs.rm), router, **kwargs)
        runners.append(runner)
        return runner

    yield make
    for runner in runners:
        runner.close()


def _result(future):
    return future.result(timeout=5)


def _error(future):
    return future.exception(timeout=5)


# QueueserverClient


def test_client_submit_returns_item_uid_and_never_touches_autostart(qs):
    client = QueueserverClient(qs.rm)

    client.check_environment()
    assert client.submit_plan(MagicMock()) == "item-1"

    qs.rm.queue_autostart.assert_not_called()


def test_client_check_environment_raises_when_closed(qs):
    qs.rm.status.return_value = {"worker_environment_exists": False}

    with pytest.raises(RuntimeError, match="environment is not open"):
        QueueserverClient(qs.rm).check_environment()


def test_client_submit_propagates_rejection(qs):
    error = RequestFailedError({"method": "queue_item_add"}, {"success": False, "msg": "plan rejected"})
    qs.rm.item_add.side_effect = error

    with pytest.raises(RequestFailedError) as exc_info:
        QueueserverClient(qs.rm).submit_plan(MagicMock())

    assert exc_info.value is error


def test_client_finished_item_reads_history_result(qs):
    result = {"exit_status": "completed", "run_uids": ["run-1"], "msg": "", "traceback": ""}
    qs.history.extend(
        [{"item_uid": "item-0", "result": {"exit_status": "failed"}}, {"item_uid": "item-1", "result": result}]
    )
    client = QueueserverClient(qs.rm)

    assert client.finished_item("item-1") == result
    assert client.finished_item("item-2") is None


def test_client_remove_queued_item_reports_refusal(qs):
    client = QueueserverClient(qs.rm)
    assert client.remove_queued_item("item-1") is False

    qs.rm.item_remove.side_effect = None
    assert client.remove_queued_item("item-1") is True


# Runner: submission


def test_run_claims_run_before_submitting_plan(qs, make_runner):
    optimizer = RecordingOptimizer()
    runner = make_runner(optimizer, acquisition_plan_kwargs={"exposure": 0.5})
    # The run finishes before item_add returns; it must still reach the runner.
    qs.on_item_add = lambda plan: qs.finish("run-0")

    result = _result(runner.run(iterations=1, num_points=2))

    plan = qs.plans[0]
    assert plan.name == "adaptive_acquire"
    assert plan.args == [optimizer.suggested[0], ["motor"], ["det"]]
    assert plan.kwargs["exposure"] == 0.5
    assert plan.kwargs["md"]["blop_suggestions"] == optimizer.suggested[0]
    assert plan.kwargs["md"][CORRELATION_UID_KEY]
    assert result == OptimizationResult(iterations_completed=1, num_points=2, uids=("run-0",))


def test_run_completes_all_iterations(qs, make_runner):
    optimizer = RecordingOptimizer()
    records = []
    runner = make_runner(optimizer, on_iteration=records.append)

    future = runner.run(iterations=3, num_points=2)
    for i in range(3):
        qs.wait_submitted(i + 1)
        assert runner.in_flight.iteration == i
        qs.finish(f"run-{i}")

    assert _result(future) == OptimizationResult(iterations_completed=3, num_points=2, uids=("run-0", "run-1", "run-2"))
    assert runner.state == "completed"
    assert runner.current_iteration == 3
    assert runner.in_flight is None
    assert len(qs.plans) == 3
    assert optimizer.ingested == [evaluate("", suggested) for suggested in optimizer.suggested]
    assert [(r.iteration, r.run_uid, r.item_uid, r.error) for r in records] == [
        (0, "run-0", "item-1", None),
        (1, "run-1", "item-2", None),
        (2, "run-2", "item-3", None),
    ]
    assert [r.correlation_uid for r in records] == [qs.md(i)[CORRELATION_UID_KEY] for i in range(3)]


def test_run_rejects_second_operation_while_running(make_runner):
    runner = make_runner(RegisteringOptimizer())
    runner.run(iterations=2)

    with pytest.raises(RuntimeError, match="already running"):
        runner.run(iterations=1)
    with pytest.raises(RuntimeError, match="already running"):
        runner.submit_suggestions([{"x": 1.0}])


def test_run_requires_open_environment(qs, make_runner):
    optimizer = RecordingOptimizer()
    runner = make_runner(optimizer)
    qs.rm.status.return_value = {"worker_environment_exists": False}

    with pytest.raises(RuntimeError, match="environment is not open"):
        runner.run()

    assert runner.state == "idle"
    assert optimizer.suggested == []
    assert qs.plans == []


def test_run_submission_rejection_fails_operation_and_reraises(qs, make_runner):
    error = RequestFailedError({}, {"success": False, "msg": "plan rejected"})
    qs.rm.item_add.side_effect = error
    optimizer = FaultAwareOptimizer()
    runner = make_runner(optimizer)

    with pytest.raises(RequestFailedError):
        runner.run(iterations=2)

    assert runner.state == "failed"
    assert _error(runner.future) is error
    assert optimizer.failed == optimizer.suggested
    qs.rm.item_add.side_effect = qs._item_add
    runner.run()
    assert runner.state == "running"


# Runner: run processing


def test_run_from_another_owner_leaves_runner_untouched(qs, router, make_runner):
    optimizer = RecordingOptimizer()
    runner = make_runner(optimizer)
    future = runner.run(iterations=1)
    other_runs = []
    router.claim("corr-other", "runner-b", other_runs.append)

    qs.feed.emit(
        {"uid": "other-run", CORRELATION_UID_KEY: "corr-other"}, {"run_start": "other-run", "exit_status": "success"}
    )
    qs.feed.emit({"uid": "stray-run", CORRELATION_UID_KEY: "corr-stale"}, {"run_start": "stray-run", "exit_status": "fail"})

    assert [run.uid for run in other_runs] == ["other-run"]
    assert not future.done()
    assert runner.state == "running"
    qs.finish("run-0")
    assert _result(future).uids == ("run-0",)
    assert len(optimizer.ingested) == 1


@pytest.mark.parametrize(
    ("evaluation_function", "message"),
    [
        (lambda uid, suggestions: [{"_id": "wrong", "y": 1.0} for _ in suggestions], "same IDs"),
        (MagicMock(side_effect=KeyError("missing column")), "missing column"),
    ],
    ids=["outcome-id-mismatch", "evaluation-raises"],
)
def test_failed_evaluation_fails_operation_and_registers_failures(qs, make_runner, evaluation_function, message):
    optimizer = FaultAwareOptimizer()
    records = []
    runner = make_runner(optimizer, evaluation_function=evaluation_function, on_iteration=records.append)
    future = runner.run(iterations=3)

    qs.finish("run-0")

    assert message in str(_error(future))
    assert runner.state == "failed"
    assert optimizer.ingested == []
    assert optimizer.failed == optimizer.suggested
    assert len(qs.plans) == 1
    assert records[-1].run_uid == "run-0"
    assert message in records[-1].error


def test_register_failures_error_keeps_original_error(qs, make_runner):
    optimizer = FaultAwareOptimizer()
    optimizer.register_failures = MagicMock(side_effect=RuntimeError("register exploded"))
    error = RuntimeError("evaluation failed")
    runner = make_runner(optimizer, evaluation_function=MagicMock(side_effect=error))
    future = runner.run()

    qs.finish("run-0")

    assert _error(future) is error
    optimizer.register_failures.assert_called_once()


@pytest.mark.parametrize("exit_status", ["fail", "abort"])
def test_unsuccessful_run_fails_operation(qs, make_runner, exit_status):
    optimizer = FaultAwareOptimizer()
    runner = make_runner(optimizer)
    future = runner.run(iterations=3)
    qs.finish("run-0")
    qs.wait_submitted(2)

    qs.finish("run-1", exit_status=exit_status, reason="beam lost")

    error = _error(future)
    assert isinstance(error, RuntimeError)
    assert exit_status in str(error)
    assert "beam lost" in str(error)
    assert runner.state == "failed"
    assert optimizer.failed == [optimizer.suggested[1]]
    assert len(optimizer.ingested) == 1
    assert len(qs.plans) == 2


def test_failing_iteration_hook_does_not_break_loop(qs, make_runner):
    runner = make_runner(on_iteration=MagicMock(side_effect=RuntimeError("hook broke")))
    future = runner.run(iterations=2)

    qs.finish("run-0")
    qs.wait_submitted(2)
    qs.finish("run-1")

    assert _result(future).uids == ("run-0", "run-1")
    assert runner.state == "completed"


@pytest.mark.parametrize(("interval", "after_ingests"), [(None, []), (1, [1, 2, 3]), (2, [2])])
def test_checkpoint_interval(qs, make_runner, interval, after_ingests):
    optimizer = CheckpointingOptimizer()
    runner = make_runner(optimizer)
    future = runner.run(iterations=3, checkpoint_interval=interval)

    for i in range(3):
        qs.wait_submitted(i + 1)
        qs.finish(f"run-{i}")

    _result(future)
    assert optimizer.checkpoints == after_ingests


def test_checkpoint_interval_needs_checkpointable_optimizer(qs, make_runner):
    runner = make_runner(RecordingOptimizer())
    future = runner.run(iterations=2, checkpoint_interval=1)

    qs.finish("run-0")

    assert "not checkpointable" in str(_error(future))
    assert runner.state == "failed"


# Runner: watchdog


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"exit_status": "failed", "run_uids": [], "msg": "Plan raised ZeroDivisionError"}, "ZeroDivisionError"),
        ({"exit_status": "completed", "run_uids": []}, "without opening a run"),
        ({"exit_status": "completed", "run_uids": ["lost-run"]}, "lost-run"),
    ],
    ids=["item-failed", "no-run", "feed-timeout"],
)
def test_watchdog_fails_item_that_cannot_deliver_a_run(qs, make_runner, result, message):
    optimizer = FaultAwareOptimizer()
    runner = make_runner(optimizer, poll_interval=0.01, run_feed_timeout=0.05)
    future = runner.run(iterations=2)

    qs.history.append({"item_uid": "item-1", "result": result})

    assert message in str(_error(future))
    assert runner.state == "failed"
    assert optimizer.failed == optimizer.suggested
    assert len(qs.plans) == 1


def test_watchdog_waits_for_feed_after_completed_item(qs, make_runner):
    runner = make_runner(poll_interval=0.01, run_feed_timeout=5.0)
    future = runner.run()
    qs.history.append({"item_uid": "item-1", "result": {"exit_status": "completed", "run_uids": ["run-0"]}})
    wait_until(lambda: qs.rm.history_get.call_count >= 3)

    qs.finish("run-0")

    assert _result(future).uids == ("run-0",)


# Runner: stop and close


def test_stop_while_queued_removes_item_and_stops_at_once(qs, make_runner):
    optimizer = FaultAwareOptimizer()
    records = []
    runner = make_runner(optimizer, on_iteration=records.append)
    future = runner.run(iterations=3)
    qs.rm.item_remove.side_effect = None

    assert runner.stop() == "stopped"

    qs.rm.item_remove.assert_called_once_with(uid="item-1")
    assert future.done()
    assert future.result() == OptimizationResult(iterations_completed=0, num_points=1, uids=())
    assert runner.state == "stopped"
    assert optimizer.failed == optimizer.suggested
    assert [(r.iteration, r.error) for r in records] == [(0, "removed from queue by stop()")]
    # The removed plan's run, should it still arrive, is not ingested.
    qs.finish("run-0")
    assert optimizer.ingested == []


def test_stop_while_running_ingests_in_flight_run_then_stops(qs, make_runner):
    optimizer = RecordingOptimizer()
    runner = make_runner(optimizer)
    future = runner.run(iterations=3)

    assert runner.stop() == "stopping"
    assert runner.state == "stopping"
    assert not future.done()

    qs.finish("run-0")

    assert _result(future) == OptimizationResult(iterations_completed=1, num_points=1, uids=("run-0",))
    assert runner.state == "stopped"
    assert len(optimizer.ingested) == 1
    assert len(qs.plans) == 1


def test_stop_during_suggestion_submits_nothing(qs, make_runner):
    entered, release = threading.Event(), threading.Event()

    class SlowOptimizer(FaultAwareOptimizer):
        def suggest(self, num_points=None):
            entered.set()
            assert release.wait(5)
            return super().suggest(num_points)

    optimizer = SlowOptimizer()
    runner = make_runner(optimizer)
    with ThreadPoolExecutor(max_workers=1) as pool:
        started = pool.submit(runner.run, iterations=3)
        assert entered.wait(5)
        assert runner.stop() == "stopping"
        release.set()
        future = started.result(timeout=5)

    assert _result(future) == OptimizationResult(iterations_completed=0, num_points=1, uids=())
    assert runner.state == "stopped"
    assert qs.plans == []
    assert optimizer.failed == optimizer.suggested


def test_stop_after_completion_changes_nothing(qs, make_runner):
    runner = make_runner()
    future = runner.run()
    qs.finish("run-0")
    _result(future)

    assert runner.stop() == "completed"
    assert runner.state == "completed"


def test_close_fails_active_operation_and_rejects_new_runs(qs, make_runner):
    optimizer = RecordingOptimizer()
    runner = make_runner(optimizer)
    future = runner.run(iterations=2)

    runner.close()

    error = _error(future)
    assert isinstance(error, RuntimeError)
    assert "runner closed" in str(error)
    qs.finish("run-0")
    assert optimizer.ingested == []
    with pytest.raises(RuntimeError, match="runner closed"):
        runner.run()


# Runner: submit_suggestions


def test_submit_suggestions_registers_only_points_without_id(qs, make_runner):
    optimizer = RegisteringOptimizer()
    runner = make_runner(optimizer)

    future = runner.submit_suggestions([{"x": 1.0}, {"_id": 7, "x": 2.0}, {"x": 3.0}])
    qs.finish("run-0")

    assert _result(future) == OptimizationResult(iterations_completed=1, num_points=3, uids=("run-0",))
    assert optimizer.registered == [[{"x": 1.0}, {"x": 3.0}]]
    assert optimizer.suggested == []
    submitted = qs.md()["blop_suggestions"]
    assert [point["_id"] for point in submitted] == [100, 7, 101]
    assert [point["x"] for point in submitted] == [1.0, 2.0, 3.0]
    assert optimizer.ingested == [evaluate("", submitted)]


def test_submit_suggestions_without_ids_needs_registering_optimizer(qs, make_runner):
    runner = make_runner(RecordingOptimizer())

    with pytest.raises(ValueError, match="'_id'"):
        runner.submit_suggestions([{"x": 1.0}])

    assert qs.plans == []
    assert runner.state == "idle"


# Runner: context


def _contexts(uid):
    return {"context-run": {"c": 1.0}, "run-0": {"c": 2.0}, "run-1": {"c": 3.0}}[uid]


def test_contextual_run_chains_context_through_own_runs(qs, make_runner):
    optimizer = ContextOptimizer()
    records = []
    runner = make_runner(optimizer, context_function=_contexts, on_iteration=records.append)
    assert runner.is_contextual

    future = runner.run(iterations=2, num_points=2, context_uid="context-run")
    qs.finish("run-0")
    qs.wait_submitted(2)
    qs.finish("run-1")

    assert _result(future).uids == ("run-0", "run-1")
    assert optimizer.contexts == [{"c": 1.0}, {"c": 2.0}]
    assert optimizer.ingested == [
        [{**outcome, "c": 2.0} for outcome in evaluate("", optimizer.suggested[0])],
        [{**outcome, "c": 3.0} for outcome in evaluate("", optimizer.suggested[1])],
    ]
    assert [(r.context_uid, r.context, r.run_uid) for r in records] == [
        ("context-run", {"c": 1.0}, "run-0"),
        ("run-0", {"c": 2.0}, "run-1"),
    ]
    assert all("c" not in point for point in qs.md(0)["blop_suggestions"])


@pytest.mark.parametrize(
    ("contextual", "context_uid", "message"),
    [(True, None, "contextual"), (False, "context-run", "no context function")],
)
def test_run_checks_context_uid_against_problem(qs, make_runner, contextual, context_uid, message):
    optimizer = ContextOptimizer()
    runner = make_runner(optimizer, context_function=_contexts if contextual else None)

    with pytest.raises(ValueError, match=message):
        runner.run(context_uid=context_uid)

    assert runner.state == "idle"
    assert optimizer.suggested == []
    assert qs.plans == []


def test_context_function_needs_context_aware_optimizer(make_runner):
    with pytest.raises(ValueError, match="ContextAware"):
        make_runner(RecordingOptimizer(), context_function=_contexts)
