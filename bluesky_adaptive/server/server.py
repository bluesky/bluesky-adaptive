from fastapi import FastAPI
from .server_api import router as server_api_router
from .ioc_server import IOC_Server
from .server_resources import SR
from .worker import WorkerProcess
from multiprocessing import Pipe

import logging
logger = logging.getLogger("uvicorn")

worker_process = None
ioc_server = None

def create_conn_pipes():
    server_conn, worker_conn = Pipe()
    return server_conn, worker_conn

def build_app():

    app = FastAPI()
    app.include_router(server_api_router)

    @app.on_event("startup")
    async def startup_event():
        global worker_process
        global ioc_server

        logger.info("Starting the server ...")

        server_conn, worker_conn = create_conn_pipes()

        SR.init_comm_to_worker(conn=server_conn)

        worker_process = WorkerProcess(conn=worker_conn)
        worker_process.start()

        ioc_server = IOC_Server(ioc_prefix="agent_IOC")
        await ioc_server.start()


    @app.on_event("shutdown")
    async def shutdown_event():
        global worker_process
        global ioc_server
        logger.info("Shutting down the server ...")

        ioc_server.stop()

        if worker_process and worker_process.is_alive():
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
