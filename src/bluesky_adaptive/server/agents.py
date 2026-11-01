"""
Agents: building them from specs, and the per-agent state the API reports.

.. warning::

    This module is **experimental**. The API is not yet stable and may change
    in future releases without a deprecation period.
"""

import logging
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from ..protocols import (
    CanRegisterSuggestions,
    Checkpointable,
    ContextAware,
    ContextFunction,
    EvaluationFunction,
    Optimizer,
    QueueserverOptimizationProblem,
    TrialFaultAware,
)
from ..queueserver import IterationRecord, OptimizationResult, QueueserverClient, QueueserverOptimizationRunner
from .catalog import Catalog, ContextEntry, IngestionEntry, RunIngestor
from .connections import Connections, QueueserverConnection, acquisition_contract_problems, flatten_devices
from .errors import ConflictError, InvalidRequestError, NotFoundError, ServerError
from .ingestion import IngestionManager
from .points import json_native, validate_points, validate_suggestions
from .problem import EntryConfig, ProblemDefinition, ProblemSpec

logger = logging.getLogger(__name__)

OperationKind = Literal["run", "submit-suggestions"]
"""Operations that acquire data through the queueserver."""

_ACTIVE = ("running", "stopping")
_EntryT = TypeVar("_EntryT")


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


@dataclass
class _Operation:
    id: str
    kind: OperationKind
    request: dict[str, Any]
    started_at: float
    state: str = "running"
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class _ContextLookup:
    """The context entry's function, restricted to the declared features; unknown runs raise ``ValueError``."""

    def __init__(self, function: ContextFunction[str], features: Sequence[str]):
        self._function = function
        self._features = tuple(features)

    def __call__(self, uid: str) -> Mapping[str, Any]:
        try:
            features = dict(self._function(uid))
        except KeyError as err:
            raise ValueError(f"no context for run {uid!r}: {err}") from err
        missing = sorted(set(self._features) - set(features))
        if missing:
            raise ValueError(f"context of run {uid!r} misses features {missing}")
        return json_native({name: features[name] for name in self._features})


