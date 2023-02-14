from bluesky_adaptive.server import register_variable, start_task

print(f"The script implementing the simulated agent ...")

class AA:
    def __init__(self):
        self._depth = 10
        self._width = 60

aa = AA()

bb = {"x": 100, "y": 900}

_v = 600

def some_function(a):
    print(f"Function is running: a = {a!r}", flush=True)

def width_getter():
    return aa._depth

def width_setter(value):
    aa._depth = value
    # task_info = start_task(some_function, 50)
    task_info = start_task(some_function, 50, run_in_background=False)
    print(f"task_info = {task_info}")
    return value

# raise Exception("Hello")

register_variable("depth", aa, "_depth", pv_type="float")
register_variable("width", None, None, pv_type="float", getter=width_getter, setter=width_setter)
register_variable("x", bb, "x", pv_type="int", pv_max_length=1000)
register_variable("y", bb, "y", pv_type="int")
register_variable("v", globals(), "_v", pv_type="int")
