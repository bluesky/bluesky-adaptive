import pytest
from bluesky_queueserver_api.comm_base import RequestFailedError, RequestTimeoutError

from .conftest import API, CONTEXTUAL, DOF, make_spec, response_value, wait_for


def _post(server, agent_id: str, action: str, body: dict | None = None):
    return server.client.post(f"{API}/agents/{agent_id}/{action}", json=body)


# --- Catalog and queueservers ----------------------------------------------------------------------


def test_catalog_describes_entries(server):
    client = server.client
    optimizers = {entry["name"]: entry for entry in client.get(f"{API}/catalog/optimizers").json()}
    evaluations = {entry["name"]: entry for entry in client.get(f"{API}/catalog/evaluations").json()}
    [context] = client.get(f"{API}/catalog/contexts").json()
    [ingestion] = client.get(f"{API}/catalog/ingestions").json()

    assert set(optimizers) == {"toy", "toy-ctx", "picky", "shy"}
    assert "seed" in optimizers["toy"]["schema"]["properties"]
    assert evaluations["quad"]["outputs"] == ["y"]
    # Outputs that depend on required params are not known up front.
    assert evaluations["named"]["outputs"] is None
    assert evaluations["named"]["schema"]["required"] == ["output"]
    assert (context["name"], context["features"]) == ("condition", ["c"])
    assert (ingestion["name"], ingestion["outputs"]) == ("scans", ["y"])
    assert "plan_names" in ingestion["schema"]["properties"]
    assert client.get(f"{API}/catalog/widgets").status_code == 404


def test_queueserver_endpoints_describe_plans_devices_and_feed(server):
    client = server.client
    [queueserver] = client.get(f"{API}/queueservers").json()
    assert queueserver["name"] == "qs"
    assert queueserver["run_feed"]["state"] == "subscribed"
    assert queueserver["status"]["worker_environment_exists"] is True

    plans = {plan["name"]: plan for plan in client.get(f"{API}/queueservers/qs/plans").json()}
    assert plans["adaptive_acquire"]["fits_acquisition_contract"] is True
    assert plans["count"]["fits_acquisition_contract"] is False
    assert plans["count"]["problems"]

    devices = client.get(f"{API}/queueservers/qs/devices").json()
    assert devices["m.x"]["is_movable"] is True
    assert devices["det"] == {"is_movable": False, "is_readable": True, "is_flyable": False}
    assert client.get(f"{API}/queueservers/nope/plans").status_code == 404


# --- Creating agents -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"evaluation": {"name": "nope"}}, id="unknown-entry"),
        pytest.param({"optimizer": {"name": "toy", "options": {"seed": "x"}}}, id="invalid-options"),
        pytest.param({"objectives": [{"name": "z"}]}, id="objective-not-an-output"),
        pytest.param({"dofs": [{"name": DOF, "actuator": "m.q", "bounds": [-1, 1]}]}, id="unknown-actuator"),
        pytest.param({"dofs": [{"name": DOF, "actuator": "det", "bounds": [-1, 1]}]}, id="non-movable-actuator"),
        pytest.param({"sensors": ["nope"]}, id="unknown-sensor"),
        pytest.param({"sensors": ["pusher"]}, id="non-readable-sensor"),
        pytest.param({"acquisition_plan": {"name": "scan"}}, id="plan-not-allowed"),
        pytest.param({"acquisition_plan": {"name": "count"}}, id="plan-breaks-acquisition-contract"),
        pytest.param({"optimizer": {"name": "picky"}}, id="build-raises-value-error"),
        pytest.param({"context": {"name": "condition"}}, id="context-with-non-context-aware-optimizer"),
        pytest.param({"queueserver": "nope"}, id="unknown-queueserver"),
        pytest.param(
            {
                "evaluation": {"name": "named", "params": {"output": "z"}},
                "objectives": [{"name": "z"}],
                "ingestion": {"name": "scans"},
            },
            id="ingestion-outputs-not-evaluation-outputs",
        ),
    ],
)
def test_create_agent_rejects_invalid_spec(server, overrides):
    response = server.client.post(f"{API}/agents", json=make_spec(**overrides))
    assert response.status_code == 422, response.text
    assert server.client.get(f"{API}/agents").json() == []