class ManagedAgent:
    """
    One agent: its problem, optimizer, runner, optional ingestion and history.

    Every optimizer call holds :attr:`lock`, shared by the runner, ingestion and the API.

    Parameters
    ----------
    agent_id : str
        Agent ID, also the owner of the runs its runner claims.
    spec : ProblemSpec
        The client's spec.
    problem : ProblemDefinition
        The validated problem.
    entries : Mapping[str, Mapping[str, str]]
        Name and version of each catalog entry used, by kind.
    optimizer : Optimizer
        The built optimizer.
    evaluation : EvaluationFunction[str]
        The built evaluation function.
    context_function : ContextFunction[str] | None
        The built context function, for contextual agents.
    ingestor : RunIngestor | None
        The built ingestor, for agents that learn from other runs.
    connection : QueueserverConnection
        The agent's queueserver.
    no_data_errors : tuple[type[Exception], ...]
        Exceptions meaning the optimizer has too little data; reported with 409.
    """

    def __init__(
        self,
        agent_id: str,
        spec: ProblemSpec,
        problem: ProblemDefinition,
        entries: Mapping[str, Mapping[str, str]],
        optimizer: Optimizer,
        evaluation: EvaluationFunction[str],
        context_function: ContextFunction[str] | None,
        ingestor: RunIngestor | None,
        connection: QueueserverConnection,
        no_data_errors: tuple[type[Exception], ...] = (),
    ):
        self.id = agent_id
        self.spec = spec
        self.problem = problem
        self.entries = {kind: dict(entry) for kind, entry in entries.items()}
        self.optimizer = optimizer
        self.connection = connection
        self.created_at = time.time()
        self.lock = threading.RLock()
        self._no_data_errors = no_data_errors
        self._context_function = (
            _ContextLookup(context_function, problem.context_features) if context_function is not None else None
        )
        self._busy = threading.Lock()
        self._history_lock = threading.Lock()
        self._operations: list[_Operation] = []
        self._iterations: list[dict[str, Any]] = []
        self._current: _Operation | None = None
        self.runner = QueueserverOptimizationRunner(
            QueueserverOptimizationProblem(
                optimizer=optimizer,
                actuators=problem.actuators,
                sensors=problem.sensors,
                evaluation_function=evaluation,
                acquisition_plan=problem.plan_name,
                acquisition_plan_kwargs=dict(problem.plan_kwargs),
                context_function=self._context_function,
            ),
            QueueserverClient(connection.api),
            connection.router,
            name=agent_id,
            optimizer_lock=self.lock,
            on_iteration=self._on_iteration,
        )
        self.ingestion = (
            IngestionManager(agent_id, problem, ingestor, optimizer, self.lock, connection.router, self._context_function)
            if ingestor is not None
            else None
        )

    @property
    def capabilities(self) -> dict[str, bool]:
        """Optional behaviors this agent supports."""
        return {
            "contextual": self._context_function is not None,
            "register_suggestions": isinstance(self.optimizer, CanRegisterSuggestions),
            "checkpointable": isinstance(self.optimizer, Checkpointable),
            "trial_fault_aware": isinstance(self.optimizer, TrialFaultAware),
            "ingestion": self.ingestion is not None,
        }

    def info(self) -> dict[str, Any]:
        """Everything the API reports about the agent."""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "spec": self.spec.model_dump(mode="json"),
            "outputs": list(self.problem.outputs),
            "context_features": list(self.problem.context_features),
            "entries": self.entries,
            "capabilities": self.capabilities,
            "status": self.status(),
            "ingestion": self.ingestion.summary() if self.ingestion is not None else None,
        }

    def status(self) -> dict[str, Any]:
        """Runner state, the current or last operation and the in-flight iteration."""
        state = self.runner.state
        current_iteration = self.runner.current_iteration
        in_flight = self.runner.in_flight
        with self._history_lock:
            operation = asdict(self._current) if self._current is not None else None
        return {
            "state": state,
            "operation": operation,
            "current_iteration": current_iteration,
            "in_flight": json_native(asdict(in_flight)) if in_flight is not None else None,
        }

    def history(self) -> dict[str, Any]:
        """Every operation and iteration, oldest first."""
        with self._history_lock:
            return {
                "operations": [asdict(operation) for operation in self._operations],
                "iterations": list(self._iterations),
            }

    def run(
        self, iterations: int, n_points: int = 1, checkpoint_interval: int | None = None, context: str | None = None
    ) -> dict[str, Any]:
        """
        Start an optimization loop; see :meth:`QueueserverOptimizationRunner.run`.

        Parameters
        ----------
        iterations : int
            Number of iterations.
        n_points : int
            Points per iteration.
        checkpoint_interval : int | None
            Iterations between optimizer checkpoints.
        context : str | None
            UID of the run whose context the first iteration is suggested in; required exactly
            for contextual agents. Later iterations use the agent's own previous run.

        Returns
        -------
        dict[str, Any]
            The new operation.
        """
        request = {
            "iterations": iterations,
            "n_points": n_points,
            "checkpoint_interval": checkpoint_interval,
            "context": context,
        }
        self._check_context(context)
        with self.exclusive("run"):
            return self._start(
                "run",
                request,
                lambda: self.runner.run(
                    iterations=iterations,
                    num_points=n_points,
                    checkpoint_interval=checkpoint_interval,
                    context_uid=context,
                ),
            )

    def submit_suggestions(self, suggestions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """
        Acquire the given points in one iteration; see :meth:`QueueserverOptimizationRunner.submit_suggestions`.

        Every point needs every DOF, within bounds: the plan moves the actuators there.

        Returns
        -------
        dict[str, Any]
            The new operation.
        """
        try:
            checked = validate_suggestions(suggestions, self.problem)
        except ValueError as err:
            raise InvalidRequestError(str(err)) from err
        with self.exclusive("submit-suggestions"):
            return self._start(
                "submit-suggestions", {"suggestions": checked}, lambda: self.runner.submit_suggestions(checked)
            )

    def stop(self) -> dict[str, Any]:
        """Submit nothing more; see :meth:`QueueserverOptimizationRunner.stop`."""
        self.runner.stop()
        return self.status()

    def suggest(self, n_points: int = 1, context: str | None = None) -> dict[str, Any]:
        """
        Suggest points without acquiring them.

        Parameters
        ----------
        n_points : int
            Number of points.
        context : str | None
            UID of the run whose context to suggest in; required exactly for contextual agents.

        Returns
        -------
        dict[str, Any]
            ``{"suggestions": [...], "context_uid": ..., "context": {...}}``.
        """
        self._check_context(context)
        with self.exclusive("suggest"):
            features = None
            if self._context_function is not None and context is not None:
                try:
                    features = self._context_function(context)
                except ValueError as err:
                    raise InvalidRequestError(str(err)) from err
            with self.lock, self._optimizer_errors():
                if features is None:
                    suggestions = self.optimizer.suggest(n_points)
                elif isinstance(self.optimizer, ContextAware):
                    suggestions = self.optimizer.suggest_in_context(features, n_points)
                else:
                    raise TypeError("a contextual agent needs a ContextAware optimizer")
        return {
            "suggestions": json_native([dict(suggestion) for suggestion in suggestions]),
            "context_uid": context,
            "context": features,
        }

    def ingest(self, points: Sequence[Mapping[str, Any]]) -> None:
        """
        Ingest points measured elsewhere, or outcomes of suggestions by ``"_id"``.

        Raises
        ------
        InvalidRequestError
            If the points do not fit the problem or the optimizer rejects them.
        """
        try:
            checked = validate_points(points, self.problem, allow_ids=True)
        except ValueError as err:
            raise InvalidRequestError(str(err)) from err
        with self.lock, self._optimizer_errors():
            self.optimizer.ingest(checked)

    def best_points(self) -> list[dict[str, Any]]:
        """
        Return the optimizer's best points.

        Returns
        -------
        list[dict[str, Any]]
            ``[{"_id": ..., "parameters": {...}, "outcomes": {...}}]``.

        Raises
        ------
        ConflictError
            If the optimizer has no best point yet.
        """
        with self.lock:
            try:
                best = self.optimizer.get_best_points()
            except (ValueError, *self._no_data_errors) as err:
                raise ConflictError(f"no best points yet: {_describe(err)}") from err
        return [
            {"_id": json_native(point_id), "parameters": json_native(parameters), "outcomes": json_native(outcomes)}
            for point_id, parameters, outcomes in best
        ]

    def close(self) -> None:
        """Stop ingestion and the runner."""
        if self.ingestion is not None:
            self.ingestion.close()
        self.runner.close()

    @contextmanager
    def exclusive(self, action: str) -> Generator[None, None, None]:
        """
        Hold the agent for ``action``.

        Raises
        ------
        ConflictError
            If another request holds the agent, or an operation is running or stopping.
        """
        if not self._busy.acquire(blocking=False):
            raise ConflictError(f"agent {self.id} is busy with another request; retry {action} later")
        try:
            state = self.runner.state
            if state in _ACTIVE:
                raise ConflictError(f"agent {self.id} is {state}; stop it or wait before {action}")
            yield
        finally:
            self._busy.release()

    @contextmanager
    def _optimizer_errors(self) -> Generator[None, None, None]:
        try:
            yield
        except ServerError:
            raise
        except self._no_data_errors as err:
            raise ConflictError(f"the optimizer has too little data: {_describe(err)}") from err
        except (ValueError, KeyError) as err:
            raise InvalidRequestError(_describe(err)) from err

    def _check_context(self, context: str | None) -> None:
        if self._context_function is None and context is not None:
            raise InvalidRequestError(f"agent {self.id} has no context entry; do not give a context")
        if self._context_function is not None and context is None:
            raise InvalidRequestError(
                f"agent {self.id} is contextual; give 'context', the uid of the run whose conditions to suggest for"
            )

    def _start(
        self, kind: OperationKind, request: dict[str, Any], start: Callable[[], Future[OptimizationResult]]
    ) -> dict[str, Any]:
        operation = _Operation(id=uuid.uuid4().hex[:8], kind=kind, request=json_native(request), started_at=time.time())
        previous_future = self.runner.future
        with self._history_lock:
            previous_operation, self._current = self._current, operation
        try:
            try:
                future = start()
            except ServerError:
                raise
            except self._no_data_errors as err:
                raise ConflictError(f"the optimizer has too little data: {_describe(err)}") from err
            except RuntimeError as err:
                # The runner raises RuntimeError when it cannot start: busy, closed, or environment closed.
                raise ConflictError(str(err)) from err
            except ValueError as err:
                raise InvalidRequestError(str(err)) from err
        except Exception:
            begun = self.runner.future
            if begun is None or begun is previous_future:
                # Rejected before the runner began an operation: nothing to record.
                with self._history_lock:
                    self._current = previous_operation
            else:
                self._track(operation, begun)
            raise
        self._track(operation, future)
        with self._history_lock:
            return asdict(operation)

    def _track(self, operation: _Operation, future: Future[OptimizationResult]) -> None:
        with self._history_lock:
            self._operations.append(operation)
        future.add_done_callback(lambda done: self._finish_operation(operation, done))

    def _finish_operation(self, operation: _Operation, future: Future[OptimizationResult]) -> None:
        exc = future.exception()
        # The runner sets its final state before resolving the future; a newer operation replaces both.
        if self.runner.future is future:
            state: str = self.runner.state
        else:
            state = "failed" if exc is not None else "completed"
        with self._history_lock:
            operation.state = state
            operation.finished_at = time.time()
            if exc is not None:
                operation.error = _describe(exc)
            else:
                operation.result = json_native(asdict(future.result()))

    def _on_iteration(self, record: IterationRecord) -> None:
        with self._history_lock:
            operation_id = self._current.id if self._current is not None else None
            self._iterations.append({"operation": operation_id, **json_native(asdict(record))})
        if self.ingestion is not None and record.run_uid is not None and record.error is None:
            self.ingestion.mark_ingested(record.run_uid)


@dataclass(frozen=True)
class _Choice(Generic[_EntryT]):
    """A catalog entry chosen by a spec, with its validated params."""

    entry: _EntryT
    config: EntryConfig


def _choose(
    entries: Mapping[str, _EntryT],
    name: str,
    data: Mapping[str, Any],
    label: str,
    model: Callable[[_EntryT], type[BaseModel]],
) -> _Choice[_EntryT]:
    try:
        entry = entries[name]
    except KeyError:
        raise InvalidRequestError(f"unknown {label} {name!r}; available: {sorted(entries)}") from None
    try:
        params = model(entry).model_validate(data)
    except ValidationError as err:
        raise InvalidRequestError(f"invalid params for {label} {name!r}: {err}") from err
    return _Choice(entry, EntryConfig(name, params))


@contextmanager
def _build_errors(label: str) -> Generator[None, None, None]:
    try:
        yield
    except (ValueError, ValidationError) as err:
        raise InvalidRequestError(f"building {label} failed: {err}") from err


class AgentRegistry:
    """
    Builds agents from specs and holds them.

    Parameters
    ----------
    catalog : Catalog
        The application's entries.
    connections : Connections
        Queueserver connections.
    storage_dir : Path
        Agent files go in ``storage_dir / "agents" / <id>``.
    """

    def __init__(self, catalog: Catalog, connections: Connections, storage_dir: Path):
        self._catalog = catalog
        self._connections = connections
        self._storage_dir = Path(storage_dir)
        self._lock = threading.Lock()
        self._agents: dict[str, ManagedAgent] = {}

    def __getitem__(self, agent_id: str) -> ManagedAgent:
        """Return the agent; raise :class:`NotFoundError` if there is none."""
        with self._lock:
            agent = self._agents.get(agent_id)
        if agent is None:
            raise NotFoundError(f"unknown agent {agent_id!r}")
        return agent

    def __iter__(self) -> Iterator[ManagedAgent]:
        """Iterate over the agents, oldest first."""
        with self._lock:
            return iter(list(self._agents.values()))

    def create(self, spec: ProblemSpec) -> ManagedAgent:
        """
        Validate a spec against the catalog and the queueserver, build its entries and register the agent.

        Parameters
        ----------
        spec : ProblemSpec
            The client's spec.

        Returns
        -------
        ManagedAgent
            The new agent.

        Raises
        ------
        InvalidRequestError
            If the spec does not fit the catalog, the queueserver's plans and devices, or a build fails.
        bluesky_queueserver_api.comm_base.RequestTimeoutError
            If the queueserver does not answer.
        """
        catalog = self._catalog
        try:
            connection = self._connections[spec.queueserver]
        except NotFoundError as err:
            raise InvalidRequestError(str(err)) from err

        evaluation = _choose(
            catalog.evaluations, spec.evaluation.name, spec.evaluation.params, "evaluation", lambda entry: entry.params
        )
        optimizer = _choose(
            catalog.optimizers, spec.optimizer.name, spec.optimizer.options, "optimizer", lambda entry: entry.options
        )
        context: _Choice[ContextEntry[Any]] | None = None
        if spec.context is not None:
            context = _choose(
                catalog.contexts, spec.context.name, spec.context.params, "context", lambda entry: entry.params
            )
        ingestion: _Choice[IngestionEntry[Any]] | None = None
        if spec.ingestion is not None:
            ingestion = _choose(
                catalog.ingestions, spec.ingestion.name, spec.ingestion.params, "ingestion", lambda entry: entry.params
            )

        with _build_errors(f"the outputs of evaluation {evaluation.config.name!r}"):
            outputs = list(evaluation.entry.outputs(evaluation.config.params))
        features: list[str] = []
        if context is not None:
            with _build_errors(f"the features of context {context.config.name!r}"):
                features = list(context.entry.features(context.config.params))
        if ingestion is not None and ingestion.entry.outputs is not None:
            with _build_errors(f"the outputs of ingestion {ingestion.config.name!r}"):
                unknown = sorted(set(ingestion.entry.outputs(ingestion.config.params)) - set(outputs))
            if unknown:
                raise InvalidRequestError(f"ingestion outputs {unknown} are not evaluation outputs {sorted(outputs)}")

        self._check_plan(connection, spec)
        self._check_devices(connection, spec)
        try:
            problem = ProblemDefinition.from_spec(
                spec,
                outputs=outputs,
                context_features=features,
                evaluation=evaluation.config,
                optimizer=optimizer.config,
                context=context.config if context is not None else None,
                ingestion=ingestion.config if ingestion is not None else None,
            )
        except ValueError as err:
            raise InvalidRequestError(str(err)) from err

        entries = {
            kind: {"name": choice.entry.name, "version": choice.entry.version}
            for kind, choice in (
                ("evaluation", evaluation),
                ("optimizer", optimizer),
                ("context", context),
                ("ingestion", ingestion),
            )
            if choice is not None
        }
        agent_id = uuid.uuid4().hex[:8]
        agent_dir = self._storage_dir / "agents" / agent_id
        agent_dir.mkdir(parents=True)
        try:
            with _build_errors(f"evaluation {evaluation.config.name!r}"):
                evaluation_function = evaluation.entry.build(problem, evaluation.config.params)
            context_function = None
            if context is not None:
                with _build_errors(f"context {context.config.name!r}"):
                    context_function = context.entry.build(problem, context.config.params)
            ingestor = None
            if ingestion is not None:
                with _build_errors(f"ingestion {ingestion.config.name!r}"):
                    ingestor = ingestion.entry.build(problem, ingestion.config.params)
            with _build_errors(f"optimizer {optimizer.config.name!r}"):
                built_optimizer = optimizer.entry.build(problem, optimizer.config.params, agent_dir / "optimizer")
            if context is not None and not isinstance(built_optimizer, ContextAware):
                raise InvalidRequestError(
                    f"context {context.config.name!r} needs a context-aware optimizer; "
                    f"optimizer {optimizer.config.name!r} is not"
                )
            agent = ManagedAgent(
                agent_id,
                spec,
                problem,
                entries,
                built_optimizer,
                evaluation_function,
                context_function,
                ingestor,
                connection,
                no_data_errors=optimizer.entry.no_data_errors,
            )
        except Exception:
            shutil.rmtree(agent_dir, ignore_errors=True)
            raise
        with self._lock:
            self._agents[agent_id] = agent
        logger.info("Created agent %s on queueserver %r", agent_id, spec.queueserver)
        return agent

    def delete(self, agent_id: str) -> None:
        """
        Close and drop an agent.

        Raises
        ------
        ConflictError
            While the agent runs or handles another request.
        """
        agent = self[agent_id]
        with agent.exclusive("delete"):
            with self._lock:
                self._agents.pop(agent_id, None)
            agent.close()
        logger.info("Deleted agent %s", agent_id)

    def close(self) -> None:
        """Close every agent."""
        with self._lock:
            agents = list(self._agents.values())
            self._agents.clear()
        for agent in agents:
            try:
                agent.close()
            except Exception:
                logger.exception("Failed to close agent %s", agent.id)

    @staticmethod
    def _check_plan(connection: QueueserverConnection, spec: ProblemSpec) -> None:
        plans = connection.api.plans_allowed().get("plans_allowed") or {}
        name = spec.acquisition_plan.name
        if name not in plans:
            raise InvalidRequestError(
                f"plan {name!r} is not allowed for user group {connection.settings.user_group!r} on queueserver "
                f"{connection.name!r}; allowed: {sorted(plans)}"
            )
        problems = acquisition_contract_problems(plans[name], spec.acquisition_plan.kwargs)
        if problems:
            raise InvalidRequestError(f"plan {name!r} cannot be the acquisition plan: {'; '.join(problems)}")

    @staticmethod
    def _check_devices(connection: QueueserverConnection, spec: ProblemSpec) -> None:
        devices = flatten_devices(connection.api.devices_allowed().get("devices_allowed") or {})
        for dof in spec.dofs:
            if dof.actuator is None:
                continue
            flags = devices.get(dof.actuator)
            if flags is None:
                movable = sorted(path for path, flags in devices.items() if flags["is_movable"] and path.count(".") <= 1)
                raise InvalidRequestError(
                    f"actuator {dof.actuator!r} of DOF {dof.name!r} is not an allowed device; movable: {movable}"
                )
            if not (flags["is_movable"] or flags["is_flyable"]):
                raise InvalidRequestError(f"actuator {dof.actuator!r} of DOF {dof.name!r} is not movable")
        for sensor in spec.sensors:
            flags = devices.get(sensor)
            if flags is None:
                readable = sorted(path for path, flags in devices.items() if flags["is_readable"] and "." not in path)
                raise InvalidRequestError(f"sensor {sensor!r} is not an allowed device; readable devices: {readable}")
            if not flags["is_readable"]:
                raise InvalidRequestError(f"sensor {sensor!r} is not readable")
