import sys

from bluesky_adaptive.server import register_variable, shutdown_decorator, start_task, startup_decorator

print("The script implementing the simulated agent ...")


class AA:
    def __init__(self):
        self._depth = 10
        self._width = 60


aa = AA()

bb = {"x": 100, "y": 900}

_v = 600

_no_pv = 100


def some_function(a):
    print(f"Function is running: a = {a!r}", flush=True)


def width_getter():
    return aa._width


def width_setter(value):
    aa._width = value
    # task_info = start_task(some_function, 50)
    task_info = start_task(some_function, value, run_in_background=False)
    print(f"task_info = {task_info}")
    return value


@startup_decorator
def startup1():
    print("This is startup function #1")
    aa._depth = 20
    print(f"sys.__stdin__.isatty() = {sys.__stdin__.isatty()}")


@startup_decorator
def startup2():
    print("This is startup function #2")
    aa._width = 70


@shutdown_decorator
def shutdown1():
    print("This is shutdown function #1")


@shutdown_decorator
def shutdown2():
    print("This is shutdown function #2")


# raise Exception("Hello")

register_variable("depth", aa, "_depth", pv_type="float")
register_variable("width", None, None, pv_type="float", getter=width_getter, setter=width_setter)
register_variable("x", bb, "x", pv_type="int", pv_max_length=1000)
register_variable("y", bb, "y", pv_type="int")
register_variable("v", globals(), "_v", pv_type="int")
register_variable("no_pv", globals(), "_no_pv")
