"""RE Manager startup script for the bluesky-adaptive prototypes on the TST XRT BMM simulation.

Runs inside the tst-sim-tools ``qs`` environment. Requires ``TILED_URI`` (and optionally
``TILED_API_KEY``) for the TiledWriter and ``TST_DATA_DIR`` for detector HDF5 files.
Names starting with ``_`` are hidden from the queueserver by the permissions file.
"""

import os

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from bluesky import RunEngine
from bluesky.plan_stubs import mv
from bluesky.plans import count, grid_scan, list_scan, rel_scan, scan
from bluesky_tiled_plugins import TiledWriter
from tiled.client import from_uri

# The RunEngine must exist before the devices are created: init_devices uses the bluesky event loop.
RE = RunEngine({})
RE.subscribe(TiledWriter(from_uri(os.environ["TILED_URI"], api_key=os.environ.get("TILED_API_KEY"))))

# Applies the aioca CA-context workaround before connecting the devices.
from tst_sim_tools.startup.devices import *

_sd = bpp.SupplementalData(baseline=[m1, dcm_c1, dcm_c2, m2, m3])
RE.preprocessors.append(_sd)


def adaptive_acquire(suggestions, actuators, sensors=None, *, md=None):
    """Acquire one Bluesky run with one event per suggestion.

    Parameters are deliberately unannotated: the queueserver converts device-path strings
    (for example ``dcm_c2.pitch``) to device objects only for unannotated parameters.

    Parameters
    ----------
    suggestions : list of dict
        Points to measure, keyed by actuator ophyd name (for example ``dcm_c2-pitch``) plus ``_id``.
        Keys without a matching actuator are ignored for motion. Event ``i`` corresponds to
        suggestion ``i``.
    actuators : list of movable devices
        Devices moved to the suggested values and read in every event.
    sensors : list of readable devices, optional
        Detectors triggered and read in every event.
    md : dict, optional
        Extra start-document metadata (for example ``blop_correlation_uid``).

    Returns
    -------
    str
        The run uid.

    Yields
    ------
    Msg
        Bluesky messages. Detectors are staged inside the run, so their HDF5 files close
        before the stop document is emitted.
    """
    suggestions = list(suggestions)
    actuators = list(actuators)
    sensors = list(sensors or [])
    readables = sensors + [a for a in actuators if all(a is not s for s in sensors)]
    _md = {
        "plan_name": "adaptive_acquire",
        "detectors": [s.name for s in sensors],
        "motors": [a.name for a in actuators],
        "num_points": len(suggestions),
        "blop_suggestions": suggestions,
        **(md or {}),
    }

    def _inner():
        for suggestion in suggestions:
            args = []
            for actuator in actuators:
                if actuator.name in suggestion:
                    args += [actuator, suggestion[actuator.name]]
            if args:
                yield from bps.mv(*args)
            yield from bps.trigger_and_read(readables)

    return (yield from bpp.run_wrapper(bpp.stage_wrapper(_inner(), readables), md=_md))
