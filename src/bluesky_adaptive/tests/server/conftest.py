from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, Field

from bluesky_adaptive.protocols import OptimizationProblem, QueueserverOptimizationProblem
from bluesky_adaptive.queueserver import QueueserverClient
from bluesky_adaptive.server.catalog import Catalog, ProblemTemplate
from bluesky_adaptive.server.manager import OptimizationManager
from bluesky_adaptive.server.repository import InMemoryOptimizationRepository


class ProblemParams(BaseModel):
    x: int = Field(ge=0)


class FakeOptimizer:
    def __init__(self) -> None:
        self.next_id = 0
        self.ingested: list[list[Mapping[str, Any]]] = []
        self.failures: list[list[Mapping[str, Any]]] = []

    def suggest(self, num_points: int | None = None) -> Sequence[Mapping[str, Any]]:
        count = num_points or 1
        points = [{"_id": self.next_id + index, "x": self.next_id + index} for index in range(count)]
        self.next_id += count
        return points

    def ingest(self, points: Sequence[Mapping[str, Any]]) -> None:
        self.ingested.append(list(points))

    def get_best_points(self) -> Sequence[tuple[Any, Mapping[str, Any], Mapping[str, Any]]]:
        return [(0, {"x": 0}, {"score": 1})]

    def register_suggestions(self, suggestions: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
        registered = []
        for suggestion in suggestions:
            registered.append({"_id": self.next_id, **suggestion})
            self.next_id += 1
        return registered

    def register_failures(self, suggestions: Sequence[Mapping[str, Any]]) -> None:
        self.failures.append(list(suggestions))


def make_problem(params: ProblemParams) -> OptimizationProblem[str]:
    optimizer = FakeOptimizer()

    def evaluate(*, uid: str, suggestions: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
        return [{"_id": point["_id"], "score": point["x"] + params.x} for point in suggestions]

    return OptimizationProblem(optimizer=optimizer, actuators=[], sensors=[], evaluation_function=evaluate)


def make_queueserver_problem(params: ProblemParams) -> QueueserverOptimizationProblem:
    optimizer = FakeOptimizer()

    def evaluate(*, uid: str, suggestions: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
        return [{"_id": point["_id"], "score": point["x"] + params.x} for point in suggestions]

    return QueueserverOptimizationProblem(
        optimizer=optimizer,
        actuators=["motor"],
        sensors=["detector"],
        evaluation_function=evaluate,
        acquisition_plan="acquire",
    )


@pytest.fixture
def build_calls() -> list[ProblemParams]:
    return []


@pytest.fixture
def manager(build_calls: list[ProblemParams]) -> OptimizationManager:
    def build(params: ProblemParams) -> OptimizationProblem[str]:
        build_calls.append(params)
        return make_problem(params)

    return OptimizationManager(Catalog([ProblemTemplate("fake", ProblemParams, build)]), InMemoryOptimizationRepository())


@pytest.fixture
def queueserver() -> MagicMock:
    client = MagicMock(spec=QueueserverClient)
    client.submit_plan.side_effect = lambda plan: f"item-{client.submit_plan.call_count}"
    client.remove_item.return_value = False
    return client


@pytest.fixture
def qmanager(queueserver: MagicMock) -> OptimizationManager:
    return OptimizationManager(
        Catalog([ProblemTemplate("queue", ProblemParams, make_queueserver_problem)]),
        InMemoryOptimizationRepository(),
        queueserver=queueserver,
        history_poll_interval=None,
    )
