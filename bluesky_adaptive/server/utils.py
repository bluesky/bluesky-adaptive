import contextvars
import functools
import importlib
import logging
import os
import sys
import traceback
from inspect import signature
from typing import Any, Callable, get_type_hints

from pydantic import BaseModel, create_model

from bluesky_adaptive.server.types import MethodRegistration, RegisteredMethod

logger = logging.getLogger(__name__)


class _WorkerResources:
    def __init__(self):
        self._worker_obj = None
        self._agent_server_vars = None
        self._startup_tasks = []
        self._shutdown_tasks = []
        self._agent_server_methods: dict[str, RegisteredMethod] = {}

    def _check_initialization(self):
        if (self._worker_obj is None) or (self._agent_server_vars is None):
            raise RuntimeError("Worker initialization is incomplete")

    @property
    def worker_obj(self):
        self._check_initialization()
        return self._worker_obj

    @property
    def agent_server_vars(self):
        self._check_initialization()
        return self._agent_server_vars

    @property
    def agent_server_methods(self):
        """
        Returns the reference to the dictionary of registered methods (not the copy).
        The dictionary contains all methods registered with the ``register_method`` function.
        """
        self._check_initialization()
        return self._agent_server_methods

    @property
    def startup_tasks(self):
        """
        Returns the reference to the list of startup tasks (not the copy).
        """
        return self._startup_tasks

    @property
    def shutdown_tasks(self):
        """
        Returns the reference to the list of shutdown tasks (not the copy).
        """
        return self._shutdown_tasks

    def set_worker_obj(self, worker_obj):
        """
        This method should not be called by user code. Creates a reference to the worker object.
        """
        self._worker_obj = worker_obj

    def set_agent_server_vars(self, agent_server_vars):
        """
        This method should not be called by user code. Creates a reference to the dictionary of
        agent server variables. The dictionary contains all variables registered with the
        ``register_variable`` function. Used by the WorkerProcess object.
        """
        self._agent_server_vars = agent_server_vars

    def set_agent_server_methods(self, agent_server_methods):
        """
        This method should not be called by user code. Creates a reference to the dictionary of
        agent server methods. The dictionary contains all methods registered with the
        ``register_method`` function. Used by the WorkerProcess object.
        """
        self._agent_server_methods = agent_server_methods

    def add_startup_task(self, func):
        self._startup_tasks.append(func)

    def add_shutdown_task(self, func):
        self._shutdown_tasks.append(func)


WR = _WorkerResources()


def start_task(target, *args, run_in_background=True, task_uid=None, **kwargs):
    """
    Start a task in a background thread (default) or put the task in the queue to be
    executed in the main thread.

    Parameters
    ----------
    target: Callable
        Reference to the callable function. The function must accept the arguments
        passed as ``args`` and ``kwargs``.
    *args
        Arguments passed to the function.
    run_in_background: boolean (optional)
        Run the function ``target`` in the background thread (True, default),
        otherwise put the function in the queue to be executed in the main thread.
    task_uid: str or None (optional)
        Task UID for the task. If *None* (default), then the task UID is generated
        automatically.
    **kwargs
        Kwargs passed to the function.
    """

    return WR.worker_obj.start_task(
        name="task",
        target=target,
        target_args=args,
        target_kwargs=kwargs,
        run_in_background=run_in_background,
        task_uid=task_uid,
    )


