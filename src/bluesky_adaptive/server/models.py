"""Records and request bodies of the optimization server."""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

SuggestionId = int | str
"""
An optimizer's ``"_id"`` for a suggestion, as exposed over the API.

The protocols allow any hashable ``"_id"``; the server supports the JSON-representable ones.
"""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentSpec(_Model):
    """
    What an agent builds: a registered problem template and its parameters.

    Parameters
    ----------
    problem : str
        The name of a registered :class:`~bluesky_adaptive.server.ProblemTemplate`.
    params : dict[str, JsonValue]
        Parameters validated by the template. Stored in the canonical form the template's model produces.
    description : str
        A human-readable description.
    """

    problem: str
    params: dict[str, JsonValue] = Field(default_factory=dict)
    description: str = ""


class AgentConfig(AgentSpec):
    """A named agent: saved configuration that optimizations start from."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=64)


class AgentRecord(AgentConfig):
    """A saved agent."""

    created_at: datetime
    updated_at: datetime


class OptimizationStatus(StrEnum):
    """
    The state of an optimization.

    ``active``
        Open and idle: accepts manual operations and queueserver executions.
    ``running``
        A queueserver execution is in progress.
    ``stopping``
        Stop requested; waiting for the in-flight acquisition to be ingested.
    ``stopped``
        Ended by a client. Terminal.
    ``interrupted``
        The server stopped while the optimization was open. Terminal; never resumed automatically.
    """

    ACTIVE = "active"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        """Whether the optimization has ended."""
        return self in (OptimizationStatus.STOPPED, OptimizationStatus.INTERRUPTED)


class ExecutionKind(StrEnum):
    """What a queueserver execution does."""

    RUN = "run"
    """Suggest, acquire, evaluate, and ingest for a number of iterations."""
    ACQUIRE = "acquire"
    """Acquire, evaluate, and ingest given points once."""


class ExecutionStatus(StrEnum):
    """The state of a queueserver execution."""

    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ExecutionRecord(_Model):
    """One queueserver execution within an optimization."""

    number: int
    kind: ExecutionKind
    status: ExecutionStatus
    iterations: int
    num_points: int
    iterations_completed: int = 0
    run_uids: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime
    ended_at: datetime | None = None


class OptimizationRecord(_Model):
    """
    One optimization session: a configuration snapshot, its executions, and its lifecycle.

    The live optimizer exists only while the optimization is open; trials are stored separately.
    """

    id: str
    agent: str
    config: AgentConfig
    status: OptimizationStatus
    created_at: datetime
    ended_at: datetime | None = None
    executions: list[ExecutionRecord] = Field(default_factory=list)


class TrialStatus(StrEnum):
    """The state of a trial."""

    SUGGESTED = "suggested"
    """Suggested, possibly being acquired; no outcomes yet."""
    EVALUATED = "evaluated"
    """Outcomes computed but not ingested yet."""
    COMPLETED = "completed"
    """Outcomes ingested by the optimizer."""
    FAILED = "failed"
    """Acquisition or evaluation failed."""
    ABANDONED = "abandoned"
    """The optimization ended before the trial got outcomes."""


class TrialRecord(_Model):
    """
    One point: its parameters and, once evaluated, its outcomes.

    Parameter and outcome values are any JSON values, not only floats.
    """

    number: int
    suggestion_id: SuggestionId | None
    status: TrialStatus
    parameters: dict[str, JsonValue]
    outcomes: dict[str, JsonValue] | None = None
    acquisition_uid: JsonValue = None
    execution: int | None = None
    iteration: int | None = None
    created_at: datetime
    updated_at: datetime


class RunReference(_Model):
    """A Bluesky run acquired by an optimization."""

    run_uid: str
    execution: int
    iteration: int


class OptimizationStatusView(_Model):
    """A compact live view of an optimization."""

    id: str
    agent: str
    status: OptimizationStatus
    execution: ExecutionRecord | None
    trials: dict[TrialStatus, int]


class BestPoint(_Model):
    """A best point reported by the optimizer."""

    suggestion_id: JsonValue
    parameters: dict[str, JsonValue]
    outcomes: dict[str, JsonValue]


class StartOptimizationRequest(_Model):
    """Options for starting an optimization. Every optimization starts with a fresh optimizer."""


class SuggestRequest(_Model):
    """Request suggestions without acquiring them."""

    num_points: int = Field(default=1, ge=1)


class EvaluateRequest(_Model):
    """
    Evaluate suggested trials from data the client acquired, then ingest the outcomes.

    Parameters
    ----------
    uid : JsonValue
        The acquisition UID passed to the evaluation function, for example a Bluesky run UID.
    suggestion_ids : list[SuggestionId] | None
        The suggested trials the acquisition covered. Default: every suggested trial.
    """

    uid: JsonValue
    suggestion_ids: list[SuggestionId] | None = None


class IngestPoint(_Model):
    """
    An evaluated point.

    Parameters
    ----------
    suggestion_id : SuggestionId | None
        The ``"_id"`` of a suggested trial; its parameters are known. Omit for external data.
    parameters : dict[str, JsonValue]
        The parameter values. Required for external data.
    outcomes : dict[str, JsonValue]
        The measured outcomes.
    """

    suggestion_id: SuggestionId | None = None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    outcomes: dict[str, JsonValue]

    @model_validator(mode="after")
    def _external_points_have_parameters(self) -> Self:
        if self.suggestion_id is None and not self.parameters:
            raise ValueError("A point without a suggestion_id must have parameters")
        return self


class IngestRequest(_Model):
    """Ingest evaluated points."""

    points: list[IngestPoint] = Field(min_length=1)


class RunRequest(_Model):
    """Run the optimization loop through the queueserver."""

    iterations: int = Field(default=1, ge=1)
    num_points: int = Field(default=1, ge=1)
    checkpoint_interval: int | None = Field(default=None, ge=1)


class AcquireRequest(_Model):
    """
    Acquire given points through the queueserver once.

    Give exactly one of ``suggestion_ids`` (suggested trials) or ``parameters`` (new points,
    which the optimizer must be able to register).
    """

    suggestion_ids: list[SuggestionId] | None = Field(default=None, min_length=1)
    parameters: list[dict[str, JsonValue]] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if (self.suggestion_ids is None) == (self.parameters is None):
            raise ValueError("Give exactly one of suggestion_ids or parameters")
        return self


class AnalysisRequest(_Model):
    """Parameters of an analysis, validated by the analysis method."""

    params: dict[str, JsonValue] = Field(default_factory=dict)


class HealthCheckResult(_Model):
    """The result of one dependency check."""

    ok: bool
    detail: str = ""


class HealthReport(_Model):
    """Overall health and the result of every dependency check."""

    ok: bool
    checks: dict[str, HealthCheckResult]
