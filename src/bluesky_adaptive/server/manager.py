"""The optimization manager: agents, optimization sessions, and their execution."""

import json
import logging
import threading
import traceback
import uuid
from collections import Counter
from collections.abc import Callable, Hashable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

import numpy as np
from pydantic import BaseModel, JsonValue, ValidationError

from ..protocols import ID_KEY, CanRegisterSuggestions, QueueserverOptimizationProblem
from ..queueserver import (
    AcquisitionRequest,
    IterationRecord,
    OptimizationResult,
    QueueserverClient,
    QueueserverOptimizationRunner,
)
from ..utils import _validate_outcomes
from .catalog import AnalysisMethod, Catalog, CatalogEntryDescription, Problem, ProblemTemplate
from .errors import ConflictError, InvalidRequestError, NotFoundError, ServerError, UnavailableError
from .events import EventBroker
from .models import (
    AgentConfig,
    AgentRecord,
    AgentSpec,
    BestPoint,
    ExecutionKind,
    ExecutionRecord,
    ExecutionStatus,
    IngestPoint,
    OptimizationRecord,
    OptimizationStatus,
    OptimizationStatusView,
    RunReference,
    SuggestionId,
    TrialRecord,
    TrialStatus,
)
from .repository import OptimizationRepository

logger = logging.getLogger(__name__)

_OPEN_TRIAL_STATUSES = (TrialStatus.SUGGESTED, TrialStatus.EVALUATED)
_SPEC_FIELDS = {"problem", "params", "description"}


def _now() -> datetime:
    return datetime.now(UTC)


def _describe(exc: BaseException) -> str:
    """Describe an exception: type, message, and notes."""
    return "".join(traceback.format_exception_only(exc)).strip()


