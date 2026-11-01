"""Fakes for the adaptive server: a queueserver that executes plans, a run feed, and a toy catalog."""

import random
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

pytest.importorskip("fastapi")

from bluesky_queueserver_api.comm_base import RequestFailedError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from bluesky_adaptive.runs import CORRELATION_UID_KEY, CompletedRun  # noqa: E402
from bluesky_adaptive.server import (  # noqa: E402
    ContextEntry,
    EvaluationEntry,
    IngestionEntry,
    OptimizerEntry,
    ServerSettings,
    create_app,
)

DOF = "m-x"
API = "/api/v1"


def response_value(x: float, c: float) -> float:
    """The toy beamline's response: a peak at ``x = 0.3 + c``."""
    return -((x - 0.3 - c) ** 2)


@dataclass
class RunData:
    """What a fake run recorded: the points it measured and the condition ``c`` it ran under."""

    start: dict[str, Any]
    points: list[dict[str, Any]]
    c: float


class FakeFeed:
    """A run feed the fake queueserver (and tests) push completed runs into."""

    def __init__(self):
        self._callback: Callable[[CompletedRun], None] | None = None
        self._state = "idle"

    @property
    def state(self) -> str:
        return self._state

    def start(self, callback: Callable[[CompletedRun], None]) -> None:
        self._callback = callback
        self._state = "subscribed"

    def stop(self) -> None:
        self._state = "stopped"

    def emit(self, start: Mapping[str, Any], exit_status: str = "success") -> None:
        assert self._callback is not None, "feed not started"
        self._callback(CompletedRun(start=dict(start), stop={"run_start": start["uid"], "exit_status": exit_status}))


class Beamline:
    """Run store shared by the fake queueserver and the catalog entries; also plays other users."""

    def __init__(self, feed: FakeFeed):
        self.feed = feed
        self.runs: dict[str, RunData] = {}
        self.condition = 0.0
        self._count = 0
        self._lock = threading.Lock()

    def new_uid(self) -> str:
        with self._lock:
            self._count += 1
            return f"run-{self._count}"

    def record(self, start: dict[str, Any], points: list[dict[str, Any]], c: float) -> None:
        self.runs[start["uid"]] = RunData(start=start, points=points, c=c)

    def external_run(
        self, xs: Sequence[float], *, c: float = 0.0, plan_name: str = "scan", motors: Sequence[str] = (DOF,), uid=None
    ) -> str:
        """Record a run another user took and publish it on the feed; return its uid."""
        uid = uid or self.new_uid()
        start = {"uid": uid, "time": time.time(), "plan_name": plan_name, "motors": list(motors), "detectors": ["det"]}
        self.record(start, [{DOF: x} for x in xs], c)
        self.feed.emit(start)
        return uid


