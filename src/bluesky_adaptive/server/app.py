"""
The adaptive server's HTTP API.

.. warning::

    This module is **experimental**. The API is not yet stable and may change
    in future releases without a deprecation period.

An application module supplies the catalog entries and serves the app with Uvicorn::

    # my_server.py
    from bluesky_adaptive.server import ServerSettings, create_app

    app = create_app(ServerSettings.from_file("settings.yml"), optimizers=[...], evaluations=[...])

.. code-block:: bash

    uvicorn my_server:app --port 8100

Interactive OpenAPI documentation is served at ``/docs``.
"""

import logging
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Any

try:
    from bluesky_queueserver_api.comm_base import RequestFailedError, RequestTimeoutError
    from fastapi import APIRouter, FastAPI, Request, Response, status
    from fastapi.responses import JSONResponse
except ImportError as e:
    raise ImportError(
        "The adaptive server requires additional dependencies. Install them with: pip install bluesky-adaptive[server]"
    ) from e
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import __version__
from .agents import AgentRegistry
from .catalog import Catalog, ContextEntry, EvaluationEntry, IngestionEntry, OptimizerEntry
from .connections import ConnectFunction, Connections, QueueserverConnection, acquisition_contract_problems, flatten_devices
from .errors import NotFoundError, ServerError
from .ingestion import IngestionManager
from .problem import ProblemSpec
from .settings import ServerSettings

logger = logging.getLogger(__name__)

_STATUS_KEYS = (
    "manager_state",
    "worker_environment_exists",
    "re_state",
    "items_in_queue",
    "running_item_uid",
    "queue_autostart_enabled",
)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunRequest(_Request):
    """Start an optimization loop."""

    iterations: int = Field(ge=1)
    n_points: int = Field(default=1, ge=1)
    checkpoint_interval: int | None = Field(default=None, ge=1)
    context: str | None = Field(default=None, description="UID of the run that sets the first iteration's context")


class SubmitSuggestionsRequest(_Request):
    """Acquire these points in one iteration."""

    suggestions: list[dict[str, Any]] = Field(min_length=1)


class SuggestRequest(_Request):
    """Suggest points without acquiring them."""

    n_points: int = Field(default=1, ge=1)
    context: str | None = Field(default=None, description="UID of the run whose context to suggest in")


class IngestRequest(_Request):
    """Points measured elsewhere, or outcomes of suggestions by ``"_id"``."""

    points: list[dict[str, Any]] = Field(min_length=1)


class JobsRequest(_Request):
    """Runs to ingest by UID."""

    run_uids: list[str] = Field(min_length=1)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _error_handler(status_code: int | None = None) -> Callable[[Request, Exception], Any]:
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        code = status_code if status_code is not None else getattr(exc, "status_code", 500)
        return JSONResponse(status_code=code, content={"detail": str(exc) if code < 500 else _describe(exc)})

    return handler


def _declared(entry_function: Callable[[Any], Iterable[str]] | None, model: type[BaseModel]) -> list[str] | None:
    """Names an entry declares for its default params, or ``None`` when they depend on the params."""
    if entry_function is None:
        return None
    try:
        return list(entry_function(model()))
    except (ValidationError, ValueError, TypeError):
        return None


def _catalog_entries(catalog: Catalog, kind: str) -> list[dict[str, Any]]:
    described: list[dict[str, Any]] = []
    if kind == "optimizers":
        for entry in catalog.optimizers.values():
            described.append(_entry_summary(entry, entry.options))
    elif kind == "evaluations":
        for entry in catalog.evaluations.values():
            described.append({**_entry_summary(entry, entry.params), "outputs": _declared(entry.outputs, entry.params)})
    elif kind == "contexts":
        for entry in catalog.contexts.values():
            described.append({**_entry_summary(entry, entry.params), "features": _declared(entry.features, entry.params)})
    elif kind == "ingestions":
        for entry in catalog.ingestions.values():
            described.append({**_entry_summary(entry, entry.params), "outputs": _declared(entry.outputs, entry.params)})
    else:
        raise NotFoundError(f"unknown catalog kind {kind!r}; kinds: optimizers, evaluations, contexts, ingestions")
    return described