def test_create_agent_reports_unresponsive_queueserver_as_503(server):
    server.rm.errors["plans_allowed"] = RequestTimeoutError("no reply", {"method": "plans_allowed"})
    assert server.client.post(f"{API}/agents", json=make_spec()).status_code == 503
    assert server.client.get(f"{API}/agents").json() == []


# --- Running ---------------------------------------------------------------------------------------


def test_run_lifecycle_is_reported_in_status_and_history(server):
    agent = server.create_agent()
    agent_id = agent["id"]
    assert agent["status"]["state"] == "idle"
    assert agent["outputs"] == ["y"]
    assert agent["capabilities"]["register_suggestions"] is True
    assert agent["capabilities"]["contextual"] is False

    response = _post(server, agent_id, "run", {"iterations": 3, "n_points": 2})
    assert response.status_code == 202
    operation_id = response.json()["operation"]["id"]

    status = server.wait_state(agent_id, "completed")
    assert status["current_iteration"] == 3
    assert status["in_flight"] is None
    operation = status["operation"]
    assert (operation["id"], operation["kind"], operation["state"]) == (operation_id, "run", "completed")
    run_uids = operation["result"]["uids"]
    assert operation["result"]["iterations_completed"] == 3
    assert len(set(run_uids)) == 3

    history = server.history(agent_id)
    assert [op["id"] for op in history["operations"]] == [operation_id]
    iterations = history["iterations"]
    assert [record["iteration"] for record in iterations] == [0, 1, 2]
    assert {record["operation"] for record in iterations} == {operation_id}
    assert [record["run_uid"] for record in iterations] == run_uids
    for record in iterations:
        assert record["error"] is None
        assert [point["_id"] for point in record["points"]] == [s["_id"] for s in record["suggestions"]]
        for suggestion, point in zip(record["suggestions"], record["points"], strict=True):
            assert point["y"] == pytest.approx(response_value(suggestion[DOF], 0.0))
    assert len(server.rm.items) == 3


def test_busy_agent_rejects_conflicting_requests_and_deletes_when_idle(server):
    agent_id = server.create_agent()["id"]
    server.rm.finish_gate.clear()
    assert _post(server, agent_id, "run", {"iterations": 2}).status_code == 202
    wait_for(lambda: server.rm.running, message="the plan to start")

    assert _post(server, agent_id, "run", {"iterations": 1}).status_code == 409
    assert _post(server, agent_id, "suggest", {}).status_code == 409
    assert _post(server, agent_id, "submit-suggestions", {"suggestions": [{DOF: 0.1}]}).status_code == 409
    assert server.client.delete(f"{API}/agents/{agent_id}").status_code == 409

    server.rm.finish_gate.set()
    server.wait_state(agent_id, "completed")
    assert server.client.delete(f"{API}/agents/{agent_id}").status_code == 204
    assert server.client.get(f"{API}/agents/{agent_id}").status_code == 404
    assert server.client.delete(f"{API}/agents/{agent_id}").status_code == 404
    assert server.client.get(f"{API}/agents").json() == []


def test_rejected_submission_returns_502_and_agent_can_run_again(server):
    agent_id = server.create_agent()["id"]
    server.rm.errors["item_add"] = RequestFailedError({"method": "item_add"}, {"success": False, "msg": "rejected"})

    assert _post(server, agent_id, "run", {"iterations": 2}).status_code == 502
    status = server.status(agent_id)
    assert status["state"] == "failed"
    assert status["operation"]["state"] == "failed"
    assert status["operation"]["error"]

    assert _post(server, agent_id, "run", {"iterations": 1}).status_code == 202
    assert server.wait_state(agent_id, "completed")["operation"]["result"]["iterations_completed"] == 1


def test_run_with_closed_environment_returns_409_without_an_operation(server):
    agent_id = server.create_agent()["id"]
    server.rm.environment_open = False

    assert _post(server, agent_id, "run", {"iterations": 1}).status_code == 409
    assert server.status(agent_id)["state"] == "idle"
    assert server.history(agent_id)["operations"] == []
    assert server.rm.items == []


