"""
Reusable server components: an optimization manager and mountable FastAPI routers.

.. warning::

    This package is an **experimental prototype**. The API may change without a deprecation period.

An application declares problem templates (code that builds optimization problems) and mounts
the routers it needs; clients configure agents from the templates and run optimizations over HTTP,
receiving lifecycle events as Server-Sent Events.
"""

try:
    import fastapi  # noqa: F401
except ImportError as e:
    raise ImportError(
        "The server requires additional dependencies. Install them with: pip install bluesky-adaptive[server]"
    ) from e

from .app import create_app
from .catalog import AnalysisMethod, Catalog, CatalogEntryDescription, NoParams, ProblemTemplate
from .errors import ConflictError, InvalidRequestError, NotFoundError, ServerError, UnavailableError
from .events import Event, EventBroker
from .manager import OptimizationManager
from .models import (
    AgentConfig,
    AgentRecord,
    AgentSpec,
    ExecutionRecord,
    ExecutionStatus,
    OptimizationRecord,
    OptimizationStatus,
    TrialRecord,
    TrialStatus,
)
from .repository import InMemoryOptimizationRepository, OptimizationRepository
from .routers import (
    HealthCheck,
    create_agents_router,
    create_analysis_router,
    create_events_router,
    create_optimizations_router,
    create_optimizer_router,
    create_queueserver_router,
    create_system_router,
)

__all__ = [
    "AgentConfig",
    "AgentRecord",
    "AgentSpec",
    "AnalysisMethod",
    "Catalog",
    "CatalogEntryDescription",
    "ConflictError",
    "Event",
    "EventBroker",
    "ExecutionRecord",
    "ExecutionStatus",
    "HealthCheck",
    "InMemoryOptimizationRepository",
    "InvalidRequestError",
    "NoParams",
    "NotFoundError",
    "OptimizationManager",
    "OptimizationRecord",
    "OptimizationRepository",
    "OptimizationStatus",
    "ProblemTemplate",
    "ServerError",
    "TrialRecord",
    "TrialStatus",
    "UnavailableError",
    "create_agents_router",
    "create_analysis_router",
    "create_app",
    "create_events_router",
    "create_optimizations_router",
    "create_optimizer_router",
    "create_queueserver_router",
    "create_system_router",
]
