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

    async def worker_get_method_descriptions(self):
        """
        Get descriptions of all methods registered in the worker process.

        Raises
        ------
        CommTimeoutError, RequestFailedError
        """
        result = await self._comm_to_worker.send_msg("methods")
        if not result["success"]:
            raise RequestFailedError(result["msg"])
        return result["methods"]

    async def worker_get_method_description(self, name):
        """
        Get description of a method registered in the worker process.

        Parameters
        ----------
        name: str
            Name of the method to get description for.

        Raises
        ------
        CommTimeoutError, RequestFailedError
        """
        result = await self._comm_to_worker.send_msg("method_get", params={"name": name})
        if not result["success"]:
            raise RequestFailedError(result["msg"])
        return {key: result[key] for key in ("name", "description", "input_schema", "output_type") if key in result}

    async def worker_execute_method(self, name, params=None):
        """
        Execute a method in the worker process.

        Parameters
        ----------
        name: str
            Name of the method to execute.
        params: dict, optional
            Parameters to pass to the method.

        Raises
        ------
        CommTimeoutError, RequestFailedError
        """
        result = await self._comm_to_worker.send_msg("method_execute", params={"name": name, "params": params})
        if not result["success"]:
            raise RequestFailedError(result["msg"])
        return result["result"]

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
