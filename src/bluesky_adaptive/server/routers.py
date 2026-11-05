"""
Mountable FastAPI routers for the optimization manager.

Each ``create_*_router`` function returns an independent :class:`fastapi.APIRouter`, so an
application mounts only the routers it needs, next to its own beamline-specific routers.
Routers translate :class:`~bluesky_adaptive.server.errors.ServerError` into HTTP responses
themselves and need no application-level exception handlers.
"""

import logging
from collections.abc import AsyncIterable, Callable, Coroutine, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import JsonValue

from .catalog import CatalogEntryDescription
from .errors import ServerError, UnavailableError
from .events import Event
from .manager import OptimizationManager
from .models import (
    AcquireRequest,
    AgentConfig,
    AgentRecord,
    AgentSpec,
    AnalysisRequest,
    BestPoint,
    EvaluateRequest,
    ExecutionRecord,
    HealthCheckResult,
    HealthReport,
    IngestRequest,
    OptimizationRecord,
    OptimizationStatusView,
    RunReference,
    RunRequest,
    SuggestRequest,
    TrialRecord,
)

logger = logging.getLogger(__name__)

HealthCheck = Callable[[], None]
"""A dependency check: returns if the dependency is usable and raises an exception describing why not otherwise."""


class ServerErrorRoute(APIRoute):
    """A route that reports :class:`~bluesky_adaptive.server.errors.ServerError` with its status code."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the handler to translate server errors into JSON error responses."""
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except ServerError as err:
                return JSONResponse({"detail": err.detail}, status_code=err.status_code)

        return handle


def _router(tags: list[str]) -> APIRouter:
    return APIRouter(tags=list(tags), route_class=ServerErrorRoute)


