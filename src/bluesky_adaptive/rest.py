"""
REST API for serving a :class:`~bluesky_adaptive.ax.queueserver_agent.QueueserverAgent`.

.. warning::

    This module is **experimental**. The API is not yet stable and may change
    in future releases without a deprecation period. It is not recommended for
    production use.

The server hosts one agent. Acquisition always goes through the agent's Bluesky Queueserver
connection; the server never owns a RunEngine or devices. Evaluation functions are Python code, so
the agent is built in Python and handed to :func:`create_app`. Serve it with Uvicorn's factory mode:

.. code-block:: python

    # my_agent.py
    from bluesky_adaptive.rest import create_app


    def app():
        agent = QueueserverAgent(...)
        return create_app(agent)

.. code-block:: bash

    uvicorn --factory my_agent:app

Interactive OpenAPI documentation is served at ``/docs``.
"""

import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from concurrent.futures import Future
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any, Literal

try:
    from ax.exceptions.core import AxError, UserInputError
    from bluesky_queueserver_api.comm_base import RequestFailedError, RequestTimeoutError
    from fastapi import FastAPI, HTTPException, Request, Response, status
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
except ImportError as e:
    raise ImportError(
        "The REST API requires additional dependencies. Install them with: pip install bluesky-adaptive[server]"
    ) from e

from . import __version__
from .ax.queueserver_agent import QueueserverAgent
from .protocols import ID_KEY
from .queueserver import OptimizationResult

AgentState = Literal["idle", "running", "completed", "stopped", "failed"]
"""Lifecycle of the most recent queueserver operation."""
Operation = Literal["run", "submit_suggestions"]
"""Kind of queueserver operation started through the API."""


class RunRequest(BaseModel):
    """
    Request body for ``POST /run``.

    Attributes
    ----------
    iterations : int
        Number of suggest -> acquire -> ingest iterations.
    n_points : int
        Number of points suggested and acquired per iteration.
    checkpoint_interval : int | None
        Iterations between optimizer checkpoints, or ``None`` to disable checkpoints.
    """

    iterations: int = Field(default=1, ge=1)
    n_points: int = Field(default=1, ge=1)
    checkpoint_interval: int | None = Field(default=None, ge=1)


class SuggestRequest(BaseModel):
    """
    Request body for ``POST /suggest``.

    Attributes
    ----------
    n_points : int
        Number of points to suggest.
    """

    n_points: int = Field(default=1, ge=1)


class SuggestionsRequest(BaseModel):
    """
    Request body for ``POST /submit-suggestions``.

    Attributes
    ----------
    suggestions : list[dict[str, Any]]
        Parameterizations to acquire. Points from ``POST /suggest`` keep their ``"_id"``;
        points without one are registered with the optimizer as new trials.
    """

    suggestions: list[dict[str, Any]] = Field(min_length=1)


class IngestRequest(BaseModel):
    """
    Request body for ``POST /ingest``.

    Attributes
    ----------
    points : list[dict[str, Any]]
        Outcomes to ingest. Include ``"_id"`` for suggested points; external data carries
        parameter values and outcomes without ``"_id"``.
    """

    points: list[dict[str, Any]] = Field(min_length=1)


class AgentStatus(BaseModel):
    """
    Status of the agent's most recent queueserver operation.

    Attributes
    ----------
    state : AgentState
        ``"idle"`` before the first operation, ``"running"`` while it is in progress, then
        ``"completed"``, ``"stopped"``, or ``"failed"``.
    operation : Operation | None
        The endpoint that started the operation.
    current_iteration : int
        The iteration currently or most recently acquired.
    result : OptimizationResult | None
        The result of a completed or stopped operation.
    error : str | None
        The exception that failed the operation.
    """

    state: AgentState
    operation: Operation | None = None
    current_iteration: int
    result: OptimizationResult | None = None
    error: str | None = None


class BestPoint(BaseModel):
    """
    An optimal point reported by the optimizer.

    Attributes
    ----------
    id : Any
        The suggestion identifier, serialized as ``"_id"``.
    parameters : dict[str, Any]
        Parameter values of the point.
    outcomes : dict[str, Any]
        Measured outcomes. Ax reports each as ``[mean, sem]``; an unknown SEM is ``null``.
    """

    id: Any = Field(alias=ID_KEY)
    parameters: dict[str, Any]
    outcomes: dict[str, Any]


@dataclass
class _OperationRecord:
    kind: Operation
    future: Future[OptimizationResult]
    stop_requested: bool = False


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


@contextmanager
def _translate(status_code: int, *exc_types: type[Exception]) -> Generator[None, None, None]:
    """Report the given exceptions to the client with ``status_code`` instead of a bare 500."""
    try:
        yield
    except exc_types as exc:
        raise HTTPException(status_code, detail=_describe(exc)) from exc


