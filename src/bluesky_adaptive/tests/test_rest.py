from unittest.mock import MagicMock

import pytest
from bluesky.callbacks.zmq import RemoteDispatcher
from bluesky_queueserver_api.comm_base import RequestFailedError
from bluesky_queueserver_api.zmq import REManagerAPI
from fastapi.testclient import TestClient

from bluesky_adaptive.ax.dof import RangeDOF
from bluesky_adaptive.ax.objective import Objective
from bluesky_adaptive.ax.queueserver_agent import QueueserverAgent
from bluesky_adaptive.queueserver import CORRELATION_UID_KEY
from bluesky_adaptive.rest import create_app


def _evaluate(uid, suggestions):
    return [{"_id": s["_id"], "y": s["x1"] ** 2} for s in suggestions]


@pytest.fixture
def re_manager_api():
    api = MagicMock(spec=REManagerAPI)
    api.status.return_value = {"worker_environment_exists": True}
    return api


@pytest.fixture
def document_dispatcher():
    return MagicMock(spec=RemoteDispatcher)


@pytest.fixture
def client(re_manager_api, document_dispatcher):
    agent = QueueserverAgent(
        re_manager_api=re_manager_api,
        document_dispatcher=document_dispatcher,
        sensors=["det"],
        dofs=[RangeDOF(actuator="x1", bounds=(-5.0, 5.0), parameter_type="float")],
        objectives=[Objective(name="y", minimize=True)],
        evaluation_function=_evaluate,
    )
    with TestClient(create_app(agent)) as test_client:
        yield test_client


def _finish_acquisition(re_manager_api, document_dispatcher, run_uid, exit_status="success"):
    """Emit the documents the queueserver would publish for the most recently submitted plan."""
    consumer = document_dispatcher.subscribe.call_args.args[0]
    md = re_manager_api.item_add.call_args.args[0].kwargs["md"]
    consumer.start({"uid": run_uid, "time": 0.0, CORRELATION_UID_KEY: md[CORRELATION_UID_KEY]})
    consumer.stop({"uid": f"{run_uid}-stop", "run_start": run_uid, "time": 0.0, "exit_status": exit_status})


def test_run_reports_progress_until_completed(client, re_manager_api, document_dispatcher):
    assert client.get("/status").json()["state"] == "idle"

    response = client.post("/run", json={"iterations": 2})
    assert response.status_code == 202
    assert response.json()["state"] == "running"

    _finish_acquisition(re_manager_api, document_dispatcher, "run-0")
    assert client.get("/status").json()["state"] == "running"

    _finish_acquisition(re_manager_api, document_dispatcher, "run-1")
    assert client.get("/status").json() == {
        "state": "completed",
        "operation": "run",
        "current_iteration": 2,
        "result": {"iterations_completed": 2, "num_points": 1, "uids": ["run-0", "run-1"]},
        "error": None,
    }


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/run", {}),
        ("/submit-suggestions", {"suggestions": [{"x1": 1.0}]}),
        ("/suggest", {}),
        ("/ingest", {"points": [{"x1": 1.0, "y": 1.0}]}),
        ("/checkpoint", None),
    ],
)
def test_optimizer_mutations_conflict_while_running(client, path, body):
    client.post("/run", json={"iterations": 2})

    response = client.post(path, json=body)

    assert response.status_code == 409
    assert client.get("/trials").status_code == 200


def test_failed_acquisition_fails_operation(client, re_manager_api, document_dispatcher):
    client.post("/run", json={"iterations": 2})

    _finish_acquisition(re_manager_api, document_dispatcher, "run-0", exit_status="fail")

    status = client.get("/status").json()
    assert status["state"] == "failed"
    assert "'run-0' ended with status 'fail'" in status["error"]


def test_stop_reports_partial_result_and_allows_new_run(client):
    client.post("/run", json={"iterations": 3})

    status = client.post("/stop").json()

    assert status["state"] == "stopped"
    assert status["result"] == {"iterations_completed": 0, "num_points": 1, "uids": []}
    assert client.post("/run", json={}).status_code == 202


def test_run_conflicts_when_queueserver_environment_closed(client, re_manager_api):
    re_manager_api.status.return_value = {"worker_environment_exists": False}

    response = client.post("/run", json={})

    assert response.status_code == 409
    assert "environment is not open" in response.json()["detail"]
    assert client.get("/status").json()["state"] == "idle"


def test_rejected_plan_is_bad_gateway(client, re_manager_api):
    re_manager_api.item_add.side_effect = RequestFailedError({}, {"success": False, "msg": "Plan is not allowed"})

    response = client.post("/run", json={})

    assert response.status_code == 502
    assert "Plan is not allowed" in response.json()["detail"]


def test_ask_tell_serializes_missing_values_as_null(client):
    suggestions = client.post("/suggest", json={"n_points": 2}).json()
    assert [set(s) for s in suggestions] == [{"_id", "x1"}, {"_id", "x1"}]

    completed = suggestions[1]
    assert client.post("/ingest", json={"points": [{"_id": completed["_id"], "y": 4.0}]}).status_code == 204

    assert client.get("/best-points").json() == [
        {"_id": completed["_id"], "parameters": {"x1": completed["x1"]}, "outcomes": {"y": [4.0, None]}}
    ]
    trials = {trial["trial_index"]: trial for trial in client.get("/trials").json()}
    assert trials[suggestions[0]["_id"]]["y"] is None
    assert trials[completed["_id"]]["y"] == 4.0


@pytest.mark.parametrize(
    ("method", "path", "body", "status_code"),
    [
        ("post", "/ingest", {"points": [{"_id": 999, "y": 1.0}]}, 422),
        ("get", "/best-points", None, 409),
        ("post", "/checkpoint", None, 409),
    ],
)
def test_optimizer_errors_are_reported_to_client(client, method, path, body, status_code):
    response = client.request(method, path, json=body)

    assert response.status_code == status_code
    assert response.json()["detail"]
