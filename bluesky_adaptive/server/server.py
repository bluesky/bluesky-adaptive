import asyncio
from fastapi import FastAPI
from .server_api import router as server_api_router
from .ioc_server import start_ioc_server
from .shared_ns import shared_ns


import logging
logger = logging.getLogger("uvicorn")

ioc_server_task = None

def build_app():

    app = FastAPI()
    app.include_router(server_api_router)

    @app.on_event("startup")
    async def startup_event():
        global ioc_server_task
        logger.info("Starting the server ...")

        pvs_dict = {}
        # It is expected that the following code will be replaced with something meaningful
        for name, _ in shared_ns["server_parameters"].items():
            pvs_dict["parameter:" + name] = {}
        for name, _ in shared_ns["server_variables"].items():
            pvs_dict["variable:" + name] = {}

        ioc_server_task = asyncio.create_task(start_ioc_server(pvs_dict=pvs_dict))

    @app.on_event("shutdown")
    async def shutdown_event():
        global ioc_server_task
        logger.info("Shutting down the server ...")
        ioc_server_task.cancel()

    return app


# Start with uvicorn (default host: 129.0.0.1 default port: 8000)
# uvicorn bluesky_adaptive.server:app
# Start with gunicorn (single worker)
# gunicorn -k uvicorn.workers.UvicornWorker bluesky_adaptive.server:app

app = build_app()
