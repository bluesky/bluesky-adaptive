import asyncio
import contextvars
import copy
import logging

import caproto
from caproto import SkipWrite
from caproto.asyncio.server import start_server
from caproto.server import PVGroup, get_pv_pair_wrapper

from .comms import CommTimeoutError
from .server_resources import SR
from .utils import no_reentry
from .worker import EState

caproto.select_backend("array")


pvproperty_with_rbv = get_pv_pair_wrapper(setpoint_suffix="", readback_suffix="_RBV")

logger = logging.getLogger(__name__)


class IOC_Server:
    """
    Before the server is started, ``SR`` must be configured to communicate with
    the worker.

    Starting the IOC Server:

    ioc_server = IOC_Server(ioc_prefix="...")
    ioc_server.start()  # Starts the tasks

    Stopping the server:

    ioc_server.stop()
    """

    def __init__(self, ioc_prefix="agent_ioc", pv_update_period=0.1):
        self._ioc_prefix = ioc_prefix
        self._pv_update_period = pv_update_period
        self._vars_desc = {}
        self._vars_to_pv_names = {}
        self._ioc = None

        self._ioc_server_task = None
        self._pv_update_task = None

        self._ctxvar_internal_update = contextvars.ContextVar("internal_update")

    def _create_ioc_class(self):
        """
        Create the new IOC class with the set of PVs defined by variable descriptions.
        Two PVs (setpoint and readback) are constructed for each variable using
        the following pattern:

            <ioc_prefix>:<PV_NAME>
            <ioc_prefix>:<PV_NAME>_RBV

        where PV_NAME is the variable name converted to upper-case letters. The readback
        PV is read-only, the setpoint PV can be used for reading and writing. In some cases,
        the setpoint PV may take longer to update than the readback PV.
        """
        body = {}
        self._vars_to_pv_names.clear()

        for name, vars_desc in self._vars_desc.items():
            property_name = name.lower().replace(":", "_")
            pv_name = name.upper()
            params = copy.deepcopy(vars_desc)

            print(f"Creating a new PV: {pv_name!r}")

            def put_factory(var_name):
                @no_reentry
                async def put(obj, instance, value):
                    # Update setpoint and readback only if the new value is different from the PV value
                    #   to avoid unnecessary triggering of monitors. This behavior seems consistent
                    #   with standard IOC behavior.

                    if value != obj.setpoint.value:
                        await obj.setpoint.write(value)

                    if not self._ctxvar_internal_update.get(False):
                        # Update PVs and the respective variables in the worker after external PV put.
                        print(f"Writing new value of the variable {var_name!r}: type={type(value)} value={value}")
                        vres = await SR.worker_set_variable(name=var_name, value=value)
                        value = vres[var_name]
                        if value != obj.setpoint.value:
                            await obj.setpoint.write(value)

                    if value != obj.readback.value:
                        await obj.readback.write(value)

                    raise SkipWrite()

                return put

            body[property_name] = pvproperty_with_rbv(name=pv_name, put=put_factory(name), **params)
            self._vars_to_pv_names[name] = self._ioc_prefix + ":" + pv_name

        return type("ServerIOC", (PVGroup,), body)

    async def _start_ioc_server(self):
        """
        Creates the IOC class based on variable descriptions (``vars_desc``) and then
        starts the IOC server.

        The task runs continuously until the IOC server is stopped.
        """

        await self._read_vars_desc()

        # The options must be passed somehow from server configuration
        ioc_prefix = self._ioc_prefix
        ioc_options = {"prefix": f"{ioc_prefix}:", "macros": {}}
        run_options = {"log_pv_names": False, "interfaces": ["0.0.0.0"]}

        server_ioc_class = self._create_ioc_class()

        try:
            self._ioc = server_ioc_class(**ioc_options)
            await start_server(self._ioc.pvdb, **run_options)

        except Exception as ex:
            print(f"Failed to start the IOC server: {ex}")
            import traceback

            traceback.print_exc()

    async def _update_pv_values(self):
        """
        Update all PVs by reading the respective variables from the worker.
        Note, that the PV writes that trigger monitors are performed only if
        the new value is different from the current PV value.

        The task runs continuously until the IOC server is stopped.
        """
        # TODO: is there a more efficient and quick way to update PVs?
        while True:
            await asyncio.sleep(self._pv_update_period)
            for name, pv_name in self._vars_to_pv_names.items():
                try:
                    pv = self._ioc.pvdb[pv_name]
                    vd = await SR.worker_get_variable(name)
                    value = vd[name]
                    self._ctxvar_internal_update.set(True)
                    await pv.write(value)
                    self._ctxvar_internal_update.set(False)
                except Exception as ex:
                    print(ex)

    async def _read_vars_desc(self):
        """
        Read variable descriptions from the worker and convert them to caproto PV parameters.
        """
        while True:
            try:
                await asyncio.sleep(1)
                status = await SR.worker_get_status()

                if status["state"] != EState.INITIALIZING.value:
                    vars = await SR.worker_get_all_variable_descriptions()
                    vars_desc = {}
                    for k, v in vars.items():
                        # Variable MUST have valid 'pv_type'. Otherwise no PV is created.
                        if "pv_type" in v and isinstance(v["pv_type"], str):
                            vars_desc[k] = {"dtype": eval(v["pv_type"])}  # string to type
                            if "pv_max_length" in v and isinstance(v["pv_max_length"], int):
                                vars_desc[k]["max_length"] = v["pv_max_length"]

                    self._vars_desc = vars_desc
                    break

            except CommTimeoutError:
                pass
            except Exception as ex:
                logger.exception(ex)
                raise

    async def _run_ioc_server(self):
        """
        Create tasks for the server and PV updates.
        """
        try:
            self._ioc_server_task = asyncio.create_task(self._start_ioc_server())
            self._pv_update_task = asyncio.create_task(self._update_pv_values())
            logger.info("IOC server configuration was loaded. Server startup was initiated.")
        except Exception as ex:
            logger.exception(ex)
            raise

    async def start(self):
        """
        Start the IOC server.
        """
        await self._run_ioc_server()

    def stop(self):
        """
        Stop the IOC server.
        """
        if self._pv_update_task:
            self._pv_update_task.cancel()
        if self._ioc_server_task:
            self._ioc_server_task.cancel()
