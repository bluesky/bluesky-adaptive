"""Storage of agents, optimizations, and trials."""

import threading
from typing import Protocol

from .models import AgentRecord, OptimizationRecord, TrialRecord


class OptimizationRepository(Protocol):
    """
    The storage operations the optimization manager uses.

    The manager serializes its own calls; implementations need not be thread-safe for a single
    manager. Records are passed and returned by value: implementations must not hold on to a
    record they are given and later mutate it, and callers must not rely on mutating a returned record.
    """

    def list_agents(self) -> list[AgentRecord]:
        """Return every agent."""
        ...

    def get_agent(self, name: str) -> AgentRecord | None:
        """Return an agent, or ``None`` if there is none with the name."""
        ...

    def save_agent(self, agent: AgentRecord) -> None:
        """Insert or replace an agent by name."""
        ...

    def delete_agent(self, name: str) -> None:
        """Delete an agent. Its optimizations are kept."""
        ...

    def list_optimizations(self, agent: str | None = None) -> list[OptimizationRecord]:
        """Return optimizations, oldest first, optionally only those of one agent."""
        ...

    def get_optimization(self, optimization_id: str) -> OptimizationRecord | None:
        """Return an optimization, or ``None`` if there is none with the ID."""
        ...

    def save_optimization(self, optimization: OptimizationRecord) -> None:
        """Insert or replace an optimization by ID."""
        ...

    def list_trials(self, optimization_id: str) -> list[TrialRecord]:
        """Return the trials of an optimization ordered by number."""
        ...

    def save_trials(self, optimization_id: str, trials: list[TrialRecord]) -> None:
        """Insert or replace trials of an optimization by number."""
        ...


class InMemoryOptimizationRepository:
    """
    An :class:`OptimizationRepository` that keeps records in memory.

    Records do not survive a restart. Useful for tests, demonstrations, and servers whose
    history lives elsewhere.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agents: dict[str, AgentRecord] = {}
        self._optimizations: dict[str, OptimizationRecord] = {}
        self._trials: dict[str, dict[int, TrialRecord]] = {}

    def list_agents(self) -> list[AgentRecord]:
        """Return every agent."""
        with self._lock:
            return [agent.model_copy(deep=True) for agent in self._agents.values()]

    def get_agent(self, name: str) -> AgentRecord | None:
        """Return an agent, or ``None`` if there is none with the name."""
        with self._lock:
            agent = self._agents.get(name)
            return agent.model_copy(deep=True) if agent is not None else None

    def save_agent(self, agent: AgentRecord) -> None:
        """Insert or replace an agent by name."""
        with self._lock:
            self._agents[agent.name] = agent.model_copy(deep=True)

    def delete_agent(self, name: str) -> None:
        """Delete an agent. Its optimizations are kept."""
        with self._lock:
            self._agents.pop(name, None)

    def list_optimizations(self, agent: str | None = None) -> list[OptimizationRecord]:
        """Return optimizations, oldest first, optionally only those of one agent."""
        with self._lock:
            return [
                optimization.model_copy(deep=True)
                for optimization in self._optimizations.values()
                if agent is None or optimization.agent == agent
            ]

    def get_optimization(self, optimization_id: str) -> OptimizationRecord | None:
        """Return an optimization, or ``None`` if there is none with the ID."""
        with self._lock:
            optimization = self._optimizations.get(optimization_id)
            return optimization.model_copy(deep=True) if optimization is not None else None

    def save_optimization(self, optimization: OptimizationRecord) -> None:
        """Insert or replace an optimization by ID."""
        with self._lock:
            self._optimizations[optimization.id] = optimization.model_copy(deep=True)

    def list_trials(self, optimization_id: str) -> list[TrialRecord]:
        """Return the trials of an optimization ordered by number."""
        with self._lock:
            trials = self._trials.get(optimization_id, {})
            return [trials[number].model_copy(deep=True) for number in sorted(trials)]

    def save_trials(self, optimization_id: str, trials: list[TrialRecord]) -> None:
        """Insert or replace trials of an optimization by number."""
        with self._lock:
            stored = self._trials.setdefault(optimization_id, {})
            for trial in trials:
                stored[trial.number] = trial.model_copy(deep=True)
