from multiprocessing import Process
import os
import logging
import time as ttime
from .comms import PipeJsonRpcReceive
from .utils import load_worker_startup_code, get_path_to_simulated_agent
from collections.abc import Mapping
import enum

import logging

logger = logging.getLogger("uvicorn")


# State of the worker environment
class EState(enum.Enum):
    INITIALIZING = "initializing"
    IDLE = "idle"
    CLOSING = "closing"
    CLOSED = "closed"  # For completeness


class WorkerProcess(Process):

    """
    The class implementing the Worker process.

    Parameters
    ----------
    conn: multiprocessing.Connection
        One end of bidirectional (input/output) pipe. The other end is used by RE Manager.
    args, kwargs
        `args` and `kwargs` of the `multiprocessing.Process`
    """

    def __init__(
        self,
        *args,
        conn,
        config=None,
        log_level=logging.DEBUG,
        **kwargs,
    ):

        if not conn:
            raise RuntimeError("Invalid value of parameter 'conn': %S.", str(conn))

        super().__init__(*args, **kwargs)

        self._log_level = log_level

        self._is_stopping = False

        self._state = EState.INITIALIZING

        self._conn = conn
        self._config_dict = config or {}

        self._ns = {}  # Namespace
        self._variables = {}  # Descriptions of variables

    def _status_handler(self):
        status = {
            "state": self._state.value,
        }
        return status

    def _variables_handler(self):
        vars = {k: {
            "pv_type": v["pv_type"], "pv_max_length": v["pv_max_length"]
            } for k, v in self._variables.items()}
        return {"success": True, "msg": "", "variables": vars}

    def _variable_get_handler(self, *, name):
        success, msg, value = True, "", None
        try:
            if name not in self._variables:
                raise Exception(f"Variable {name!r} is not registered")

            vdesc = self._variables[name]

            if vdesc["getter"] is not None:
                value = vdesc["getter"]()

            else:
                obj = vdesc["object"]
                attr_or_key = vdesc["attr_or_key"]
                if (obj is None) or (attr_or_key is None):
                    raise Exception(f"Object for variable {name!r} is not properly set")
                if isinstance(obj, Mapping) and (attr_or_key in obj):
                    value = obj[attr_or_key]
                elif hasattr(obj, attr_or_key):
                    value = getattr(obj, attr_or_key)
                else:
                    raise Exception(f"Variable {name!r} is not accessible")

        except Exception as ex:
            success, msg = False, str(ex)

        return {"success": success, "msg": msg, "name": name, "value": value}

    def _variable_set_handler(self, *, name, value):
        success, msg = True, ""
        try:
            if name not in self._variables:
                raise Exception(f"Variable {name!r} is not registered")

            vdesc = self._variables[name]

            if vdesc["setter"] is not None:
                value = vdesc["setter"](value)

            else:
                obj = vdesc["object"]
                attr_or_key = vdesc["attr_or_key"]
                if (obj is None) or (attr_or_key is None):
                    raise Exception(f"Object for variable {name!r} is not properly set")
                if isinstance(obj, Mapping) and (attr_or_key in obj):
                    obj[attr_or_key] =  value
                elif hasattr(obj, attr_or_key):
                    setattr(obj, attr_or_key, value)
                else:
                    raise Exception(f"Variable {name!r} is not accessible")

        except Exception as ex:
            success, msg = False, str(ex)

        return {"success": success, "msg": msg, "name": name, "value": value}

    def _stop_worker_handler(self):
        print("STOPPING THE WORKER .........................")
        self._is_stopping = True

    def run(self):
        """
        Overrides the `run()` function of the `multiprocessing.Process` class. Called
        by the `start` method.
        """
        # logging.basicConfig(level=max(logging.WARNING, self._log_level))
        # setup_loggers(name="bluesky_queueserver", log_level=self._log_level)

        success = True

        # Class that supports communication over the pipe
        self._comm_to_manager = PipeJsonRpcReceive(conn=self._conn, name="RE Worker-Manager Comm")

        self._comm_to_manager.add_method(self._status_handler, "status")
        self._comm_to_manager.add_method(self._variables_handler, "variables")
        self._comm_to_manager.add_method(self._variable_get_handler, "variable_get")
        self._comm_to_manager.add_method(self._variable_set_handler, "variable_set")
        self._comm_to_manager.add_method(self._stop_worker_handler, "stop_worker")

        self._comm_to_manager.start()

        try:
            code_path = get_path_to_simulated_agent()
            self._ns = load_worker_startup_code(startup_script_path=code_path)
            self._variables = self._ns.get("_agent_server_variables__", {})
            self._state = EState.IDLE
        except Exception as ex:
            s = "Failed to load the agent code."
            if hasattr(ex, "tb"):  # ScriptLoadingError
                logger.error("%s:\n%s\n", s, ex.tb)
            else:
                logger.exception("%s: %s.", s, ex)
            success = False

        if success:
            while True:
                ttime.sleep(1)
                if self._is_stopping:
                    break

        self._comm_to_manager.stop()