def register_variable(name, obj=None, attr_or_key=None, *, getter=None, setter=None, pv_type=None, pv_max_length=None):
    """
    Register variable to make it accessible by external API. All registered variables are accessible with
    REST API. Variables with defined ``pv_type`` are also accessible as PVs of the IOC. If ``pv_max_length`` is
    not *None* or *1*, the PV type is an array with maximum length determined as ``pv_max_length``.

    Values of the variables must be JSON-serializable. REST API does not require values to have a consistent
    type. For example, any JSON-serializable dictionary would be a good return type. If the variable is
    used to create a PV, there are strict limitations on the returned value type and constency
    (see ``pv_type`` parameter).

    If both ``getter`` and ``setter`` are specified, the values of ``obj`` and ``attr_or_key`` are ignored
    and may be left *None``. If only ``getter`` is specified and ``obj`` and ``attr_or_key`` are *None*,
    then the variable is read-only. If only ``setter`` is specified, then the variable can be set, but not
    read (which is strange and not recommended).

    The ``setter`` and ``getter`` functions are responsible for interacting with Python code. The functions
    may perform small computations on the values, but are expected to exit quickly. The functions start
    tasks in background threads or place tasks in the queue for the main thread if necessary. It is also
    possible to create 'virtual' variables that do not reflect any existing Python variable, but instead
    used to initiate or monitor computations.

    Parameters
    ----------
    name: str
        Name by which the variable is accessible through the REST API. The PV name is generated by converting
        the variable names to upper-case letters. The name does not need to match the actual name of
        the variable used in the code. The name should be selected so that it could be conveniently used
        in the API.
    obj: Object
        Reference to a Python class, or dictionary. Pass ``global()`` to access global variables.
    attr_or_key: str
        The name of a class attribute or a dictionary key. Pass the name of the variable to access a global
        variable.
    getter: Callable
        Reference to a 'getter' function. If ``getter`` is not *None*, then the value of the function
        is used as the variable value. Otherwise, the variable value is determined based on ``obj`` and
        ``attr_or_key``. The 'getter' function does not take any parameters.
    setter: Callable
        Reference to a 'setter' function, which takes the new value as a parameter and returns the set
        value. If ``setter`` is not *None*, then it is responsible for setting the actual Python
        variable(s). The function may change the value if needed (e.g. if the passed value is out of range)
        and write the value to the appropriate Python variable. The returned value is passed to the client
        as part of response to the API call. The returned value must match the value of the Python variable
        or be consistent with the value returned by the 'getter' function.
    pv_type: str, None
        String containing PV type (``'str'``, ``'int'``, ``'float'``, ``'bool'``). PVs are created
        for all variables for which ``pv_type`` is specified (not *None*).
    pv_max_length: int, None
        Maximum length of a string or array size for the PV. If ``pv_max_length`` is not *None* or *1*,
        the PV is considered an array.
    """
    if not isinstance(name, str):
        raise TypeError(f"Variable name must be a string: name={name!r}")
    if not isinstance(attr_or_key, (str, type(None))):
        raise TypeError(f"Name of an attribute or a key must be a string: attr_or_key={attr_or_key!r}")
    if not isinstance(getter, type(None)) and not callable(getter):
        raise TypeError(f"Parameter 'getter' must be callable or None: type(get)={type(getter)!r}")
    if not isinstance(setter, type(None)) and not callable(setter):
        raise TypeError(f"Parameter 'setter' must be callable or None: type(setter)={type(setter)!r}")
    if not isinstance(pv_type, (str, type(None))):
        raise TypeError(f"PV type must be a string: pv_type={pv_type!r}")
    if not isinstance(pv_max_length, (int, type(None))):
        raise TypeError(f"PV max length must be int: pv_max_length={pv_max_length!r}")

    WR.agent_server_vars[name] = {
        "object": obj,
        "attr_or_key": attr_or_key,
        "pv_type": pv_type,
        "pv_max_length": pv_max_length,
        "getter": getter,
        "setter": setter,
    }


def create_input_model(func: Callable) -> type[BaseModel]:
    """Generate a Pydantic model from callable's type annotations"""
    hints = get_type_hints(func)
    sig = signature(func)

    # Create field definitions for the dynamic model
    fields = {}
    for param_name, param in sig.parameters.items():
        if param_name in hints:
            param_type = hints[param_name]
            # If parameter has default, make it optional
            if param.default is param.empty:
                fields[param_name] = (param_type, ...)  # ... means required
            else:
                fields[param_name] = (param_type, param.default)

    # Create and return dynamic model
    return create_model(f"{func.__name__}_input", **fields)