def test_stop_while_plan_is_queued_stops_at_once(server):
    agent_id = server.create_agent()["id"]
    server.rm.start_gate.clear()
    assert _post(server, agent_id, "run", {"iterations": 5}).status_code == 202

    status = _post(server, agent_id, "stop").json()
    assert status["state"] == "stopped"
    assert status["operation"]["state"] == "stopped"
    assert status["operation"]["result"]["iterations_completed"] == 0
    assert server.rm.queued == []
    assert len(server.rm.items) == 1
    [record] = server.history(agent_id)["iterations"]
    assert record["run_uid"] is None
    assert record["error"]
    optimizer = server.optimizer(agent_id)
    assert optimizer.data == []
    assert [failure["_id"] for failure in optimizer.failures] == [s["_id"] for s in record["suggestions"]]


def test_stop_while_plan_is_running_finishes_after_its_run(server):
    agent_id = server.create_agent()["id"]
    server.rm.finish_gate.clear()
    assert _post(server, agent_id, "run", {"iterations": 5}).status_code == 202
    wait_for(lambda: server.rm.running, message="the plan to start")

    assert _post(server, agent_id, "stop").json()["state"] == "stopping"
    assert server.status(agent_id)["state"] == "stopping"
    server.rm.finish_gate.set()

    status = server.wait_state(agent_id, "stopped")
    assert status["operation"]["state"] == "stopped"
    assert status["operation"]["result"]["iterations_completed"] == 1
    [record] = server.history(agent_id)["iterations"]
    assert record["run_uid"] is not None and record["error"] is None
    assert len(server.optimizer(agent_id).data) == 1
    assert len(server.rm.items) == 1


def test_two_agents_on_one_queueserver_ingest_only_their_own_runs(server):
    first = server.create_agent(optimizer={"name": "toy", "options": {"seed": 1}})["id"]
    second = server.create_agent(optimizer={"name": "toy", "options": {"seed": 2}})["id"]

    assert _post(server, first, "run", {"iterations": 4, "n_points": 2}).status_code == 202
    assert _post(server, second, "run", {"iterations": 4, "n_points": 1}).status_code == 202
    first_uids = set(server.wait_state(first, "completed")["operation"]["result"]["uids"])
    second_uids = set(server.wait_state(second, "completed")["operation"]["result"]["uids"])

    assert len(first_uids) == len(second_uids) == 4
    assert first_uids.isdisjoint(second_uids)
    assert first_uids | second_uids == set(server.beamline.runs)
    for agent_id, n_points in ((first, 8), (second, 4)):
        data = server.optimizer(agent_id).data
        assert len(data) == n_points
        # Outcomes were measured at this agent's own suggested positions.
        assert all(point["y"] == pytest.approx(response_value(point[DOF], 0.0)) for point in data)


# --- Suggest, ingest, best points, submit ----------------------------------------------------------


def test_suggest_returns_points_without_acquiring(server):
    agent_id = server.create_agent()["id"]
    response = _post(server, agent_id, "suggest", {"n_points": 3})
    assert response.status_code == 200
    body = response.json()
    assert len({s["_id"] for s in body["suggestions"]}) == 3
    assert all(-1 <= s[DOF] <= 1 for s in body["suggestions"])
    assert body["context"] is None
    assert server.rm.items == []

    run_uid = server.beamline.external_run([0.0])
    assert _post(server, agent_id, "suggest", {"context": run_uid}).status_code == 422


def test_contextual_suggest_needs_a_known_context_run(server):
    run_uid = server.beamline.external_run([0.1], c=0.25)
    agent = server.create_agent(**CONTEXTUAL)
    agent_id = agent["id"]
    assert agent["capabilities"]["contextual"] is True
    assert agent["context_features"] == ["c"]

    assert _post(server, agent_id, "suggest", {}).status_code == 422
    assert _post(server, agent_id, "suggest", {"context": "missing"}).status_code == 422

    body = _post(server, agent_id, "suggest", {"n_points": 2, "context": run_uid}).json()
    assert body["context_uid"] == run_uid
    assert body["context"] == {"c": 0.25}
    assert len(body["suggestions"]) == 2
    assert server.optimizer(agent_id).contexts == [{"c": 0.25}]


