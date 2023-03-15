import enum
import json
import logging
import queue
import threading
import time as ttime
import traceback
import uuid
from collections.abc import Mapping
from multiprocessing import Process

from .comms import PipeJsonRpcReceive
from .logging_setup import setup_loggers
from .utils import WR, get_path_to_simulated_agent, load_worker_startup_code

logger = logging.getLogger(__name__)


# State of the worker environment
class EState(enum.Enum):
    INITIALIZING = "initializing"
    IDLE = "idle"
    EXECUTING_TASK = "executing_task"
    CLOSING = "closing"
    CLOSED = "closed"  # For completeness


class RejectedError(RuntimeError):
    ...


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

        self._exit_event = None
        self._execution_queue = None

        self._running_task_uid = None

        self._background_tasks_num = 0
        self._completed_tasks = []
        self._completed_tasks_lock = None  # threading.Lock()

        self._env_state = EState.INITIALIZING

        self._conn = conn
        self._config_dict = config or {}

        self._ns = {}  # Namespace
        self._variables = {}  # Descriptions of variables

    # ------------------------------------------------------------

    def _generate_task_func(self, parameters):
        """
        Generate function for execution of a task (target function). The function is
        performing all necessary steps to complete execution of the task and report
        the results and could be executed in main or background thread.
        """
        name = parameters["name"]
        task_uid = parameters["task_uid"]
        time_submit = parameters["time_submit"]
        time_start = ttime.time()
        target = parameters["target"]
        target_args = parameters["target_args"]
        target_kwargs = parameters["target_kwargs"]
        run_in_background = parameters["run_in_background"]

        def task_func():
            # This is the function executed in a separate thread
            try:
                if run_in_background:
                    self._background_tasks_num += 1
                else:
                    self._env_state = EState.EXECUTING_TASK
                    self._running_task_uid = task_uid

                return_value = target(*target_args, **target_kwargs)

                # Attempt to serialize the result to JSON. The result can not be sent to the client
                #   if it can not be serialized, so it is better for the function to fail here so that
                #   proper error message could be sent to the client.
                try:
                    json.dumps(return_value)  # The result of the conversion is intentionally discarded
                except Exception as ex_json:
                    raise ValueError(f"Task result can not be serialized as JSON: {ex_json}") from ex_json

                success, err_msg, err_tb = True, "", ""
            except Exception as ex:
                s = f"Error occurred while executing {name!r}"
                err_msg = f"{s}: {str(ex)}"
                if hasattr(ex, "tb"):  # ScriptLoadingError
                    err_tb = str(ex.tb)
                else:
                    err_tb = traceback.format_exc()
                logger.error("%s:\n%s\n", err_msg, err_tb)

                return_value, success = None, False
            finally:
                if run_in_background:
                    self._background_tasks_num = max(self._background_tasks_num - 1, 0)
                else:
                    self._env_state = EState.IDLE
                    self._running_task_uid = None

            with self._completed_tasks_lock:
                task_res = {
                    "task_uid": task_uid,
                    "success": success,
                    "msg": err_msg,
                    "traceback": err_tb,
                    "return_value": return_value,
                    "time_submit": time_submit,
                    "time_start": time_start,
                    "time_stop": ttime.time(),
                }
                self._completed_tasks.append(task_res)

        return task_func

    def start_task(
        self,
        *,
        name,
        target,
        target_args=None,
        target_kwargs=None,
        run_in_background=True,
        task_uid=None,
    ):
        """
        Run ``target`` (any callable) in the main thread or a separate daemon thread. The callable
        is passed arguments ``target_args`` and ``target_kwargs``. The function returns once
        the generated function is passed to the main thread, started in a background thead or fails
        to start. The function is not waiting for the execution result.

        Parameters
        ----------
        name: str
            The name used in background thread name and error messages.
        target: callable
            Callable (function or method) to execute.
        target_args: list
            List of target args (passed to ``target``).
        target_kwargs: dict
            Dictionary of target kwargs (passed to ``target``).
        run_in_background: boolean
            Run in a background thread if ``True``, run as the main thread otherwise.
        task_uid: str or None
            UID of the task. If ``None``, then the new UID is generated.
        """
        target_args, target_kwargs = target_args or [], target_kwargs or {}
        status, msg = "accepted", ""

        task_uid = task_uid or str(uuid.uuid4())
        time_submit = ttime.time()
        logger.debug("Starting task '%s'. Task UID: '%s'.", name, task_uid)

        try:
            # Verify that the environment is ready
            acceptable_states = (EState.IDLE, EState.EXECUTING_TASK)
            if self._env_state not in acceptable_states:
                raise RejectedError(
                    f"Incorrect environment state: '{self._env_state.value}'. "
                    f"Acceptable states: {[_.value for _ in acceptable_states]}"
                )

            task_uid_short = task_uid.split("-")[-1]
            thread_name = f"BS Agent - {name} {task_uid_short} "

            parameters = {
                "name": name,
                "task_uid": task_uid,
                "time_submit": time_submit,
                "target": target,
                "target_args": target_args,
                "target_kwargs": target_kwargs,
                "run_in_background": run_in_background,
            }
            # Generate the target function even if it is not used here to validate parameters
            #   (if it is executed in the main thread), because this is the right place to fail.
            target_func = self._generate_task_func(parameters)

            if run_in_background:
                th = threading.Thread(target=target_func, name=thread_name, daemon=True)
                th.start()
            else:
                self._execution_queue.put((parameters))

        except RejectedError as ex:
            status, msg = "rejected", f"Task {name!r} was rejected by RE Worker process: {ex}"
        except Exception as ex:
            status, msg = "error", f"Error occurred while to starting the task {name!r}: {ex}"

        logger.debug(
            "Completing the request to start the task '%s' ('%s'): status='%s' msg='%s'.",
            name,
            task_uid,
            status,
            msg,
        )

        # Payload contains information that may be useful for tracking the execution of the task.
        payload = {"task_uid": task_uid, "time_submit": time_submit, "run_in_background": run_in_background}

        return status, msg, task_uid, payload

    def _execute_task(self, parameters):
        """
        Execute a plan or a task pulled from ``self._execution_queue``. Note, that the queue
        is used exclusively to pass data between threads and may contain at most 1 element.
        This is not a plan queue. The function is expected to run in the main thread.
        """
        logger.debug("Starting execution of a task in main thread ...")
        try:
            func = self._generate_task_func(parameters)

            # The function 'func' is self-sufficient: it is responsible for catching and processing
            #   exceptions and handling execution results.
            func()

        except Exception as ex:
            # The exception was raised while preparing the function for execution.
            logger.exception("Failed to execute task in main thread: %s", ex)
        else:
            logger.debug("Task execution is completed.")
        finally:
            self._env_state = EState.IDLE

    def _execute_in_main_thread(self):
        """
        Run this function to block the main thread. The function is polling
        `self._execution_queue` and executes the plans that are in the queue.
        If the queue is empty, then the thread remains idle.
        """
        # This function blocks the main thread
        while True:
            # Polling 10 times per second. This is fast enough for slowly executed plans.
            # ttime.sleep(0.1)
            # Exit the thread if the Event is set (necessary to gracefully close the process)
            if self._exit_event.is_set():
                break
            try:
                parameters = self._execution_queue.get(timeout=0.1)
                self._env_state = EState.EXECUTING_TASK
                self._execute_task(parameters)
            except queue.Empty:
                pass

    # ------------------------------------------------------------

    def _status_handler(self):
        background_tasks_num = self._background_tasks_num
        foreground_tasks_num = self._execution_queue.qsize()
        task_uid = self._running_task_uid

        status = {
            "state": self._env_state.value,
            "task_uid": task_uid,
            "foreground_tasks_num": foreground_tasks_num,
            "background_tasks_num": background_tasks_num,
        }
        return status

    def _variables_handler(self):
        vars = {
            k: {"pv_type": v["pv_type"], "pv_max_length": v["pv_max_length"]} for k, v in self._variables.items()
        }
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
                    obj[attr_or_key] = value
                elif hasattr(obj, attr_or_key):
                    setattr(obj, attr_or_key, value)
                else:
                    raise Exception(f"Variable {name!r} is not accessible")

        except Exception as ex:
            success, msg = False, str(ex)

        return {"success": success, "msg": msg, "name": name, "value": value}

    def _stop_worker_handler(self):
        print("STOPPING THE WORKER .........................")
        self._exit_event.set()

    def run(self):
        """
        Overrides the `run()` function of the `multiprocessing.Process` class. Called
        by the `start` method.
        """
        # logging.basicConfig(level=max(logging.WARNING, self._log_level))
        setup_loggers(log_level=self._log_level)

        import sys

        sys.__stdin__ = sys.stdin

        success = True
        WR.set_worker_obj(self)
        WR.set_agent_server_vars(self._variables)

        self._exit_event = threading.Event()
        self._execution_queue = queue.Queue()
        self._completed_tasks_lock = threading.Lock()

        # Class that supports communication over the pipe
        self._comm_to_manager = PipeJsonRpcReceive(conn=self._conn, name="RE Worker-Manager Comm")

        self._comm_to_manager.add_method(self._status_handler, "status")
        self._comm_to_manager.add_method(self._variables_handler, "variables")
        self._comm_to_manager.add_method(self._variable_get_handler, "variable_get")
        self._comm_to_manager.add_method(self._variable_set_handler, "variable_set")
        self._comm_to_manager.add_method(self._stop_worker_handler, "stop_worker")

        self._comm_to_manager.start()

        try:
            startup_script_path = self._config_dict.get("startup_script_path")
            startup_module_name = self._config_dict.get("startup_module_name")
            if not startup_script_path and not startup_module_name:
                startup_script_path = get_path_to_simulated_agent()
            self._ns = load_worker_startup_code(
                startup_script_path=startup_script_path,
                startup_module_name=startup_module_name,
            )

            # Executing startup tasks
            n_startup_tasks = len(WR.startup_tasks)
            if n_startup_tasks:
                print(f"Executing startup tasks ({n_startup_tasks}):")
                for n, func in enumerate(WR.startup_tasks):
                    print(f"Task {n} ...")
                    func()
                print("Startup tasks are completed.")

            self._env_state = EState.IDLE

        except Exception as ex:
            s = "Failed to load the agent code."
            if hasattr(ex, "tb"):  # ScriptLoadingError
                logger.error("%s:\n%s\n", s, ex.tb)
            else:
                logger.exception("%s: %s.", s, ex)
            success = False

        if success:
            logger.info("RE Environment is ready")
            self._execute_in_main_thread()

        self._env_state = EState.CLOSING

        # Executing shutdown tasks
        n_shutdown_tasks = len(WR.shutdown_tasks)
        if n_shutdown_tasks:
            print(f"Executing shutdown tasks ({n_shutdown_tasks}):")
            for n, func in enumerate(WR.shutdown_tasks):
                print(f"Task {n} ...")
                func()
            print("Shutdown tasks are completed.")

        self._comm_to_manager.stop()