class _AgentService:
    """
    Serialize API access to one agent and track its latest queueserver operation.

    Ax ``Client`` is not thread-safe. Requests share ``lock``; endpoints that mutate the optimizer
    also require that no operation is running, because the queueserver runner calls the optimizer
    from its document-listener thread.
    """

    def __init__(self, agent: QueueserverAgent) -> None:
        self.agent = agent
        self.lock = threading.Lock()
        # Replaced, never mutated in place except `stop_requested`, so `status` can read it without the lock.
        self._operation: _OperationRecord | None = None

    def status(self) -> AgentStatus:
        operation = self._operation
        current_iteration = self.agent.current_iteration
        if operation is None:
            return AgentStatus(state="idle", current_iteration=current_iteration)
        if not operation.future.done():
            return AgentStatus(state="running", operation=operation.kind, current_iteration=current_iteration)
        exc = operation.future.exception()
        if exc is not None:
            return AgentStatus(
                state="failed", operation=operation.kind, current_iteration=current_iteration, error=_describe(exc)
            )
        return AgentStatus(
            state="stopped" if operation.stop_requested else "completed",
            operation=operation.kind,
            current_iteration=current_iteration,
            result=operation.future.result(),
        )

    def _running(self) -> bool:
        return self._operation is not None and not self._operation.future.done()

    @contextmanager
    def idle(self) -> Generator[QueueserverAgent, None, None]:
        """Hold the lock and require that no queueserver operation is running."""
        with self.lock:
            if self._running():
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail="An optimization is running. Stop it or wait for it to finish.",
                )
            yield self.agent

    def start(self, kind: Operation, launch: Callable[[QueueserverAgent], Future[OptimizationResult]]) -> AgentStatus:
        with self.idle() as agent:
            # The runner raises RuntimeError when the queueserver environment is not open.
            with _translate(status.HTTP_409_CONFLICT, RuntimeError):
                future = launch(agent)
            self._operation = _OperationRecord(kind=kind, future=future)
        return self.status()

    def stop(self) -> AgentStatus:
        with self.lock:
            operation = self._operation
            if operation is not None and not operation.future.done():
                operation.stop_requested = True
            self.agent.stop()
        return self.status()


def _error_handler(status_code: int) -> Callable[[Request, Exception], Any]:
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": _describe(exc)})

    return handler


def create_app(agent: QueueserverAgent) -> FastAPI:
    """
    Create a FastAPI application that serves one queueserver agent.

    Shutting the application down stops the agent's optimization loop. Plans already queued on the
    queueserver are left to finish.

    Parameters
    ----------
    agent : QueueserverAgent
        The agent to serve. The application takes ownership of its optimization loop.

    Returns
    -------
    FastAPI
        The application, ready to be served by an ASGI server such as Uvicorn.
    """
    service = _AgentService(agent)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        service.stop()

    app = FastAPI(title="Bluesky Adaptive Agent", version=__version__, lifespan=lifespan)
    app.add_exception_handler(UserInputError, _error_handler(status.HTTP_400_BAD_REQUEST))
    # Remaining Ax errors report optimizer state that does not allow the request, e.g. no completed trials yet.
    app.add_exception_handler(AxError, _error_handler(status.HTTP_409_CONFLICT))
    app.add_exception_handler(RequestFailedError, _error_handler(status.HTTP_502_BAD_GATEWAY))
    app.add_exception_handler(RequestTimeoutError, _error_handler(status.HTTP_504_GATEWAY_TIMEOUT))

    @app.get("/status")
    def get_status() -> AgentStatus:
        """Report the most recent queueserver operation."""
        return service.status()

    @app.post("/run", status_code=status.HTTP_202_ACCEPTED)
    def run(request: RunRequest) -> AgentStatus:
        """Start the suggest -> acquire -> ingest loop on the queueserver; poll ``GET /status`` for progress."""
        return service.start(
            "run",
            lambda agent: agent.run(
                iterations=request.iterations,
                n_points=request.n_points,
                checkpoint_interval=request.checkpoint_interval,
            ),
        )

    @app.post("/submit-suggestions", status_code=status.HTTP_202_ACCEPTED)
    def submit_suggestions(request: SuggestionsRequest) -> AgentStatus:
        """Acquire the given points on the queueserver and ingest their outcomes."""
        with _translate(status.HTTP_422_UNPROCESSABLE_CONTENT, KeyError, ValueError):
            return service.start("submit_suggestions", lambda agent: agent.submit_suggestions(request.suggestions))

    @app.post("/stop")
    def stop() -> AgentStatus:
        """Stop submitting new plans. An acquisition already on the queueserver still runs."""
        return service.stop()

    @app.post("/suggest")
    def suggest(request: SuggestRequest) -> Sequence[Mapping[str, Any]]:
        """Ask the optimizer for points without acquiring them."""
        with service.idle() as idle_agent:
            return idle_agent.suggest(request.n_points)

    @app.post("/ingest", status_code=status.HTTP_204_NO_CONTENT)
    def ingest(request: IngestRequest) -> None:
        """Tell the optimizer outcomes measured outside this agent's acquisition loop."""
        with service.idle() as idle_agent, _translate(status.HTTP_422_UNPROCESSABLE_CONTENT, KeyError, ValueError):
            idle_agent.ingest(request.points)

    @app.get("/best-points")
    def best_points() -> list[BestPoint]:
        """Return the best point, or the Pareto frontier for multiple objectives."""
        with service.lock:
            points = service.agent.get_best_points()
        return [
            BestPoint(_id=trial_id, parameters=dict(parameters), outcomes=dict(outcomes))
            for trial_id, parameters, outcomes in points
        ]

    @app.get("/trials")
    def trials() -> Response:
        """Return Ax's trial summary, one JSON object per trial."""
        with service.lock:
            summary = service.agent.ax_client.summarize()
        # pandas writes NaN as null; the standard JSON encoder would reject it.
        return Response(content=summary.to_json(orient="records"), media_type="application/json")

    @app.post("/checkpoint", status_code=status.HTTP_204_NO_CONTENT)
    def checkpoint() -> None:
        """Save the optimizer state to the agent's ``checkpoint_path``."""
        # AxOptimizer raises ValueError when no checkpoint path is configured.
        with service.idle() as idle_agent, _translate(status.HTTP_409_CONFLICT, ValueError):
            idle_agent.checkpoint()

    return app