def register_method(
    name: str,
    obj: Any,
    method_name: str,
    *,
    description: str = "",
    input_schema: dict[str, tuple[type, Any]] = None,
    output_type: type = None,
):
    """
    Register a method to make it accessible by external API. All registered methods are accessible with
    REST API.

    Parameters
    ----------
    name: str
        Name by which the method is accessible through the REST API.
    obj: Object
        Reference to a Python class that contains the method.
    method_name: str
        Name of the method in the class.
    description: str
        Description of the method.
    input_schema: dict
        Input schema for the method. The schema is used to validate input parameters.
        If not specified, the input schema is inferred from the method annotations.
        This is cast into a Pydantic model, which is used to validate input parameters.
        The schema is a dictionary where keys are parameter names and values are tuples
        containing the type and default value (if any) of the parameter.
        Example: `{"param1": (int, ...), "param2": (str, "default_value")}`
        If the parameter is required, use `...` as the default value or contain it as single item in tuple.
        If the parameter is optional, specify the default value (e.g., `None`).
    output_type: type
        Output type of the method. The type is used to validate output parameters.
        If not specified, the output type is inferred from the method annotations.
    """
    if not isinstance(name, str):
        raise TypeError(f"Method name must be a string: name={name!r}")
    if not isinstance(method_name, str):
        raise TypeError(f"Method name must be a string: method_name={method_name!r}")
    if not isinstance(description, str):
        raise TypeError(f"Description must be a string: description={description!r}")
    if not isinstance(input_schema, (dict, type(None))):
        raise TypeError(f"Input schema must be a dictionary or None: input_schema={input_schema!r}")

    if not hasattr(obj, method_name):
        raise AttributeError(f"Object {obj!r} does not have a method named '{method_name}'")
    if not callable(getattr(obj, method_name)):
        raise TypeError(f"Object {obj!r} has an attribute '{method_name}' that is not callable")

    # Attempt to infer the description from the method docstring if not provided
    if not description:
        method = getattr(obj, method_name)
        if callable(method) and method.__doc__:
            description = method.__doc__.strip().split("\n")[0]

    # Attempt to infer input and output schemas from the method annotations if not provided
    method = getattr(obj, method_name)
    if input_schema is None:
        input_model = create_input_model(method)
    else:
        input_model = create_model(f"{method_name}_input", **input_schema)
    if output_type is None:
        hints = get_type_hints(method)
        output_type = hints.get("return", None)

    WR.agent_server_methods[name] = RegisteredMethod(
        callable=method,
        registration=MethodRegistration(
            name=name, description=description, input_model=input_model, output_type=output_type
        ),
    )


def startup_decorator(func):
    """
    The decorated functions are executed during startup after loading the script.
    The functions are executed in the order in which they appear in the code.
    The functions should accept no parameters. The returned values are ignored.

    The decorate will not work only with stand-alone function, not with a method of a class.
    """
    WR.add_startup_task(func)
    return func


def shutdown_decorator(func):
    """
    The decorated functions are executed during shutdown.
    The functions are executed in the order in which they appear in the code.
    The functions should accept no parameters. The returned values are ignored.

    The decorate will not work only with stand-alone function, not with a method of a class.
    """
    WR.add_shutdown_task(func)
    return func


def get_path_to_simulated_agent():
    return os.path.join(*os.path.split(__file__)[:-1], "demo", "agent_sim.py")


class ScriptLoadingError(Exception):
    def __init__(self, msg, tb):
        super().__init__(msg)
        self._tb = tb

    @property
    def tb(self):
        """
        Returns the full traceback (string) that is passed as a second parameter.
        The traceback is expected to be fully prepared so that it could be saved
        or passed to consumer over the network. Capturing traceback in the function
        executing the script is reducing references to Queue Server code, which
        is generic and unimportant for debugging startup scripts.
        """
        return self._tb