def _entry_summary(
    entry: OptimizerEntry[Any] | EvaluationEntry[Any] | ContextEntry[Any] | IngestionEntry[Any], model: type[BaseModel]
) -> dict[str, Any]:
    return {
        "name": entry.name,
        "version": entry.version,
        "description": entry.description,
        "schema": model.model_json_schema(),
    }


def _queueserver_summary(connection: QueueserverConnection) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "name": connection.name,
        "zmq_control_addr": connection.settings.zmq_control_addr,
        "user_group": connection.settings.user_group,
        "run_feed": {
            "type": connection.settings.run_feed.type,
            "state": connection.router.feed.state,
            "runs_routed": connection.router.runs_routed,
        },
        "status": None,
        "error": None,
    }
    try:
        manager_status: Mapping[str, Any] = connection.api.status()
    except (RequestFailedError, RequestTimeoutError) as err:
        summary["error"] = _describe(err)
    else:
        summary["status"] = {key: manager_status.get(key) for key in _STATUS_KEYS}
    return summary


def create_app(
    settings: ServerSettings,
    *,
    optimizers: Iterable[OptimizerEntry[Any]],
    evaluations: Iterable[EvaluationEntry[Any]],
    contexts: Iterable[ContextEntry[Any]] = (),
    ingestions: Iterable[IngestionEntry[Any]] = (),
    connect: ConnectFunction | None = None,
) -> FastAPI:
    """
    Create the adaptive server for an application's catalog.

    Queueserver connections open when the application starts and close, with every agent, when it
    shuts down. Plans already queued are left to finish.

    Parameters
    ----------
    settings : ServerSettings
        Deployment settings.
    optimizers, evaluations, contexts, ingestions : Iterable[...]
        The catalog entries clients may choose.
    connect : ConnectFunction | None, optional
        Creates the RE Manager client and run feed of each queueserver; tests pass fakes.

    Returns
    -------
    FastAPI
        The application, with the API under ``/api/v1``.
    """
    catalog = Catalog(optimizers=optimizers, evaluations=evaluations, contexts=contexts, ingestions=ingestions)
    connections = Connections(settings, connect)
    registry = AgentRegistry(catalog, connections, settings.storage_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        connections.open()
        try:
            yield
        finally:
            registry.close()
            connections.close()

    app = FastAPI(title="Bluesky Adaptive server", version=__version__, lifespan=lifespan)
    app.state.registry = registry
    app.state.connections = connections
    app.add_exception_handler(ServerError, _error_handler())
    app.add_exception_handler(RequestFailedError, _error_handler(status.HTTP_502_BAD_GATEWAY))
    app.add_exception_handler(RequestTimeoutError, _error_handler(status.HTTP_503_SERVICE_UNAVAILABLE))
    api = APIRouter(prefix="/api/v1")

    @api.get("/catalog/{kind}")
    def get_catalog(kind: str) -> list[dict[str, Any]]:
        """Entries of one kind: ``optimizers``, ``evaluations``, ``contexts`` or ``ingestions``."""
        return _catalog_entries(catalog, kind)

    @api.get("/queueservers")
    def list_queueservers() -> list[dict[str, Any]]:
        """List the configured queueservers with their manager status and run feed state."""
        return [_queueserver_summary(connection) for connection in connections]

    @api.get("/queueservers/{name}/plans")
    def list_plans(name: str) -> list[dict[str, Any]]:
        """Plans the server's user group may run, flagging those that fit the acquisition contract."""
        plans = connections[name].api.plans_allowed().get("plans_allowed") or {}
        described = []
        for plan_name, plan in sorted(plans.items()):
            problems = acquisition_contract_problems(plan)
            described.append(
                {
                    "name": plan_name,
                    "description": plan.get("description"),
                    "parameters": [parameter["name"] for parameter in plan.get("parameters") or []],
                    "fits_acquisition_contract": not problems,
                    "problems": problems,
                }
            )
        return described

    @api.get("/queueservers/{name}/devices")
    def list_devices(name: str) -> dict[str, dict[str, bool]]:
        """Devices the server's user group may use, by dotted path, with their capabilities."""
        return flatten_devices(connections[name].api.devices_allowed().get("devices_allowed") or {})

    @api.post("/agents", status_code=status.HTTP_201_CREATED)
    def create_agent(spec: ProblemSpec) -> dict[str, Any]:
        """Create an agent from a spec composed of catalog entries."""
        return registry.create(spec).info()

    @api.get("/agents")
    def list_agents() -> list[dict[str, Any]]:
        """Every agent, oldest first."""
        return [agent.info() for agent in registry]

    @api.get("/agents/{agent_id}")
    def get_agent(agent_id: str) -> dict[str, Any]:
        """Return the agent's spec, entries, capabilities, status and ingestion summary."""
        return registry[agent_id].info()

    @api.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_agent(agent_id: str) -> Response:
        """Delete an agent that is not running."""
        registry.delete(agent_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post("/agents/{agent_id}/run", status_code=status.HTTP_202_ACCEPTED)
    def run(agent_id: str, request: RunRequest) -> dict[str, Any]:
        """Start the suggest -> acquire -> ingest loop; poll the agent for progress."""
        agent = registry[agent_id]
        agent.run(request.iterations, request.n_points, request.checkpoint_interval, request.context)
        return agent.status()

    @api.post("/agents/{agent_id}/submit-suggestions", status_code=status.HTTP_202_ACCEPTED)
    def submit_suggestions(agent_id: str, request: SubmitSuggestionsRequest) -> dict[str, Any]:
        """Acquire the given points in one iteration and ingest their outcomes."""
        agent = registry[agent_id]
        agent.submit_suggestions(request.suggestions)
        return agent.status()

    @api.post("/agents/{agent_id}/stop")
    def stop(agent_id: str) -> dict[str, Any]:
        """Submit nothing more: ``stopped`` at once if the plan was still queued, otherwise ``stopping``."""
        return registry[agent_id].stop()

    @api.post("/agents/{agent_id}/suggest")
    def suggest(agent_id: str, request: SuggestRequest) -> dict[str, Any]:
        """Suggest points without acquiring them."""
        return registry[agent_id].suggest(request.n_points, request.context)

    @api.post("/agents/{agent_id}/ingest", status_code=status.HTTP_204_NO_CONTENT)
    def ingest(agent_id: str, request: IngestRequest) -> Response:
        """Ingest points measured elsewhere, or outcomes of suggestions by ``"_id"``."""
        registry[agent_id].ingest(request.points)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get("/agents/{agent_id}/best-points")
    def best_points(agent_id: str) -> list[dict[str, Any]]:
        """Return the optimizer's best points."""
        return registry[agent_id].best_points()

    @api.get("/agents/{agent_id}/history")
    def history(agent_id: str) -> dict[str, Any]:
        """Every operation and iteration."""
        return registry[agent_id].history()

    def _ingestion(agent_id: str) -> IngestionManager:
        agent = registry[agent_id]
        if agent.ingestion is None:
            raise NotFoundError(f"agent {agent_id} has no ingestion entry")
        return agent.ingestion

    @api.get("/agents/{agent_id}/ingestion")
    def get_ingestion(agent_id: str) -> dict[str, Any]:
        """Subscription state, backlog, counts by status and the most recent runs."""
        return _ingestion(agent_id).status()

    @api.post("/agents/{agent_id}/ingestion/subscribe")
    def subscribe(agent_id: str) -> dict[str, Any]:
        """Learn from every completed run the agent did not submit and its ingestor accepts."""
        ingestion = _ingestion(agent_id)
        ingestion.subscribe()
        return ingestion.summary()

    @api.post("/agents/{agent_id}/ingestion/unsubscribe")
    def unsubscribe(agent_id: str) -> dict[str, Any]:
        """Stop learning from new runs."""
        ingestion = _ingestion(agent_id)
        ingestion.unsubscribe()
        return ingestion.summary()

    @api.post("/agents/{agent_id}/ingestion/jobs", status_code=status.HTTP_202_ACCEPTED)
    def jobs(agent_id: str, request: JobsRequest) -> dict[str, Any]:
        """Ingest runs by UID, whether or not the ingestor would accept them."""
        return {"queued": _ingestion(agent_id).jobs(request.run_uids)}

    app.include_router(api)
    return app
