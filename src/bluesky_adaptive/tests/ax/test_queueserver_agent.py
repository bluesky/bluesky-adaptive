import asyncio
import itertools
from unittest.mock import MagicMock, patch

import pytest
from ax import Client
from bluesky_queueserver_api.zmq import REManagerAPI

from bluesky_adaptive.ax.dof import RangeDOF
from bluesky_adaptive.ax.objective import Objective
from bluesky_adaptive.ax.queueserver_agent import QueueserverAgent
from bluesky_adaptive.protocols import AcquisitionPlan, EvaluationFunction
from bluesky_adaptive.queueserver import OptimizationResult

from ..conftest import MovableSignal


@pytest.fixture(scope="function")
def mock_evaluation_function():
    return MagicMock(spec=EvaluationFunction)


@pytest.fixture(scope="function")
def mock_acquisition_plan():
    return MagicMock(spec=AcquisitionPlan)


@pytest.fixture(scope="function")
def mock_re_manager_api():
    return MagicMock(spec=REManagerAPI)


def test_queueserver_agent_init(mock_re_manager_api, mock_evaluation_function):
    dof1 = RangeDOF(actuator="test_motor1", bounds=(0, 10), parameter_type="float")
    dof2 = RangeDOF(actuator="test_motor2", bounds=(0, 10), parameter_type="float")
    agent = QueueserverAgent(
        mock_re_manager_api,
        ["det"],
        [dof1, dof2],
        [Objective(name="obj1", minimize=False)],
        mock_evaluation_function,
    )
    assert agent.sensors == ["det"]
    assert agent.actuators == [dof1.actuator, dof2.actuator]
    assert agent.evaluation_function == mock_evaluation_function
    assert agent.acquisition_plan is None
    assert agent.current_iteration == 0
    assert isinstance(agent.ax_client, Client)

    problem = agent.to_optimization_problem()
    assert problem.acquisition_plan is None
    assert problem.actuators == [dof1.parameter_name, dof2.parameter_name]
    assert problem.sensors == ["det"]
    assert problem.evaluation_function == mock_evaluation_function


def test_queueserver_agent_init_sends_no_manager_requests(mock_re_manager_api, mock_evaluation_function):
    """Constructing an agent leaves the shared queueserver state, including autostart, untouched."""
    QueueserverAgent(
        mock_re_manager_api,
        ["det"],
        [RangeDOF(actuator="test_motor1", bounds=(0, 10), parameter_type="float")],
        [Objective(name="obj1", minimize=False)],
        mock_evaluation_function,
    )

    assert mock_re_manager_api.method_calls == []


def test_queueserver_agent_init_acquisition_plan_kwargs(mock_re_manager_api, mock_evaluation_function):
    dof1 = RangeDOF(actuator="test_motor1", bounds=(0, 10), parameter_type="float")
    agent = QueueserverAgent(
        mock_re_manager_api,
        ["det"],
        [dof1],
        [Objective(name="obj1", minimize=False)],
        mock_evaluation_function,
        acquisition_plan_kwargs={"exposure_time": 0.5, "num_frames": 10},
    )

    problem = agent.to_optimization_problem()
    assert problem.acquisition_plan_kwargs == {"exposure_time": 0.5, "num_frames": 10}


def test_queueserver_agent_init_actuator_instance(mock_re_manager_api, mock_evaluation_function):
    movable1 = MovableSignal(name="test_movable1")
    dof1 = RangeDOF(actuator=movable1, bounds=(0, 10), parameter_type="float")
    dof2 = RangeDOF(actuator="test_movable2", bounds=(0, 10), parameter_type="float")
    agent = QueueserverAgent(
        mock_re_manager_api,
        ["det"],
        [dof1, dof2],
        [Objective(name="obj1", minimize=False)],
        mock_evaluation_function,
    )

    assert agent.actuators == [movable1.name, dof2.parameter_name]


