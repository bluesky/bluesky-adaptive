import logging
import os
from multiprocessing import Pipe

from fastapi import FastAPI

from .ioc_server import IOC_Server
from .logging_setup import setup_loggers
from .server_api import router as server_api_router
from .server_resources import SR
from .worker import WorkerProcess

logger = logging.getLogger(__name__)

worker_process = None
ioc_server = None
worker_shutdown_timeout = 5


def create_conn_pipes():
    server_conn, worker_conn = Pipe()
    return server_conn, worker_conn


def build_app():
    app = FastAPI()
    app.include_router(server_api_router)

    @app.on_event("startup")
    async def startup_event():
        global worker_process, ioc_server, worker_shutdown_timeout

        log_level = os.environ.get("BS_AGENT_LOG_LEVEL", "INFO")
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError(
                f"Logging level value {log_level!r} is not supported. "
                "Check value of 'BS_AGENT_LOG_LEVEL' environment variable"
            )
        setup_loggers(log_level=log_level)

        logger.info("Starting the server ...")

        ioc_prefix = os.environ.get("BS_AGENT_IOC_PREFIX", "agent_ioc")
        startup_script_path = os.environ.get("BS_AGENT_STARTUP_SCRIPT_PATH", None)
        startup_module_name = os.environ.get("BS_AGENT_STARTUP_MODULE_NAME", None)
        worker_shutdown_timeout = os.environ.get("BS_AGENT_WORKER_SHUTDOWN_TIMEOUT", 5)

        worker_config = {
            "startup_script_path": startup_script_path,
            "startup_module_name": startup_module_name,
        }

        server_conn, worker_conn = create_conn_pipes()

        SR.init_comm_to_worker(conn=server_conn)

        worker_process = WorkerProcess(conn=worker_conn, config=worker_config, log_level=log_level)
        worker_process.start()

        ioc_server = IOC_Server(ioc_prefix=ioc_prefix)
        await ioc_server.start()

    @app.on_event("shutdown")
    async def shutdown_event():
        global worker_process, ioc_server, worker_shutdown_timeout
        logger.info("Shutting down the server ...")

        ioc_server.stop()

        if worker_process and worker_process.is_alive():
            print("Stopping the worker process ...")
            await SR.worker_initiate_stop()
            worker_process.join(timeout=2)
            if not worker_process.is_alive():
                logger.info("Worker process is closed.")
            else:
                logger.warning("Worker process was not closed properly. Terminating the process ...")
                worker_process.kill()
                logger.info("Worker process is terminated.")

        SR.stop_comm_to_worker()

    return app


# Start with uvicorn (default host: 129.0.0.1 default port: 8000)
# uvicorn bluesky_adaptive.server:app
# Start with gunicorn (single worker)
# gunicorn -k uvicorn.workers.UvicornWorker bluesky_adaptive.server:app

app = build_app()
