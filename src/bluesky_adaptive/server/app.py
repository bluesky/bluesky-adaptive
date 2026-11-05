"""Assemble a FastAPI application from the optimization manager and its routers."""

from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from pydantic import JsonValue

from .manager import OptimizationManager
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


def create_app(
    manager: OptimizationManager,
    *,
    prefix: str = "/api/v1",
    routers: Iterable[APIRouter] = (),
    health_checks: Mapping[str, HealthCheck] | None = None,
    capabilities: Mapping[str, JsonValue] | None = None,
    title: str = "Bluesky Adaptive",
) -> FastAPI:
    """
    Create an application with every built-in router and the manager's lifecycle.

    The queueserver router is included only if the manager has a queueserver. Applications
    that want a subset of the routers compose their own :class:`fastapi.FastAPI` from the
    ``create_*_router`` functions and call :meth:`OptimizationManager.startup` and
    :meth:`OptimizationManager.shutdown` in its lifespan.

    Parameters
    ----------
    manager : OptimizationManager
        The manager the routes operate on.
    prefix : str, optional
        The path prefix of every route. Default is ``"/api/v1"``.
    routers : Iterable[APIRouter], optional
        Application routers, for example beamline-specific ones, mounted under the same prefix.
    health_checks : Mapping[str, HealthCheck] | None, optional
        Application dependency checks reported by ``GET /health``.
    capabilities : Mapping[str, JsonValue] | None, optional
        Application capabilities reported by ``GET /capabilities``.
    title : str, optional
        The OpenAPI title.

    Returns
    -------
    FastAPI
        The application.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        manager.startup()
        try:
            yield
        finally:
            manager.shutdown()

    app = FastAPI(title=title, lifespan=lifespan)
    built_in = [
        create_system_router(manager, health_checks=health_checks, capabilities=capabilities),
        create_agents_router(manager),
        create_optimizations_router(manager),
        create_optimizer_router(manager),
        create_analysis_router(manager),
        create_events_router(manager),
    ]
    if manager.queueserver is not None:
        built_in.append(create_queueserver_router(manager))
    for router in (*built_in, *routers):
        app.include_router(router, prefix=prefix)
    return app