@patch("bluesky_adaptive.ax.queueserver_agent.QueueserverClient")
@patch("bluesky_adaptive.ax.queueserver_agent.QueueserverOptimizationRunner")
def test_queueserver_agent_run(
    mock_queueserver_runner_cls,
    mock_queueserver_client_cls,
    mock_re_manager_api,
    mock_evaluation_function,
):
    dof1 = RangeDOF(actuator="test_motor1", bounds=(0, 10), parameter_type="float")
    dof2 = RangeDOF(actuator="test_motor2", bounds=(0, 10), parameter_type="float")
    agent = QueueserverAgent(
        mock_re_manager_api,
        ["det"],
        [dof1, dof2],
        [Objective(name="obj1", minimize=False)],
        mock_evaluation_function,
    )
    mock_queueserver_client_cls.assert_called_once_with(mock_re_manager_api)
    mock_queueserver_runner_cls.assert_called_once()

    agent.run()
    mock_queueserver_runner_cls.return_value.run.assert_called_once_with(
        iterations=1, num_points=1, checkpoint_interval=None
    )


@patch("bluesky_adaptive.ax.queueserver_agent.QueueserverClient")
@patch("bluesky_adaptive.ax.queueserver_agent.QueueserverOptimizationRunner")
def test_queueserver_agent_run_with_checkpoint_interval(
    mock_queueserver_runner_cls,
    mock_queueserver_client_cls,
    mock_re_manager_api,
    mock_evaluation_function,
):
    dof1 = RangeDOF(actuator="test_motor1", bounds=(0, 10), parameter_type="float")
    dof2 = RangeDOF(actuator="test_motor2", bounds=(0, 10), parameter_type="float")
    agent = QueueserverAgent(
        mock_re_manager_api,
        ["det"],
        [dof1, dof2],
        [Objective(name="obj1", minimize=False)],
        mock_evaluation_function,
    )
    mock_queueserver_client_cls.assert_called_once_with(mock_re_manager_api)
    mock_queueserver_runner_cls.assert_called_once()

    agent.run(checkpoint_interval=1)
    mock_queueserver_runner_cls.return_value.run.assert_called_once_with(iterations=1, num_points=1, checkpoint_interval=1)


@patch("bluesky_adaptive.ax.queueserver_agent.QueueserverClient")
@patch("bluesky_adaptive.ax.queueserver_agent.QueueserverOptimizationRunner")
def test_queueserver_agent_submit_suggestions(
    mock_queueserver_runner_cls,
    mock_queueserver_client_cls,
    mock_re_manager_api,
    mock_evaluation_function,
):
    dof1 = RangeDOF(actuator="test_motor1", bounds=(0, 10), parameter_type="float")
    dof2 = RangeDOF(actuator="test_motor2", bounds=(0, 10), parameter_type="float")
    agent = QueueserverAgent(
        mock_re_manager_api,
        ["det"],
        [dof1, dof2],
        [Objective(name="obj1", minimize=False)],
        mock_evaluation_function,
    )
    mock_queueserver_client_cls.assert_called_once_with(mock_re_manager_api)
    mock_queueserver_runner_cls.assert_called_once()

    suggestions = [{"test_motor1": 5, "test_motor2": 9}]
    agent.submit_suggestions(suggestions)
    mock_queueserver_runner_cls.return_value.submit_suggestions.assert_called_once_with(suggestions)


@patch("bluesky_adaptive.ax.queueserver_agent.QueueserverClient")
@patch("bluesky_adaptive.ax.queueserver_agent.QueueserverOptimizationRunner")
def test_queueserver_agent_stop(
    mock_queueserver_runner_cls,
    mock_queueserver_client_cls,
    mock_re_manager_api,
    mock_evaluation_function,
):
    dof1 = RangeDOF(actuator="test_motor1", bounds=(0, 10), parameter_type="float")
    dof2 = RangeDOF(actuator="test_motor2", bounds=(0, 10), parameter_type="float")
    agent = QueueserverAgent(
        mock_re_manager_api,
        ["det"],
        [dof1, dof2],
        [Objective(name="obj1", minimize=False)],
        mock_evaluation_function,
    )
    mock_queueserver_client_cls.assert_called_once_with(mock_re_manager_api)
    mock_queueserver_runner_cls.assert_called_once()

    agent.stop()
    mock_queueserver_runner_cls.return_value.stop.assert_called_once()


