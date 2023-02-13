from bluesky_adaptive.server.utils import register_variable

print(f"The script implementing the simulated agent ...")

class AA:
    def __init__(self):
        self._depth = 10
        self._width = 60

aa = AA()

bb = {"x": 100, "y": 900}

_v = 600

def width_getter():
    return aa._depth

def width_setter(value):
    aa._depth = value
    return value

# raise Exception("Hello")

register_variable("depth", aa, "_depth", pv_type="float", global_dict=globals())
register_variable("width", None, None, pv_type="float",
                  getter=width_getter, setter=width_setter, global_dict=globals())
register_variable("x", bb, "x", pv_type="int", pv_max_length=1000, global_dict=globals())
register_variable("y", bb, "y", pv_type="int", global_dict=globals())
register_variable("v", globals(), "_v", pv_type="int", global_dict=globals())

# print(f"_agent_server_variables__ = {_agent_server_variables__}")
