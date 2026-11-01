"""Open the RE Worker environment of the prototype queueserver and list what it exposes.

Run from either the repository ``integration`` env or the tst-sim-tools ``qs`` env::

    python prototypes/deploy/open_env.py

Exits with status 1 (after printing the captured worker console) if the environment
fails to open. On success it sets ``ignore_failures=True`` and turns autostart on, so a
failed plan does not stall the queue for other clients.
"""

import argparse
import sys
from collections.abc import Iterator, Mapping
from typing import Any

from bluesky_queueserver_api.zmq import REManagerAPI

DEFAULT_CONTROL_ADDR = "tcp://127.0.0.1:60615"
DEFAULT_INFO_ADDR = "tcp://127.0.0.1:60625"


def flatten_devices(devices: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Walk ``devices_allowed`` output and yield ``(dotted path, description)`` pairs.

    Parameters
    ----------
    devices : Mapping[str, Any]
        Device descriptions keyed by name; nested devices live under ``components``.
    prefix : str, optional
        Dotted path of the parent device.

    Yields
    ------
    tuple of (str, Mapping[str, Any])
        Device path such as ``dcm_c2.pitch`` and its description with ``is_movable`` etc.
    """
    for name, description in devices.items():
        path = f"{prefix}.{name}" if prefix else name
        yield path, description
        yield from flatten_devices(description.get("components", {}) or {}, path)


def main(argv: list[str] | None = None) -> int:
    """Open the environment, configure the queue, and print plans and devices.

    Parameters
    ----------
    argv : list of str, optional
        Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zmq-control-addr", default=DEFAULT_CONTROL_ADDR)
    parser.add_argument("--zmq-info-addr", default=DEFAULT_INFO_ADDR)
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the environment to open")
    parser.add_argument("--all-devices", action="store_true", help="list every device component, not only optics/screens")
    args = parser.parse_args(argv)

    rm = REManagerAPI(zmq_control_addr=args.zmq_control_addr, zmq_info_addr=args.zmq_info_addr)
    try:
        # The console monitor only captures lines published after enable().
        rm.console_monitor.enable()
        rm.wait_for_idle(timeout=60)
        if not rm.status()["worker_environment_exists"]:
            rm.environment_open()
            rm.wait_for_idle(timeout=args.timeout)
        if not rm.status(reload=True)["worker_environment_exists"]:
            print(rm.console_monitor.text(), file=sys.stderr)
            print("RE Worker environment failed to open", file=sys.stderr)
            return 1
        rm.queue_mode_set(ignore_failures=True)
        rm.queue_autostart(True)

        plans = rm.plans_allowed()["plans_allowed"]
        print("Plans allowed:")
        for name in sorted(plans):
            params = ", ".join(p["name"] for p in plans[name].get("parameters", []))
            print(f"  {name}({params})")

        devices = rm.devices_allowed()["devices_allowed"]
        print("Devices allowed (path: movable/readable/flyable):")
        for path, description in flatten_devices(devices):
            depth = path.count(".")
            if not args.all_devices and depth > 1:
                continue
            flags = "/".join(flag[3:] for flag in ("is_movable", "is_readable", "is_flyable") if description.get(flag))
            print(f"  {path}: {flags or '-'}")
    finally:
        rm.console_monitor.disable()
        rm.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