class FakeRM:
    """
    A RE Manager that runs queued plans in order on one worker thread.

    ``start_gate`` holds items in the queue (removable); ``finish_gate`` holds the running item
    (not removable). Finished items go to the history and their run to the feed.
    """

    def __init__(self, beamline: Beamline, delay: float = 0.003):
        self.beamline = beamline
        self.delay = delay
        self.environment_open = True
        self.errors: dict[str, Exception] = {}
        self.start_gate = threading.Event()
        self.finish_gate = threading.Event()
        self.start_gate.set()
        self.finish_gate.set()
        self.items: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.running: dict[str, Any] | None = None
        self._queue: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    def _raise(self, method: str) -> None:
        error = self.errors.pop(method, None)
        if error is not None:
            raise error

    @property
    def queued(self) -> list[str]:
        with self._lock:
            return [item["item_uid"] for item in self._queue]

    def status(self) -> dict[str, Any]:
        self._raise("status")
        with self._lock:
            return {
                "worker_environment_exists": self.environment_open,
                "manager_state": "executing_queue" if self.running else "idle",
                "items_in_queue": len(self._queue),
                "re_state": "running" if self.running else "idle",
                "running_item_uid": self.running["item_uid"] if self.running else None,
            }

    def plans_allowed(self) -> dict[str, Any]:
        self._raise("plans_allowed")

        def parameter(name, kind="POSITIONAL_OR_KEYWORD", **extra):
            return {"name": name, "kind": {"name": kind}, **extra}

        return {
            "plans_allowed": {
                "adaptive_acquire": {
                    "parameters": [
                        parameter("suggestions"),
                        parameter("actuators"),
                        parameter("sensors", default="None"),
                        parameter("md", "KEYWORD_ONLY", default="None"),
                    ]
                },
                "count": {
                    "parameters": [
                        parameter("detectors"),
                        parameter("num", default="1"),
                        parameter("md", "KEYWORD_ONLY", default="None"),
                    ]
                },
            }
        }

    def devices_allowed(self) -> dict[str, Any]:
        self._raise("devices_allowed")
        return {
            "devices_allowed": {
                "m": {
                    "is_movable": True,
                    "is_readable": True,
                    "components": {"x": {"is_movable": True, "is_readable": True}},
                },
                "det": {"is_readable": True},
                "pusher": {"is_movable": True, "is_readable": False},
            }
        }

    def item_add(self, plan) -> dict[str, Any]:
        self._raise("item_add")
        item = {**plan.to_dict()}
        with self._lock:
            item["item_uid"] = f"item-{len(self.items) + 1}"
            self.items.append(item)
            self._queue.append(item)
        return {"success": True, "item": {"item_uid": item["item_uid"]}}

    def item_remove(self, uid: str) -> dict[str, Any]:
        with self._lock:
            for item in self._queue:
                if item["item_uid"] == uid:
                    self._queue.remove(item)
                    return {"success": True}
        raise RequestFailedError({"method": "item_remove"}, {"success": False, "msg": f"item {uid} is not queued"})

    def history_get(self) -> dict[str, Any]:
        with self._lock:
            return {"items": list(self.history)}

    def queue_autostart(self, enable: bool) -> None:
        pass

    def close(self) -> None:
        self._closed.set()
        self.start_gate.set()
        self.finish_gate.set()

    def _work(self) -> None:
        while not self._closed.is_set():
            with self._lock:
                # Checked under the lock that item_add takes, so a cleared gate holds every later item.
                item = self._queue.popleft() if self._queue and self.start_gate.is_set() else None
                self.running = item
            if item is None:
                self._closed.wait(0.002)
                continue
            time.sleep(self.delay)
            self.finish_gate.wait()
            if self._closed.is_set():
                return
            self._finish(item)

    def _finish(self, item: dict[str, Any]) -> None:
        suggestions, _, sensors = item["args"]
        md = item["kwargs"]["md"]
        uid = self.beamline.new_uid()
        motors = sorted({key for suggestion in suggestions for key in suggestion if key != "_id"})
        start = {
            "uid": uid,
            "time": time.time(),
            "plan_name": item["name"],
            "motors": motors,
            "detectors": list(sensors),
            CORRELATION_UID_KEY: md[CORRELATION_UID_KEY],
        }
        self.beamline.record(start, [dict(suggestion) for suggestion in suggestions], self.beamline.condition)
        with self._lock:
            self.history.append({"item_uid": item["item_uid"], "result": {"exit_status": "completed", "run_uids": [uid]}})
            self.running = None
        self.beamline.feed.emit(start)


# --- Toy catalog -----------------------------------------------------------------------------------


class NoParams(BaseModel):
    pass


class ToyOptions(BaseModel):
    seed: int = 0


class NamedParams(BaseModel):
    output: str


class ScanParams(BaseModel):
    plan_names: list[str] = ["scan", "adaptive_acquire"]


class NotEnoughData(Exception):
    pass