def test_contextual_run_takes_context_from_its_previous_run(server):
    seed_run = server.beamline.external_run([0.0], c=0.25)
    agent_id = server.create_agent(**CONTEXTUAL)["id"]
    assert _post(server, agent_id, "run", {"iterations": 3}).status_code == 422

    server.beamline.condition = -0.1
    assert _post(server, agent_id, "run", {"iterations": 3, "context": seed_run}).status_code == 202
    server.wait_state(agent_id, "completed")

    iterations = server.history(agent_id)["iterations"]
    run_uids = [record["run_uid"] for record in iterations]
    assert [record["context_uid"] for record in iterations] == [seed_run, *run_uids[:2]]
    assert [record["context"] for record in iterations] == [{"c": 0.25}, {"c": -0.1}, {"c": -0.1}]
    assert all(point["c"] == -0.1 for record in iterations for point in record["points"])
    assert server.optimizer(agent_id).contexts == [{"c": 0.25}, {"c": -0.1}, {"c": -0.1}]


@pytest.mark.parametrize(
    "points",
    [
        pytest.param([{DOF: 0.1, "y": 1.0, "z": 2.0}], id="unknown-key"),
        pytest.param([{DOF: 0.1}], id="no-outputs"),
        pytest.param([{"y": 1.0}], id="no-id-and-missing-dof"),
        pytest.param([{DOF: [0.1], "y": 1.0}], id="non-scalar-value"),
    ],
)
def test_ingest_rejects_invalid_points(server, points):
    agent_id = server.create_agent()["id"]
    assert _post(server, agent_id, "ingest", {"points": points}).status_code == 422
    assert server.optimizer(agent_id).data == []


def test_ingest_completes_suggestions_by_id_and_reports_best_points(server):
    agent_id = server.create_agent()["id"]
    assert server.client.get(f"{API}/agents/{agent_id}/best-points").status_code == 409

    [suggestion] = _post(server, agent_id, "suggest", {}).json()["suggestions"]
    points = [{"_id": suggestion["_id"], "y": 2.0}, {DOF: 0.5, "y": 1.0}]
    assert _post(server, agent_id, "ingest", {"points": points}).status_code == 204

    best = server.client.get(f"{API}/agents/{agent_id}/best-points").json()
    assert best == [{"_id": suggestion["_id"], "parameters": {DOF: suggestion[DOF]}, "outcomes": {"y": 2.0}}]


def test_contextual_ingest_needs_context_features(server):
    agent_id = server.create_agent(**CONTEXTUAL)["id"]
    assert _post(server, agent_id, "ingest", {"points": [{DOF: 0.1, "y": 1.0}]}).status_code == 422
    assert _post(server, agent_id, "ingest", {"points": [{DOF: 0.1, "y": 1.0, "c": 0.2}]}).status_code == 204
    assert server.optimizer(agent_id).data == [{DOF: 0.1, "y": 1.0, "c": 0.2}]


def test_optimizer_without_enough_data_answers_409(server):
    agent_id = server.create_agent(optimizer={"name": "shy"})["id"]
    assert _post(server, agent_id, "suggest", {}).status_code == 409
    assert server.client.get(f"{API}/agents/{agent_id}/best-points").status_code == 409
    assert _post(server, agent_id, "run", {"iterations": 1}).status_code == 409
    assert server.rm.items == []

    assert _post(server, agent_id, "ingest", {"points": [{DOF: 0.2, "y": -1.0}]}).status_code == 204
    [best] = server.client.get(f"{API}/agents/{agent_id}/best-points").json()
    assert best["parameters"] == {DOF: 0.2}
    assert _post(server, agent_id, "suggest", {}).status_code == 200


@pytest.mark.parametrize(
    "suggestions",
    [
        pytest.param([{DOF: 5.0}], id="out-of-bounds"),
        pytest.param([{}], id="missing-dof"),
        pytest.param([{DOF: 0.1, "other": 1.0}], id="unknown-key"),
    ],
)
def test_submit_suggestions_rejects_invalid_points_before_submitting(server, suggestions):
    agent_id = server.create_agent()["id"]
    assert _post(server, agent_id, "submit-suggestions", {"suggestions": suggestions}).status_code == 422
    assert server.rm.items == []
    assert server.status(agent_id)["state"] == "idle"


