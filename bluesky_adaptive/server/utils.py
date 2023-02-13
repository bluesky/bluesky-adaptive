import importlib
import os
import sys
import traceback

import logging
logger = logging.getLogger(__name__)


def register_variable(name, obj=None, attr_or_key=None, *, getter=None, setter=None, pv_type=None, pv_max_length=None, global_dict=None):
    if global_dict is None:
        raise ValueError("Missing required parameter: set 'global_dict=globals()'")
    if not isinstance(name, str):
        raise TypeError(f"Variable name must be a string: name={name!r}")
    if not isinstance(attr_or_key, (str, type(None))):
        raise TypeError(f"Name of an attribute or a key must be a string: attr_or_key={attr_or_key!r}")
    if not isinstance(getter, type(None)) and not callable(getter):
        raise TypeError(f"Parameter 'getter' must be callable or None: type(get)={type(getter)!r}")
    if not isinstance(setter, type(None)) and not callable(setter):
        raise TypeError(f"Parameter 'setter' must be callable or None: type(setter)={type(setter)!r}")
    if not isinstance(pv_type, (str, None)):
        raise TypeError(f"PV type must be a string: pv_type={pv_type!r}")
    if not (isinstance(pv_max_length, int) or (pv_max_length is None)):
        raise TypeError(f"PV max length must be int: pv_max_length={pv_max_length!r}")

    global_dict.setdefault("_agent_server_variables__", {})
    global_dict["_agent_server_variables__"][name] = {
        "object": obj,
        "attr_or_key": attr_or_key,
        "pv_type": pv_type,
        "pv_max_length": pv_max_length,
        "getter": getter,
        "setter": setter,
    }


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
        sm_keys = list(sys.modules.keys())

    try:
        nspace = {}
        if not os.path.isfile(script_path):
            raise IOError(f"Startup file {script_path!r} was not found")

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
            for key in list(sys.modules.keys()):
                if key not in sm_keys:
                    # print(f"Deleting the key '{key}'")
                    del sys.modules[key]

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
        logger.warning(
            "Source of startup information is not specified. No startup code is loaded."
        )
        nspace = {}

    return nspace