def _open_queue_agent(re_manager_api, evaluation_function, **kwargs):
    """Create an agent whose RE Manager has an open environment and returns queue item UIDs "item-0", "item-1", ..."""
    uids = (f"item-{i}" for i in itertools.count())
    re_manager_api.status.return_value = {"worker_environment_exists": True}
    re_manager_api.item_add.side_effect = lambda plan: {"success": True, "item": {"item_uid": next(uids)}}
    return QueueserverAgent(
        re_manager_api,
        ["det"],
        [RangeDOF(actuator="test_motor1", bounds=(0, 10), parameter_type="float")],
        [Objective(name="obj1", minimize=False)],
        evaluation_function,
        **kwargs,
    )


def _evaluate(uid, suggestions):
    return [{"_id": suggestion["_id"], "obj1": suggestion["test_motor1"]} for suggestion in suggestions]


def test_queueserver_agent_run_async(mock_re_manager_api, tmp_path):
    """run_async() runs iterations of n_points each, checkpoints every checkpoint_interval, and returns the result."""
    checkpoint_path = tmp_path / "agent.json"
    agent = _open_queue_agent(mock_re_manager_api, _evaluate, checkpoint_path=str(checkpoint_path))

    result = asyncio.run(agent.run_async(iterations=2, n_points=3, checkpoint_interval=1))

    assert result == OptimizationResult(iterations_completed=2, num_points=3, uids=("item-0", "item-1"))
    assert checkpoint_path.exists()


def test_queueserver_agent_submit_suggestions_async(mock_re_manager_api):
    """submit_suggestions_async() registers and evaluates manual points, then returns the result."""
    evaluation_function = MagicMock(side_effect=_evaluate)
    agent = _open_queue_agent(mock_re_manager_api, evaluation_function)

    result = asyncio.run(agent.submit_suggestions_async([{"test_motor1": 2.5}]))

    assert result == OptimizationResult(iterations_completed=1, num_points=1, uids=("item-0",))
    assert [suggestion["test_motor1"] for suggestion in evaluation_function.call_args.kwargs["suggestions"]] == [2.5]


def test_queueserver_agent_records_its_optimization_uid_in_submitted_plans(mock_re_manager_api):
    """The agent's optimization UID reaches the md of every submitted plan."""
    agent = _open_queue_agent(mock_re_manager_api, _evaluate, optimization_uid="opt-1")

    asyncio.run(agent.run_async(iterations=1))

    assert agent.optimization_uid == "opt-1"
    assert mock_re_manager_api.item_add.call_args.args[0].kwargs["md"]["bluesky-adaptive"] == {"optimization_uid": "opt-1"}


def test_queueserver_agent_problems_share_its_generated_optimization_uid(mock_re_manager_api, mock_evaluation_function):
    """Without an explicit optimization UID, every problem the agent builds carries the one it generated."""
    agent = _open_queue_agent(mock_re_manager_api, mock_evaluation_function)

    assert agent.to_optimization_problem().optimization_uid == agent.optimization_uid
    assert agent.to_optimization_problem().optimization_uid == agent.optimization_uid


def test_queueserver_agent_sends_its_optimization_runs_to_subscribed_callbacks(mock_re_manager_api):
    """Callbacks subscribed and metadata set after construction reach the runs the agent starts afterwards."""
    agent = _open_queue_agent(mock_re_manager_api, _evaluate, optimization_uid="opt-1")
    documents = []
    agent.subscribe(lambda name, doc: documents.append((name, doc)))
    agent.metadata["sample"] = "s1"

    asyncio.run(agent.run_async(iterations=1))

    (start,) = [doc for name, doc in documents if name == "start"]
    assert start["sample"] == "s1"
    assert start["bluesky-adaptive"]["optimization_uid"] == "opt-1"
    assert [doc["data"]["adaptive_acquisition_uid"] for name, doc in documents if name == "event"] == ["item-0"]
    assert documents[-1][0] == "stop"
    assert documents[-1][1]["exit_status"] == "success"
