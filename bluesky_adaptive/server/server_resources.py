from .comms import PipeJsonRpcSendAsync, RequestFailedError


class ServerResources:
    def __init__(self):
        self._comm_to_worker = None

    def init_comm_to_worker(self, *, conn, timeout=1):
        self._comm_to_worker = PipeJsonRpcSendAsync(
            conn=conn,
            name="Server-to-Worker",
            timeout=timeout,
        )
        self._comm_to_worker.start()

    def stop_comm_to_worker(self):
        self._comm_to_worker.stop()

    async def worker_get_status(self):
        """
        Raises
        ------
        CommTimeoutError
        """
        return await self._comm_to_worker.send_msg("status")

    async def worker_get_all_variable_descriptions(self):
        """
        Raises
        ------
        CommTimeoutError, RequestFailedError
        """
        result = await self._comm_to_worker.send_msg("variables")
        if not result["success"]:
            raise RequestFailedError(result["msg"])
        return result["variables"]

    async def worker_get_variable(self, name):
        """
        Raises
        ------
        CommTimeoutError, RequestFailedError
        """
        result = await self._comm_to_worker.send_msg("variable_get", params={"name": name})
        if not result["success"]:
            raise RequestFailedError(result["msg"])
        return {result["name"]: result["value"]}

    async def worker_set_variable(self, name, value):
        """
        Raises
        ------
        CommTimeoutError, RequestFailedError
        """
        result = await self._comm_to_worker.send_msg("variable_set", params={"name": name, "value": value})
        if not result["success"]:
            raise RequestFailedError(result["msg"])
        return {result["name"]: result["value"]}

    async def worker_initiate_stop(self):
        """
        Initiate orderly closing of the worker process. Wait for the process to close using ``join()``
        with appropriate timeout.

        Raises
        ------
        CommTimeoutError
        """
        await self._comm_to_worker.send_msg("stop_worker")


SR = ServerResources()