def load_startup_module(module_name):
    """
    Populate namespace by importing a module.

    Parameters
    ----------
    module_name: str
        name of the module to import

    Returns
    -------
    nspace: dict
        namespace that contains objects loaded from the module.
    """
    importlib.invalidate_caches()

    try:
        _module = importlib.import_module(module_name)
        nspace = _module.__dict__

    except BaseException as ex:
        # Capture traceback and send it as a message
        msg = f"Error while loading module {module_name!r}: {ex}"
        ex_str = traceback.format_exception(*sys.exc_info())
        ex_str = "".join(ex_str) + "\n" + msg
        raise ScriptLoadingError(msg, ex_str) from ex

    return nspace


def load_startup_script(script_path, *, enable_local_imports=True):
    """
    Populate namespace by executing a script.

    Parameters
    ----------
    script_path : str
        full path to the startup script
    enable_local_imports : boolean
        If ``False``, local imports from the script will not work. Setting to ``True``
        enables local imports.

    Returns
    -------
    nspace: dict
        namespace that contains objects loaded from the module.
    """
    importlib.invalidate_caches()

    if not os.path.isfile(script_path):
        raise ImportError(f"Failed to load the script '{script_path}': script was not found")

    nspace = {}

    if enable_local_imports:
        p = os.path.split(script_path)[0]
        sys.path.insert(0, p)  # Needed to make local imports work.
        # Save the list of available modules
        # sm_keys = list(sys.modules.keys())

    try:
        nspace = {}
        if not os.path.isfile(script_path):
            raise OSError(f"Startup file {script_path!r} was not found")

        # Set '__file__' and '__name__' variables
        patch = f"__file__ = '{script_path}'; __name__ = 'startup_script'\n"
        exec(patch, nspace, nspace)

        code = compile(open(script_path).read(), script_path, "exec")
        exec(code, nspace, nspace)

    except BaseException as ex:
        # Capture traceback and send it as a message
        msg = f"Error while executing script {script_path!r}: {ex}"
        ex_str = traceback.format_exception(*sys.exc_info())
        ex_str = "".join(ex_str) + "\n" + msg
        raise ScriptLoadingError(msg, ex_str) from ex

    finally:
        patch = "del __file__\n"  # Do not delete '__name__'
        exec(patch, nspace, nspace)

        if enable_local_imports:
            # Delete data on all modules that were loaded by the script.
            # We don't need them anymore. Modules will be reloaded from disk if
            #   the script is executed again.
            # for key in list(sys.modules.keys()):
            #     if key not in sm_keys:
            #         # print(f"Deleting the key '{key}'")
            #         del sys.modules[key]

            sys.path.remove(p)

    return nspace


def load_worker_startup_code(*, startup_module_name=None, startup_script_path=None):
    """
    Load worker startup code from a startup script or startup module.

    Parameters
    ----------
    startup_module_name : str
        Name of the startup module.
    startup_script_path : str
        Path to startup script.

    Returns
    -------
    nspace : dict
       Dictionary with loaded namespace data
    """

    if sum([_ is not None for _ in [startup_module_name, startup_script_path]]) > 1:
        raise ValueError("Source of the startup code was not specified or multiple sources were specified.")

    if startup_module_name is not None:
        logger.info("Loading startup code from module '%s' ...", startup_module_name)
        nspace = load_startup_module(startup_module_name)

    elif startup_script_path is not None:
        logger.info("Loading startup code from script '%s' ...", startup_script_path)
        startup_script_path = os.path.abspath(os.path.expanduser(startup_script_path))
        nspace = load_startup_script(startup_script_path)

    else:
        logger.warning("Source of startup information is not specified. No startup code is loaded.")
        nspace = {}

    return nspace


_internal_process = contextvars.ContextVar("internal_process", default=False)


def no_reentry(func):
    @functools.wraps(func)
    async def inner(*args, **kwargs):
        if _internal_process.get():
            return
        try:
            _internal_process.set(True)
            return await func(*args, **kwargs)
        finally:
            _internal_process.set(False)

    return inner
