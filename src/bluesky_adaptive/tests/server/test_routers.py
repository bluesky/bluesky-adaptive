from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from bluesky_adaptive.server.app import create_app
from bluesky_adaptive.server.models import AgentConfig
from bluesky_adaptive.server.routers import create_events_router


def test_http_agent_errors_health_capabilities_and_queue_absence(manager):
    def broken_check():
        raise RuntimeError("offline")

    app = create_app(manager, health_checks={"dependency": broken_check}, capabilities={"beamline": "test"})
    with TestClient(app) as client:
        response = client.post("/api/v1/agents", json={"name": "agent", "problem": "fake", "params": {"x": 1}})
        assert response.status_code == 201
        assert response.json()["params"] == {"x": 1}
        assert client.post("/api/v1/agents", json={"name": "agent", "problem": "fake"}).status_code == 409
        invalid = client.post("/api/v1/agents/validate", json={"problem": "fake", "params": {"x": -1}})
        assert invalid.status_code == 422
        assert isinstance(invalid.json()["detail"], list)
        assert client.get("/api/v1/agents/missing").status_code == 404
        health = client.get("/api/v1/health")
        assert health.status_code == 503
        assert health.json()["checks"]["dependency"]["detail"] == "RuntimeError: offline"
        capabilities = client.get("/api/v1/capabilities").json()
        assert capabilities == {"problems": ["fake"], "analyses": [], "queueserver": False, "beamline": "test"}
        assert client.get("/api/v1/queueserver/status").status_code == 404


def test_health_is_ok_without_failing_checks(manager):
    with TestClient(create_app(manager)) as client:
        assert client.get("/api/v1/health").json() == {"ok": True, "checks": {}}


def test_event_stream_snapshot_filter_and_resume(manager):
    # The SSE endpoint never ends, so drive its generator directly instead of through HTTP.
    stream = next(route.endpoint for route in create_events_router(manager).routes if route.path == "/events")
    manager.create_agent(AgentConfig(name="agent", problem="fake", params={"x": 1}))
    optimization = manager.start_optimization("agent")

    async def consume():
        events = stream(optimization_id=optimization.id, snapshot=True, last_event_id=None)
        snapshot = await anext(events)
        manager.events.publish("trial", {"number": 99}, "another-optimization")
        manager.suggest(optimization.id, 1)
        trial = await anext(events)
        await events.aclose()

        resumed = stream(optimization_id=None, snapshot=True, last_event_id=snapshot.id)
        replayed = [await anext(resumed), await anext(resumed)]
        await resumed.aclose()
        return snapshot, trial, replayed

    snapshot, trial, replayed = asyncio.run(consume())
    assert snapshot.event == "snapshot"
    assert [record["id"] for record in snapshot.data["optimizations"]] == [optimization.id]
    assert (trial.event, trial.data["number"], trial.data["status"]) == ("trial", 0, "suggested")
    # Resuming replays every later event, including other optimizations', without a new snapshot.
    assert [(event.event, event.data["number"]) for event in replayed] == [("trial", 99), ("trial", 0)]