class Toy:
    """Random search that remembers its suggestions, so outcomes can be ingested by ``_id``."""

    def __init__(self, problem, options: ToyOptions):
        self._bounds = {dof.name: dof.bounds for dof in problem.dofs}
        self._rng = random.Random(options.seed)
        self._next_id = 0
        self.pending: dict[int, dict[str, Any]] = {}
        self.data: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    def _register(self, point: Mapping[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        registered = {**point, "_id": self._next_id}
        self.pending[self._next_id] = registered
        return dict(registered)

    def suggest(self, num_points=None):
        return [
            self._register({name: self._rng.uniform(*bounds) for name, bounds in self._bounds.items()})
            for _ in range(num_points or 1)
        ]

    def register_suggestions(self, suggestions):
        return [self._register(suggestion) for suggestion in suggestions]

    def register_failures(self, suggestions):
        self.failures.extend(dict(suggestion) for suggestion in suggestions)

    def ingest(self, points):
        for point in points:
            if "_id" in point:
                if point["_id"] not in self.pending:
                    raise ValueError(f"unknown _id {point['_id']!r}")
                point = {**self.pending.pop(point["_id"]), **point}
            self.data.append(dict(point))

    def get_best_points(self):
        if not self.data:
            raise ValueError("no data")
        best = max(self.data, key=lambda point: point["y"])
        return [(best.get("_id"), {name: best[name] for name in self._bounds}, {"y": best["y"]})]


class ToyContextual(Toy):
    def __init__(self, problem, options):
        super().__init__(problem, options)
        self.contexts: list[dict[str, Any]] = []

    def suggest_in_context(self, context, num_points=None):
        self.contexts.append(dict(context))
        return self.suggest(num_points)


class Shy(Toy):
    """Refuses to suggest or report best points before it has data."""

    def suggest(self, num_points=None):
        if not self.data:
            raise NotEnoughData("needs data first")
        return super().suggest(num_points)

    def get_best_points(self):
        if not self.data:
            raise NotEnoughData("needs data first")
        return super().get_best_points()


class ScanIngestor:
    """Learns from runs of the listed plans that moved exactly the agent's DOFs."""

    def __init__(self, problem, params: ScanParams, beamline: Beamline):
        self._dofs = set(problem.dof_names)
        self._plans = set(params.plan_names)
        self._beamline = beamline

    def accepts(self, start):
        return start.get("plan_name") in self._plans and set(start.get("motors") or ()) == self._dofs

    def __call__(self, uid):
        run = self._beamline.runs[uid]
        return [{DOF: point[DOF], "y": response_value(point[DOF], run.c)} for point in run.points]


def _raise_value_error(*args):
    raise ValueError("this optimizer rejects every problem")


def make_catalog(beamline: Beamline) -> dict[str, list]:
    def evaluate(uid, suggestions):
        run = beamline.runs[uid]
        return [{"_id": point["_id"], "y": response_value(point[DOF], run.c)} for point in run.points]

    def named(problem, params: NamedParams):
        return lambda uid, suggestions: [{"_id": o["_id"], params.output: o["y"]} for o in evaluate(uid, suggestions)]

    def context_function(uid):
        return {"c": beamline.runs[uid].c}

    return {
        "optimizers": [
            OptimizerEntry("toy", "1", ToyOptions, lambda problem, options, path: Toy(problem, options), "random search"),
            OptimizerEntry("toy-ctx", "1", ToyOptions, lambda problem, options, path: ToyContextual(problem, options)),
            OptimizerEntry("picky", "1", ToyOptions, _raise_value_error),
            OptimizerEntry(
                "shy", "1", ToyOptions, lambda problem, options, path: Shy(problem, options), no_data_errors=(NotEnoughData,)
            ),
        ],
        "evaluations": [
            EvaluationEntry("quad", "1", NoParams, lambda problem, params: evaluate, lambda params: ["y"]),
            EvaluationEntry("named", "1", NamedParams, named, lambda params: [params.output]),
        ],
        "contexts": [
            ContextEntry("condition", "1", NoParams, lambda problem, params: context_function, lambda params: ["c"])
        ],
        "ingestions": [
            IngestionEntry(
                "scans",
                "1",
                ScanParams,
                lambda problem, params: ScanIngestor(problem, params, beamline),
                lambda params: ["y"],
            )
        ],
    }


def make_spec(**overrides) -> dict[str, Any]:
    spec = {
        "queueserver": "qs",
        "dofs": [{"name": DOF, "actuator": "m.x", "bounds": [-1, 1]}],
        "objectives": [{"name": "y"}],
        "sensors": ["det"],
        "acquisition_plan": {"name": "adaptive_acquire"},
        "evaluation": {"name": "quad"},
        "optimizer": {"name": "toy"},
    }
    spec.update(overrides)
    return spec


CONTEXTUAL = {"optimizer": {"name": "toy-ctx"}, "context": {"name": "condition"}}


def wait_for(predicate: Callable[[], Any], timeout: float = 5.0, message: str = "condition") -> Any:
    """Poll ``predicate`` until it returns something truthy; return that."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {message}")
        time.sleep(0.005)


@dataclass
class Server:
    client: TestClient
    rm: FakeRM
    beamline: Beamline
    app: Any

    def create_agent(self, **overrides) -> dict[str, Any]:
        response = self.client.post(f"{API}/agents", json=make_spec(**overrides))
        assert response.status_code == 201, response.text
        return response.json()

    def status(self, agent_id: str) -> dict[str, Any]:
        response = self.client.get(f"{API}/agents/{agent_id}")
        assert response.status_code == 200, response.text
        return response.json()["status"]

    def wait_state(self, agent_id: str, *states: str, timeout: float = 5.0) -> dict[str, Any]:
        return wait_for(
            lambda: (status := self.status(agent_id))["state"] in states and status,
            timeout,
            f"agent {agent_id} to reach {states}",
        )

    def history(self, agent_id: str) -> dict[str, Any]:
        return self.client.get(f"{API}/agents/{agent_id}/history").json()

    def ingestion(self, agent_id: str) -> dict[str, Any]:
        return self.client.get(f"{API}/agents/{agent_id}/ingestion").json()

    def optimizer(self, agent_id: str):
        """The agent's built optimizer, to check what it received."""
        return self.app.state.registry[agent_id].optimizer


@pytest.fixture
def server(tmp_path):
    feed = FakeFeed()
    beamline = Beamline(feed)
    rm = FakeRM(beamline)
    settings = ServerSettings.model_validate(
        {"storage_dir": str(tmp_path), "queueservers": {"qs": {"run_feed": {"type": "zmq", "address": "localhost:5578"}}}}
    )
    app = create_app(settings, **make_catalog(beamline), connect=lambda name, qs_settings: (rm, feed))
    try:
        with TestClient(app) as client:
            yield Server(client=client, rm=rm, beamline=beamline, app=app)
    finally:
        rm.close()