def _to_json(value: Any) -> Any:
    """Convert NumPy values, tuples, and mappings produced by optimizers and evaluators to JSON values."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Mapping):
        return {str(key): _to_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_json(item) for item in value]
    return value


def _without_id(point: Mapping[str, Any]) -> dict[str, JsonValue]:
    return _to_json({key: value for key, value in point.items() if key != ID_KEY})


def _validation_detail(error: ValidationError) -> Any:
    return json.loads(error.json(include_url=False))


@dataclass
class _Session:
    """An open optimization and its live optimizer."""

    record: OptimizationRecord
    problem: Problem
    optimizer_lock: threading.RLock = field(default_factory=threading.RLock)
    trials: dict[int, TrialRecord] = field(default_factory=dict)
    pending: dict[Hashable, int] = field(default_factory=dict)
    """Trial numbers of trials without ingested outcomes, by suggestion ID."""
    runner: QueueserverOptimizationRunner | None = None
    execution: ExecutionRecord | None = None
    stop_requested: bool = False


class OptimizationManager:
    """
    Builds agents, admits operations, runs optimizations, records results, and exposes status.

    An *agent* is saved configuration: a registered problem template and its parameters. An
    *optimization* is one session started from an agent, with a snapshot of the agent's
    configuration, a freshly built optimizer, its trials, and its executions. Only one
    optimization may be open at a time. Within an open optimization, clients either drive the
    optimizer themselves (suggest, evaluate, ingest) or start queueserver executions that run the
    loop. An optimization ends when a client stops it, or when the server shuts down; optimizations
    left open by a previous server process are marked interrupted by :meth:`startup` and never resumed.

    Optimizer access is serialized: the queueserver runner, manual operations, best points, and
    analyses all hold the optimization's optimizer lock.

    Parameters
    ----------
    problems : Catalog[ProblemTemplate]
        The problem templates agents may use.
    repository : OptimizationRepository
        Stores agents, optimizations, and trials.
    queueserver : QueueserverClient | None, optional
        The queueserver that executions run on. Without it, only manual operations are available.
    analyses : Catalog[AnalysisMethod] | None, optional
        Analyses clients may compute on an open optimization's optimizer.
    events : EventBroker | None, optional
        Receives lifecycle events. A new broker is created if not given.
    history_poll_interval : float | None, optional
        Passed to each :class:`~bluesky_adaptive.queueserver.QueueserverOptimizationRunner`.
    document_timeout : float, optional
        Passed to each :class:`~bluesky_adaptive.queueserver.QueueserverOptimizationRunner`.
    """

    def __init__(
        self,
        problems: Catalog[ProblemTemplate[Any]],
        repository: OptimizationRepository,
        *,
        queueserver: QueueserverClient | None = None,
        analyses: Catalog[AnalysisMethod[Any]] | None = None,
        events: EventBroker | None = None,
        history_poll_interval: float | None = 2.0,
        document_timeout: float = 30.0,
    ):
        self.problems = problems
        self.analyses: Catalog[AnalysisMethod[Any]] = analyses if analyses is not None else Catalog()
        self.events = events if events is not None else EventBroker()
        self.queueserver = queueserver
        self._repository = repository
        self._history_poll_interval = history_poll_interval
        self._document_timeout = document_timeout
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()

    # Lifecycle

    def startup(self) -> None:
        """Mark optimizations left open by a previous server process interrupted."""
        with self._lock:
            for record in self._repository.list_optimizations():
                if record.status.is_terminal or record.id in self._sessions:
                    continue
                logger.warning(f"Optimization {record.id} was left {record.status}; marking it interrupted")
                trials = self._repository.list_trials(record.id)
                self._interrupt(record, {trial.number: trial for trial in trials})
                self._repository.save_trials(record.id, trials)
                self._repository.save_optimization(record)

    def shutdown(self) -> None:
        """
        End every open optimization as interrupted.

        A plan still waiting in the queue is removed; an acquisition already running finishes on the
        queueserver, but its outcomes are not ingested.
        """
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.runner is not None:
                session.runner.stop()
                session.runner.close()
        for session in sessions:
            with session.optimizer_lock, self._lock:
                if session.record.id in self._sessions:
                    self._end(session, OptimizationStatus.INTERRUPTED)

    # Agents

    def list_agents(self) -> list[AgentRecord]:
        """Return every agent."""
        return self._repository.list_agents()

    def get_agent(self, name: str) -> AgentRecord:
        """
        Return an agent.

        Raises
        ------
        NotFoundError
            If there is no agent with the name.
        """
        agent = self._repository.get_agent(name)
        if agent is None:
            raise NotFoundError(f"No agent named {name!r}")
        return agent

    def validate_agent(self, spec: AgentSpec) -> AgentSpec:
        """
        Validate an agent's problem and parameters by building the problem once.

        Returns
        -------
        AgentSpec
            The spec with its parameters in canonical form.

        Raises
        ------
        InvalidRequestError
            If the template is unknown, the parameters are invalid, or the template rejects them.
        """
        params, _ = self._build(spec, unknown_problem=InvalidRequestError)
        return spec.model_copy(update={"params": params})

    def create_agent(self, config: AgentConfig) -> AgentRecord:
        """
        Validate and save a new agent.

        Raises
        ------
        ConflictError
            If an agent with the name exists.
        InvalidRequestError
            If the configuration is invalid.
        """
        with self._lock:
            if self._repository.get_agent(config.name) is not None:
                raise ConflictError(f"An agent named {config.name!r} already exists")
            spec = self.validate_agent(config)
            now = _now()
            agent = AgentRecord(**spec.model_dump(include=_SPEC_FIELDS), name=config.name, created_at=now, updated_at=now)
            self._repository.save_agent(agent)
            return agent

    def update_agent(self, name: str, spec: AgentSpec) -> AgentRecord:
        """
        Validate and replace an agent's configuration. Open optimizations keep their snapshot.

        Raises
        ------
        NotFoundError
            If there is no agent with the name.
        InvalidRequestError
            If the configuration is invalid.
        """
        with self._lock:
            current = self.get_agent(name)
            spec = self.validate_agent(spec)
            agent = AgentRecord(
                **spec.model_dump(include=_SPEC_FIELDS), name=name, created_at=current.created_at, updated_at=_now()
            )
            self._repository.save_agent(agent)
            return agent

    def delete_agent(self, name: str) -> None:
        """
        Delete an agent. Its optimizations are kept.

        Raises
        ------
        NotFoundError
            If there is no agent with the name.
        """
        with self._lock:
            self.get_agent(name)
            self._repository.delete_agent(name)

    # Optimizations

    def start_optimization(self, agent_name: str) -> OptimizationRecord:
        """
        Start an optimization from an agent with a freshly built optimizer.

        Raises
        ------
        NotFoundError
            If there is no agent with the name.
        ConflictError
            If another optimization is open, or the agent's problem template is no longer registered.
        InvalidRequestError
            If the agent's configuration no longer builds.
        """
        with self._lock:
            agent = self.get_agent(agent_name)
            if self._sessions:
                session = next(iter(self._sessions.values()))
                raise ConflictError(
                    f"Optimization {session.record.id} of agent {session.record.agent!r} is {session.record.status}; "
                    "stop it before starting another"
                )
            _, problem = self._build(agent, unknown_problem=ConflictError)
            record = OptimizationRecord(
                id=uuid.uuid4().hex,
                agent=agent.name,
                config=AgentConfig.model_validate(agent.model_dump(include={"name", *_SPEC_FIELDS})),
                status=OptimizationStatus.ACTIVE,
                created_at=_now(),
            )
            self._sessions[record.id] = _Session(record=record, problem=problem)
            self._save(record)
            self._publish_optimization(record)
            return record.model_copy(deep=True)

    def list_optimizations(self, agent: str | None = None) -> list[OptimizationRecord]:
        """Return optimizations, oldest first, optionally only those of one agent."""
        return self._repository.list_optimizations(agent)

    def get_optimization(self, optimization_id: str) -> OptimizationRecord:
        """
        Return an optimization.

        Raises
        ------
        NotFoundError
            If there is no optimization with the ID.
        """
        record = self._repository.get_optimization(optimization_id)
        if record is None:
            raise NotFoundError(f"No optimization with ID {optimization_id!r}")
        return record

    def get_status(self, optimization_id: str) -> OptimizationStatusView:
        """Return a compact live view of an optimization."""
        with self._lock:
            session = self._sessions.get(optimization_id)
            if session is not None:
                record, trials = session.record, list(session.trials.values())
            else:
                record, trials = self.get_optimization(optimization_id), self._repository.list_trials(optimization_id)
            return OptimizationStatusView(
                id=record.id,
                agent=record.agent,
                status=record.status,
                execution=record.executions[-1] if record.executions else None,
                trials=dict(Counter(trial.status for trial in trials)),
            )

    def list_trials(self, optimization_id: str) -> list[TrialRecord]:
        """Return the trials of an optimization ordered by number."""
        self.get_optimization(optimization_id)
        return self._repository.list_trials(optimization_id)

    def list_runs(self, optimization_id: str) -> list[RunReference]:
        """Return the Bluesky runs acquired by an optimization's executions."""
        record = self.get_optimization(optimization_id)
        return [
            RunReference(run_uid=run_uid, execution=execution.number, iteration=iteration)
            for execution in record.executions
            for iteration, run_uid in enumerate(execution.run_uids, start=1)
        ]

    def snapshot(self, optimization_id: str | None = None) -> dict[str, Any]:
        """Return the open optimizations, or the requested one, for an event-stream snapshot."""
        with self._lock:
            if optimization_id is None:
                records = [session.record for session in self._sessions.values()]
            elif optimization_id in self._sessions:
                records = [self._sessions[optimization_id].record]
            else:
                records = [self.get_optimization(optimization_id)]
            return {"optimizations": [record.model_dump(mode="json") for record in records]}

    def stop(self, optimization_id: str) -> OptimizationRecord:
        """
        End an optimization.

        No further plans are submitted. If a queueserver acquisition is in flight, the optimization
        is ``stopping`` until its outcomes are ingested and then ``stopped``.

        Raises
        ------
        NotFoundError
            If there is no optimization with the ID.
        ConflictError
            If the optimization has already ended.
        """
        session = self._session(optimization_id)
        # Taking the optimizer lock first waits for a manual operation in progress, so the optimization never
        # ends underneath one; manual operations re-check the status once they hold the optimizer lock.
        with session.optimizer_lock, self._lock:
            if session.record.status.is_terminal:
                raise ConflictError(f"Optimization {optimization_id} is {session.record.status}")
            if session.record.status is OptimizationStatus.STOPPING:
                return session.record.model_copy(deep=True)
            session.stop_requested = True
            execution = session.execution
            if execution is None:
                self._end(session, OptimizationStatus.STOPPED)
                return session.record.model_copy(deep=True)
            session.record.status = OptimizationStatus.STOPPING
            execution.status = ExecutionStatus.STOPPING
            self._save(session.record)
            self._publish_optimization(session.record)
            self._publish_execution(session.record.id, execution)
            runner = session.runner
        if runner is not None:
            runner.stop()
        return self.get_optimization(optimization_id)

    # Manual operations

    def suggest(self, optimization_id: str, num_points: int) -> list[TrialRecord]:
        """
        Ask the optimizer for suggestions without acquiring them.

        Raises
        ------
        ConflictError
            If the optimization is not open and idle.
        """
        session = self._session(optimization_id)
        with session.optimizer_lock:
            self._require_idle(session)
            suggestions = session.problem.optimizer.suggest(num_points)
            with self._lock:
                trials = [self._add_trial(session, suggestion) for suggestion in suggestions]
                self._save_trials(session, trials)
            return [trial.model_copy(deep=True) for trial in trials]

    def evaluate(
        self, optimization_id: str, uid: JsonValue, suggestion_ids: Sequence[SuggestionId] | None = None
    ) -> list[TrialRecord]:
        """
        Evaluate suggested trials from data the client acquired, and ingest the outcomes.

        Parameters
        ----------
        optimization_id : str
            The optimization.
        uid : JsonValue
            The acquisition UID passed to the problem's evaluation function.
        suggestion_ids : Sequence[SuggestionId] | None, optional
            The suggested trials the acquisition covered. Default: every suggested trial not in an execution.

        Raises
        ------
        ConflictError
            If the optimization is not open and idle, or has no suggested trials.
        InvalidRequestError
            If a suggestion ID is not a suggested trial.
        ServerError
            If the evaluation function's outcomes do not match the suggestions.
        """
        session = self._session(optimization_id)
        with session.optimizer_lock:
            with self._lock:
                self._require_idle(session)
                if suggestion_ids is None:
                    trials = [
                        trial
                        for trial in session.trials.values()
                        if trial.status is TrialStatus.SUGGESTED and trial.execution is None
                    ]
                    if not trials:
                        raise ConflictError("There are no suggested trials to evaluate")
                else:
                    trials = self._pending_trials(session, suggestion_ids)
                suggestions = [{ID_KEY: trial.suggestion_id, **trial.parameters} for trial in trials]
            outcomes = session.problem.evaluation_function(uid=uid, suggestions=suggestions)
            try:
                _validate_outcomes(outcomes, suggestions)
            except (TypeError, ValueError) as err:
                raise ServerError(f"The evaluation function returned invalid outcomes: {err}") from err
            session.problem.optimizer.ingest(outcomes)
            with self._lock:
                completed = self._record_outcomes(session, outcomes, uid, TrialStatus.COMPLETED)
                self._save_trials(session, completed)
            return [trial.model_copy(deep=True) for trial in completed]

    def ingest(self, optimization_id: str, points: Sequence[IngestPoint]) -> list[TrialRecord]:
        """
        Ingest evaluated points: outcomes of suggested trials, or external data.

        Raises
        ------
        ConflictError
            If the optimization is not open and idle.
        InvalidRequestError
            If a suggestion ID is not a suggested trial or appears twice.
        """
        session = self._session(optimization_id)
        with session.optimizer_lock:
            with self._lock:
                self._require_idle(session)
                suggested = self._pending_trials(
                    session, [point.suggestion_id for point in points if point.suggestion_id is not None]
                )
                parameters = iter(trial.parameters for trial in suggested)
                optimizer_points = [
                    {ID_KEY: point.suggestion_id, **next(parameters), **point.outcomes}
                    if point.suggestion_id is not None
                    else {**point.parameters, **point.outcomes}
                    for point in points
                ]
            session.problem.optimizer.ingest(optimizer_points)
            with self._lock:
                now = _now()
                trials = []
                for point in points:
                    if point.suggestion_id is None:
                        trial = self._add_trial(session, point.parameters)
                    else:
                        trial = session.trials[session.pending.pop(point.suggestion_id)]
                    trial.status = TrialStatus.COMPLETED
                    trial.outcomes = _to_json(point.outcomes)
                    trial.updated_at = now
                    trials.append(trial)
                self._save_trials(session, trials)
            return [trial.model_copy(deep=True) for trial in trials]

    def best_points(self, optimization_id: str) -> list[BestPoint]:
        """
        Return the optimizer's best points.

        Raises
        ------
        ConflictError
            If the optimization has ended, so its optimizer is gone.
        """
        session = self._session(optimization_id)
        with session.optimizer_lock:
            best = session.problem.optimizer.get_best_points()
        return [
            BestPoint(suggestion_id=_to_json(suggestion_id), parameters=_to_json(parameters), outcomes=_to_json(outcomes))
            for suggestion_id, parameters, outcomes in best
        ]

    def list_analyses(self, optimization_id: str) -> list[CatalogEntryDescription]:
        """Return the analyses that apply to an open optimization's optimizer."""
        optimizer = self._session(optimization_id).problem.optimizer
        supported = Catalog(method for method in self.analyses if method.supports(optimizer))
        return supported.describe()

    def compute_analysis(self, optimization_id: str, name: str, params: Mapping[str, Any]) -> JsonValue:
        """
        Compute an analysis of an open optimization's optimizer.

        Raises
        ------
        NotFoundError
            If there is no analysis with the name.
        ConflictError
            If the analysis does not apply to the optimizer.
        InvalidRequestError
            If the parameters are invalid.
        """
        session = self._session(optimization_id)
        method = self.analyses.get(name)
        optimizer = session.problem.optimizer
        if not method.supports(optimizer):
            raise ConflictError(f"Analysis {name!r} does not apply to {type(optimizer).__name__}")
        parsed = self._parse(method.params, params)
        with session.optimizer_lock:
            return method.compute(optimizer, parsed)

    # Queueserver executions

    def run(
        self, optimization_id: str, iterations: int, num_points: int, checkpoint_interval: int | None = None
    ) -> ExecutionRecord:
        """
        Run the optimization loop through the queueserver.

        Returns immediately; progress is reported through the optimization's records and events.

        Raises
        ------
        UnavailableError
            If the server has no queueserver.
        ConflictError
            If the optimization is not open and idle, its problem is not a queueserver problem, or the
            queueserver does not accept the first plan.
        """
        return self._start_execution(
            optimization_id,
            ExecutionKind.RUN,
            iterations=iterations,
            num_points=num_points,
            checkpoint_interval=checkpoint_interval,
        )

    def acquire(
        self,
        optimization_id: str,
        *,
        suggestion_ids: Sequence[SuggestionId] | None = None,
        parameters: Sequence[Mapping[str, Any]] | None = None,
    ) -> ExecutionRecord:
        """
        Acquire suggested trials or new points through the queueserver once.

        Give exactly one of ``suggestion_ids`` and ``parameters``.

        Raises
        ------
        UnavailableError
            If the server has no queueserver.
        ConflictError
            If the optimization is not open and idle, its problem is not a queueserver problem, or the
            queueserver does not accept the plan.
        InvalidRequestError
            If a suggestion ID is not a suggested trial, or new points cannot be registered by the optimizer.
        """

        def suggestions(session: _Session) -> list[dict[str, Any]]:
            if suggestion_ids is not None:
                return [
                    {ID_KEY: trial.suggestion_id, **trial.parameters}
                    for trial in self._pending_trials(session, suggestion_ids)
                ]
            if not isinstance(session.problem.optimizer, CanRegisterSuggestions):
                raise InvalidRequestError(
                    f"{type(session.problem.optimizer).__name__} cannot register new points; acquire suggested trials"
                )
            return [dict(point) for point in parameters or ()]

        num_points = len(suggestion_ids) if suggestion_ids is not None else len(parameters or ())
        return self._start_execution(
            optimization_id, ExecutionKind.ACQUIRE, iterations=1, num_points=num_points, suggestions=suggestions
        )

    # Internals

    def _build(self, spec: AgentSpec, *, unknown_problem: type[ServerError]) -> tuple[dict[str, JsonValue], Problem]:
        """Build an agent's problem, returning its canonical parameters and the problem."""
        try:
            template = self.problems.get(spec.problem)
        except NotFoundError as err:
            raise unknown_problem(str(err)) from err
        params = self._parse(template.params, spec.params)
        try:
            problem = template.build(params)
        except ValueError as err:
            raise InvalidRequestError(f"Problem {spec.problem!r} rejected the parameters: {err}") from err
        return params.model_dump(mode="json"), problem

    @staticmethod
    def _parse(model: type[BaseModel], params: Mapping[str, Any]) -> Any:
        try:
            return model.model_validate(params)
        except ValidationError as err:
            raise InvalidRequestError(str(err), detail=_validation_detail(err)) from err

    def _session(self, optimization_id: str) -> _Session:
        """Return an open optimization's session."""
        with self._lock:
            session = self._sessions.get(optimization_id)
            if session is not None:
                return session
            record = self.get_optimization(optimization_id)
            raise ConflictError(f"Optimization {optimization_id} is {record.status}")

    def _require_idle(self, session: _Session) -> None:
        with self._lock:
            if session.record.status is not OptimizationStatus.ACTIVE:
                raise ConflictError(f"Optimization {session.record.id} is {session.record.status}")

    def _pending_trials(self, session: _Session, suggestion_ids: Sequence[SuggestionId]) -> list[TrialRecord]:
        """LOCKED: the suggested trials with the given IDs, not part of an execution."""
        if len(set(suggestion_ids)) != len(suggestion_ids):
            raise InvalidRequestError(f"Suggestion IDs must be unique, got {list(suggestion_ids)}")
        trials = []
        for suggestion_id in suggestion_ids:
            number = session.pending.get(suggestion_id)
            trial = session.trials[number] if number is not None else None
            if trial is None or trial.status is not TrialStatus.SUGGESTED or trial.execution is not None:
                raise InvalidRequestError(f"{suggestion_id!r} is not a suggested trial")
            trials.append(trial)
        return trials

    def _add_trial(
        self,
        session: _Session,
        point: Mapping[str, Any],
        *,
        execution: int | None = None,
        iteration: int | None = None,
    ) -> TrialRecord:
        """LOCKED: record a new suggested trial for a point with or without an ID."""
        now = _now()
        suggestion_id = point.get(ID_KEY)
        trial = TrialRecord(
            number=len(session.trials),
            suggestion_id=_to_json(suggestion_id),
            status=TrialStatus.SUGGESTED,
            parameters=_without_id(point),
            execution=execution,
            iteration=iteration,
            created_at=now,
            updated_at=now,
        )
        session.trials[trial.number] = trial
        if suggestion_id is not None:
            session.pending[suggestion_id] = trial.number
        return trial

    def _record_outcomes(
        self, session: _Session, outcomes: Sequence[Mapping[str, Any]], uid: JsonValue, status: TrialStatus
    ) -> list[TrialRecord]:
        """LOCKED: attach validated outcomes to their trials."""
        now = _now()
        trials = []
        for outcome in outcomes:
            trial = session.trials[session.pending[outcome[ID_KEY]]]
            trial.status = status
            trial.outcomes = _without_id(outcome)
            trial.acquisition_uid = _to_json(uid)
            trial.updated_at = now
            if status is TrialStatus.COMPLETED:
                del session.pending[outcome[ID_KEY]]
            trials.append(trial)
        return trials

    def _start_execution(
        self,
        optimization_id: str,
        kind: ExecutionKind,
        *,
        iterations: int,
        num_points: int,
        checkpoint_interval: int | None = None,
        suggestions: Callable[[_Session], list[dict[str, Any]]] | None = None,
    ) -> ExecutionRecord:
        """Admit and start a queueserver execution: a run if ``suggestions`` is None, else one acquisition."""
        if self.queueserver is None:
            raise UnavailableError("This server has no queueserver")
        queueserver = self.queueserver
        session = self._session(optimization_id)
        with self._lock:
            self._require_idle(session)
            problem = session.problem
            if not isinstance(problem, QueueserverOptimizationProblem):
                raise ConflictError(
                    f"Agent {session.record.agent!r} builds a {type(problem).__name__}, not a QueueserverOptimizationProblem"
                )
            points = suggestions(session) if suggestions is not None else None
            if session.runner is None:
                session.runner = QueueserverOptimizationRunner(
                    problem,
                    queueserver,
                    optimizer_lock=session.optimizer_lock,
                    on_submit=partial(self._on_submit, session),
                    on_evaluation=partial(self._on_evaluation, session),
                    on_iteration=partial(self._on_iteration, session),
                    history_poll_interval=self._history_poll_interval,
                    document_timeout=self._document_timeout,
                )
            runner = session.runner
            execution = ExecutionRecord(
                number=len(session.record.executions) + 1,
                kind=kind,
                status=ExecutionStatus.RUNNING,
                iterations=iterations,
                num_points=num_points,
                started_at=_now(),
            )
            session.record.executions.append(execution)
            session.execution = execution
            session.record.status = OptimizationStatus.RUNNING
            self._save(session.record)
            self._publish_optimization(session.record)
            self._publish_execution(session.record.id, execution)

        # Outside the manager lock: the runner calls back into the manager from its own threads.
        try:
            if points is None:
                future = runner.run(iterations=iterations, num_points=num_points, checkpoint_interval=checkpoint_interval)
            else:
                future = runner.submit_suggestions(points)
        except Exception as exc:
            self._finish_execution(session, execution.number, exc)
            error_type = InvalidRequestError if isinstance(exc, ValueError) else ConflictError
            raise error_type(f"The execution did not start: {exc}") from exc
        future.add_done_callback(partial(self._on_execution_done, session, execution.number))
        with self._lock:
            stop_requested = session.stop_requested
            started = execution.model_copy(deep=True)
        if stop_requested:
            # stop() arrived before the runner had started; it found nothing to stop.
            runner.stop()
        return started

    def _on_submit(self, session: _Session, request: AcquisitionRequest) -> None:
        with self._lock:
            execution = session.execution
            number = execution.number if execution is not None else None
            now = _now()
            trials = []
            for suggestion in request.suggestions:
                trial_number = session.pending.get(suggestion[ID_KEY])
                if trial_number is None:
                    trial = self._add_trial(session, suggestion, execution=number, iteration=request.iteration)
                else:
                    trial = session.trials[trial_number]
                    trial.execution, trial.iteration, trial.updated_at = number, request.iteration, now
                trials.append(trial)
            self._save_trials(session, trials)

    def _on_evaluation(self, session: _Session, record: IterationRecord) -> None:
        # Raising here fails the acquisition before anything is ingested.
        _validate_outcomes(record.outcomes, record.suggestions)
        with self._lock:
            self._save_trials(
                session, self._record_outcomes(session, record.outcomes, record.run_uid, TrialStatus.EVALUATED)
            )

    def _on_iteration(self, session: _Session, record: IterationRecord) -> None:
        with self._lock:
            self._save_trials(
                session, self._record_outcomes(session, record.outcomes, record.run_uid, TrialStatus.COMPLETED)
            )
            execution = session.execution
            if execution is not None:
                execution.iterations_completed += 1
                execution.run_uids.append(record.run_uid)
                self._save(session.record)
                self._publish_execution(session.record.id, execution)

    def _on_execution_done(self, session: _Session, number: int, future: Future[OptimizationResult]) -> None:
        self._finish_execution(session, number, future.exception())

    def _finish_execution(self, session: _Session, number: int, error: BaseException | None) -> None:
        with self._lock:
            execution = session.execution
            if execution is None or execution.number != number:
                return
            session.execution = None
            if error is not None:
                execution.status = ExecutionStatus.FAILED
                execution.error = _describe(error)
            elif session.stop_requested and execution.iterations_completed < execution.iterations:
                execution.status = ExecutionStatus.STOPPED
            else:
                execution.status = ExecutionStatus.COMPLETED
            execution.ended_at = _now()
            self._publish_execution(session.record.id, execution)
            leftover = TrialStatus.FAILED if error is not None else TrialStatus.ABANDONED
            self._close_trials(session, leftover, lambda trial: trial.execution == number)
            if session.stop_requested:
                self._end(session, OptimizationStatus.STOPPED)
            else:
                session.record.status = OptimizationStatus.ACTIVE
                self._save(session.record)
                self._publish_optimization(session.record)

    def _close_trials(self, session: _Session, status: TrialStatus, select: Callable[[TrialRecord], bool]) -> None:
        """LOCKED: give selected trials without ingested outcomes a final status."""
        now = _now()
        closed = []
        for suggestion_id, number in list(session.pending.items()):
            trial = session.trials[number]
            if select(trial):
                trial.status, trial.updated_at = status, now
                del session.pending[suggestion_id]
                closed.append(trial)
        self._save_trials(session, closed)

    def _end(self, session: _Session, status: OptimizationStatus) -> None:
        """LOCKED: end an open optimization and release its optimizer."""
        execution = session.execution
        if execution is not None:
            execution.status = ExecutionStatus.INTERRUPTED
            execution.ended_at = _now()
            self._publish_execution(session.record.id, execution)
            session.execution = None
        self._close_trials(session, TrialStatus.ABANDONED, lambda _: True)
        session.record.status = status
        session.record.ended_at = _now()
        del self._sessions[session.record.id]
        if session.runner is not None:
            session.runner.close()
        self._save(session.record)
        self._publish_optimization(session.record)

    @staticmethod
    def _interrupt(record: OptimizationRecord, trials: Mapping[int, TrialRecord]) -> None:
        """Mark an optimization from a previous server process interrupted, in place."""
        now = _now()
        record.status = OptimizationStatus.INTERRUPTED
        record.ended_at = now
        for execution in record.executions:
            if execution.status in (ExecutionStatus.RUNNING, ExecutionStatus.STOPPING):
                execution.status = ExecutionStatus.INTERRUPTED
                execution.ended_at = now
        for trial in trials.values():
            if trial.status in _OPEN_TRIAL_STATUSES:
                trial.status = TrialStatus.ABANDONED
                trial.updated_at = now

    def _save(self, record: OptimizationRecord) -> None:
        self._repository.save_optimization(record)

    def _save_trials(self, session: _Session, trials: list[TrialRecord]) -> None:
        """LOCKED: store trials and publish an event for each."""
        if not trials:
            return
        self._repository.save_trials(session.record.id, trials)
        for trial in trials:
            self.events.publish("trial", trial.model_dump(mode="json"), session.record.id)

    def _publish_optimization(self, record: OptimizationRecord) -> None:
        self.events.publish("optimization", record.model_dump(mode="json"), record.id)

    def _publish_execution(self, optimization_id: str, execution: ExecutionRecord) -> None:
        self.events.publish("execution", execution.model_dump(mode="json"), optimization_id)