def create_agents_router(manager: OptimizationManager) -> APIRouter:
    """
    Routes for problem templates and agents: listing templates, agent CRUD, validation, and starting optimizations.

    Parameters
    ----------
    manager : OptimizationManager
        The manager the routes operate on.
    """
    router = _router(["agents"])

    @router.get("/problems")
    def list_problems() -> list[CatalogEntryDescription]:
        """List the registered problem templates with the JSON schema of their parameters."""
        return manager.problems.describe()

    @router.get("/agents")
    def list_agents() -> list[AgentRecord]:
        """List agents."""
        return manager.list_agents()

    @router.post("/agents", status_code=status.HTTP_201_CREATED)
    def create_agent(config: AgentConfig) -> AgentRecord:
        """Validate and save an agent."""
        return manager.create_agent(config)

    @router.post("/agents/validate")
    def validate_agent(spec: AgentSpec) -> AgentSpec:
        """Validate an agent's problem and parameters without saving; returns the canonical form."""
        return manager.validate_agent(spec)

    @router.get("/agents/{name}")
    def get_agent(name: str) -> AgentRecord:
        """Return an agent."""
        return manager.get_agent(name)

    @router.put("/agents/{name}")
    def update_agent(name: str, spec: AgentSpec) -> AgentRecord:
        """Validate and replace an agent's configuration."""
        return manager.update_agent(name, spec)

    @router.delete("/agents/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_agent(name: str) -> None:
        """Delete an agent. Its optimizations are kept."""
        manager.delete_agent(name)

    @router.post("/agents/{name}/optimizations", status_code=status.HTTP_201_CREATED)
    def start_optimization(name: str) -> OptimizationRecord:
        """Start an optimization from the agent with a fresh optimizer."""
        return manager.start_optimization(name)

    return router


def create_optimizations_router(manager: OptimizationManager) -> APIRouter:
    """
    Routes for optimizations: list, detail, status, trials, run references, and stop.

    Parameters
    ----------
    manager : OptimizationManager
        The manager the routes operate on.
    """
    router = _router(["optimizations"])

    @router.get("/optimizations")
    def list_optimizations(agent: str | None = None) -> list[OptimizationRecord]:
        """List optimizations, oldest first."""
        return manager.list_optimizations(agent)

    @router.get("/optimizations/{optimization_id}")
    def get_optimization(optimization_id: str) -> OptimizationRecord:
        """Return an optimization."""
        return manager.get_optimization(optimization_id)

    @router.get("/optimizations/{optimization_id}/status")
    def get_status(optimization_id: str) -> OptimizationStatusView:
        """Return a compact live view of an optimization."""
        return manager.get_status(optimization_id)

    @router.get("/optimizations/{optimization_id}/trials")
    def list_trials(optimization_id: str) -> list[TrialRecord]:
        """List an optimization's trials."""
        return manager.list_trials(optimization_id)

    @router.get("/optimizations/{optimization_id}/runs")
    def list_runs(optimization_id: str) -> list[RunReference]:
        """List the Bluesky runs an optimization acquired."""
        return manager.list_runs(optimization_id)

    @router.post("/optimizations/{optimization_id}/stop")
    def stop(optimization_id: str) -> OptimizationRecord:
        """End an optimization after any in-flight acquisition is ingested."""
        return manager.stop(optimization_id)

    return router


def create_optimizer_router(manager: OptimizationManager) -> APIRouter:
    """
    Routes that drive an open optimization's optimizer and evaluation function directly.

    For clients that acquire data themselves: suggest points, evaluate the acquired data with
    the problem's evaluation function, ingest outcomes, and query the best points.

    Parameters
    ----------
    manager : OptimizationManager
        The manager the routes operate on.
    """
    router = _router(["optimizer"])

    @router.post("/optimizations/{optimization_id}/suggest")
    def suggest(optimization_id: str, request: SuggestRequest) -> list[TrialRecord]:
        """Suggest points without acquiring them."""
        return manager.suggest(optimization_id, request.num_points)

    @router.post("/optimizations/{optimization_id}/evaluate")
    def evaluate(optimization_id: str, request: EvaluateRequest) -> list[TrialRecord]:
        """Evaluate suggested trials from an acquisition UID and ingest the outcomes."""
        return manager.evaluate(optimization_id, request.uid, request.suggestion_ids)

    @router.post("/optimizations/{optimization_id}/ingest")
    def ingest(optimization_id: str, request: IngestRequest) -> list[TrialRecord]:
        """Ingest outcomes of suggested trials or external data."""
        return manager.ingest(optimization_id, request.points)

    @router.get("/optimizations/{optimization_id}/best-points")
    def best_points(optimization_id: str) -> list[BestPoint]:
        """Return the optimizer's best points."""
        return manager.best_points(optimization_id)

    return router


def create_queueserver_router(manager: OptimizationManager) -> APIRouter:
    """
    Routes that run an open optimization through the manager's queueserver.

    Parameters
    ----------
    manager : OptimizationManager
        The manager the routes operate on. Without a queueserver, the routes respond 503.
    """
    router = _router(["queueserver"])

    @router.get("/queueserver/status")
    def queueserver_status() -> dict[str, JsonValue]:
        """Return the queueserver's status."""
        if manager.queueserver is None:
            raise UnavailableError("This server has no queueserver")
        return dict(manager.queueserver.status() or {})

    @router.post("/optimizations/{optimization_id}/run", status_code=status.HTTP_202_ACCEPTED)
    def run(optimization_id: str, request: RunRequest) -> ExecutionRecord:
        """Start the suggest-acquire-evaluate-ingest loop; progress is reported through events."""
        return manager.run(optimization_id, request.iterations, request.num_points, request.checkpoint_interval)

    @router.post("/optimizations/{optimization_id}/acquire", status_code=status.HTTP_202_ACCEPTED)
    def acquire(optimization_id: str, request: AcquireRequest) -> ExecutionRecord:
        """Acquire suggested trials or new points once; progress is reported through events."""
        return manager.acquire(optimization_id, suggestion_ids=request.suggestion_ids, parameters=request.parameters)

    return router


def create_analysis_router(manager: OptimizationManager) -> APIRouter:
    """
    Routes for the analysis catalog and for computing analyses of an open optimization.

    Parameters
    ----------
    manager : OptimizationManager
        The manager the routes operate on.
    """
    router = _router(["analyses"])

    @router.get("/analyses")
    def list_analyses() -> list[CatalogEntryDescription]:
        """List every registered analysis."""
        return manager.analyses.describe()

    @router.get("/optimizations/{optimization_id}/analyses")
    def list_optimization_analyses(optimization_id: str) -> list[CatalogEntryDescription]:
        """List the analyses that apply to the optimization's optimizer."""
        return manager.list_analyses(optimization_id)

    @router.post("/optimizations/{optimization_id}/analyses/{name}")
    def compute_analysis(optimization_id: str, name: str, request: AnalysisRequest) -> JsonValue:
        """Compute an analysis of the optimization's optimizer."""
        return manager.compute_analysis(optimization_id, name, request.params)

    return router


def create_events_router(manager: OptimizationManager) -> APIRouter:
    """
    Stream lifecycle events as Server-Sent Events.

    Events are named after the resource that changed (``optimization``, ``execution``, ``trial``)
    and carry its JSON representation. A new connection first receives a ``snapshot`` event with
    the open optimizations, unless it resumes with ``Last-Event-ID`` from events still in the
    server's history.

    Parameters
    ----------
    manager : OptimizationManager
        The manager whose events are streamed.
    """
    router = _router(["events"])

    def known_optimization(optimization_id: str | None = None) -> str | None:
        # Resolved as a dependency so an unknown ID is reported before the stream starts.
        if optimization_id is not None:
            manager.get_optimization(optimization_id)
        return optimization_id

    def to_sse(event: Event) -> ServerSentEvent:
        return ServerSentEvent(event=event.type, id=event.id, data=event.data)

    @router.get("/events", response_class=EventSourceResponse)
    async def events(
        optimization_id: Annotated[str | None, Depends(known_optimization)],
        snapshot: bool = True,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> AsyncIterable[ServerSentEvent]:
        """Stream lifecycle events, optionally only those of one optimization."""
        subscription = manager.events.subscribe(last_event_id)
        try:
            if snapshot and not subscription.resumed:
                yield ServerSentEvent(event="snapshot", id=subscription.resume_id, data=manager.snapshot(optimization_id))
            for event in subscription.backlog:
                if optimization_id is None or event.optimization_id == optimization_id:
                    yield to_sse(event)
            async for event in subscription:
                if optimization_id is None or event.optimization_id == optimization_id:
                    yield to_sse(event)
        finally:
            subscription.close()

    return router


def create_system_router(
    manager: OptimizationManager,
    *,
    health_checks: Mapping[str, HealthCheck] | None = None,
    capabilities: Mapping[str, JsonValue] | None = None,
) -> APIRouter:
    """
    Health and capability routes.

    Parameters
    ----------
    manager : OptimizationManager
        The manager whose features are reported.
    health_checks : Mapping[str, HealthCheck] | None, optional
        Application dependency checks by name, for example ``{"queueserver": client.check_environment}``.
    capabilities : Mapping[str, JsonValue] | None, optional
        Application capabilities reported next to the manager's, for example beamline-specific features.
    """
    router = _router(["system"])
    checks = dict(health_checks or {})
    extra_capabilities = dict(capabilities or {})

    @router.get("/health", responses={503: {"model": HealthReport}})
    async def health() -> Response:
        """Run the dependency checks. Responds 503 if any fails."""
        results = {}
        for name, check in checks.items():
            try:
                await run_in_threadpool(check)
            except Exception as err:
                logger.warning(f"Health check {name!r} failed: {err!r}")
                results[name] = HealthCheckResult(ok=False, detail=f"{type(err).__name__}: {err}")
            else:
                results[name] = HealthCheckResult(ok=True)
        report = HealthReport(ok=all(result.ok for result in results.values()), checks=results)
        return JSONResponse(report.model_dump(mode="json"), status_code=200 if report.ok else 503)

    @router.get("/capabilities")
    def get_capabilities() -> dict[str, JsonValue]:
        """Report the problem templates, analyses, execution backends, and application capabilities."""
        return {
            "problems": [template.name for template in manager.problems],
            "analyses": [method.name for method in manager.analyses],
            "queueserver": manager.queueserver is not None,
            **extra_capabilities,
        }

    return router
