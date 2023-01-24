import copy

from caproto import ChannelType
from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run

from caproto.asyncio.server import start_server

from textwrap import dedent


def create_ioc_class(pvs_dict):
    body = {}
    for name, pv_params in pvs_dict.items():
        property_name = name.lower().replace(":", "_")
        pv_name = name.upper()
        params = copy.deepcopy(pv_params)
        params.update({"value": "{}", "max_length": 2048})

        print(f"Adding PV: pv_name={pv_name!r}")
        body[property_name] = pvproperty(name=pv_name, **params)
        body[property_name + "_rbv"] = pvproperty(name=pv_name + ".RBV", **params)

    return type("ServerIOC", (PVGroup,), body)


ioc_name = "agent_ioc"  # Should be parametrized


async def start_ioc_server(pvs_dict):

    # The options must be passed somehow from server configuration
    ioc_options = {"prefix": f"{ioc_name}:", "macros": {}}
    run_options = {'log_pv_names': False, 'interfaces': ['0.0.0.0']}

    server_ioc_class = create_ioc_class(pvs_dict=pvs_dict)
    try:
        ioc = server_ioc_class(**ioc_options)
        await start_server(ioc.pvdb, **run_options)
    except Exception as ex:
        print("Failed to start the IOC server ...")
        import traceback
        traceback.print_exc()