def test_submit_suggestions_acquires_given_points_keeping_ids(server):
    agent_id = server.create_agent()["id"]
    [suggested] = _post(server, agent_id, "suggest", {}).json()["suggestions"]

    response = _post(server, agent_id, "submit-suggestions", {"suggestions": [suggested, {DOF: -0.5}]})
    assert response.status_code == 202
    status = server.wait_state(agent_id, "completed")
    assert status["operation"]["kind"] == "submit-suggestions"

    [record] = server.history(agent_id)["iterations"]
    kept, registered = record["suggestions"]
    assert kept == suggested
    assert registered[DOF] == -0.5
    assert registered["_id"] not in (None, suggested["_id"])
    assert sorted(point[DOF] for point in server.optimizer(agent_id).data) == sorted([suggested[DOF], -0.5])


# --- Passive ingestion -----------------------------------------------------------------------------


def _wait_records(server, agent_id: str, count: int) -> list[dict]:
    return wait_for(
        lambda: len(runs := server.ingestion(agent_id)["runs"]) >= count and runs, message=f"{count} ingestion records"
    )


def test_ingestion_learns_once_from_runs_it_did_not_submit(server):
    beamline = server.beamline
    agent = server.create_agent(**CONTEXTUAL, ingestion={"name": "scans"})
    agent_id = agent["id"]
    assert agent["ingestion"]["subscribed"] is False
    assert _post(server, agent_id, "ingestion/subscribe").json()["subscribed"] is True

    accepted = beamline.external_run([0.1, 0.2, 0.3], c=0.2)
    rejected = beamline.external_run([0.4], c=0.2, plan_name="count")
    beamline.feed.emit(beamline.runs[accepted].start)  # delivered again
    records = _wait_records(server, agent_id, 3)
    assert [(r["run_uid"], r["source"], r["status"], r["n_points"]) for r in records] == [
        (accepted, "subscription", "ingested", 3),
        (rejected, "subscription", "rejected", 0),
        (accepted, "subscription", "duplicate", 0),
    ]
    assert server.ingestion(agent_id)["counts"] == {"ingested": 1, "rejected": 1, "duplicate": 1}
    assert server.optimizer(agent_id).data == [
        {DOF: x, "y": pytest.approx(response_value(x, 0.2)), "c": 0.2} for x in (0.1, 0.2, 0.3)
    ]

    # The agent's own run reaches its runner, not its ingestion.
    assert _post(server, agent_id, "run", {"iterations": 1, "context": accepted}).status_code == 202
    [own_run] = server.wait_state(agent_id, "completed")["operation"]["result"]["uids"]

    # Jobs skip `accepts`; runs already ingested, including the agent's own, are not ingested again.
    response = _post(server, agent_id, "ingestion/jobs", {"run_uids": [rejected, own_run, "missing"]})
    assert (response.status_code, response.json()) == (202, {"queued": 3})
    records = _wait_records(server, agent_id, 6)
    assert [(r["run_uid"], r["source"], r["status"], r["n_points"]) for r in records[3:]] == [
        (rejected, "job", "ingested", 1),
        (own_run, "job", "duplicate", 0),
        ("missing", "job", "failed", 0),
    ]

    assert _post(server, agent_id, "ingestion/unsubscribe").json()["subscribed"] is False
    unseen = beamline.external_run([0.0], c=0.2)
    _post(server, agent_id, "ingestion/jobs", {"run_uids": [accepted]})
    records = _wait_records(server, agent_id, 7)
    assert [r["run_uid"] for r in records[6:]] == [accepted]
    assert unseen not in {r["run_uid"] for r in records}


def test_ingestion_endpoints_need_an_ingestion_entry(server):
    agent_id = server.create_agent()["id"]
    assert server.client.get(f"{API}/agents/{agent_id}/ingestion").status_code == 404
    assert _post(server, agent_id, "ingestion/subscribe").status_code == 404
    assert _post(server, agent_id, "ingestion/jobs", {"run_uids": ["run-1"]}).status_code == 404
