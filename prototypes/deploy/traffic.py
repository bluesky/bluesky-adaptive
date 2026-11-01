"""Play "other users" on the prototype beamline through the queueserver.

Each round sets a random DCM crystal-1 pitch offset (``dcm_c1.pitch``) and scans
``dcm_c2.pitch`` while recording ``diag2``. The C1 offset is recorded in each run's
``baseline`` stream, so every run is a self-contained training point for the passive
learning server. Usage, from the repository root::

    pixi run -e integration python -m prototypes.deploy.traffic --rounds 5 --seed 0 --points 11
"""

import argparse
import logging
import random
import sys
import time
from typing import Any

from bluesky_queueserver_api import BPlan
from bluesky_queueserver_api.comm_base import RequestFailedError
from bluesky_queueserver_api.zmq import REManagerAPI

logger = logging.getLogger(__name__)

DEFAULT_CONTROL_ADDR = "tcp://127.0.0.1:60615"
DEFAULT_INFO_ADDR = "tcp://127.0.0.1:60625"
MODES = ("scan", "list")


class TrafficError(RuntimeError):
    """A traffic plan was rejected by, or failed in, the queueserver."""


def run_item(
    rm: REManagerAPI, item: BPlan, *, timeout: float = 600.0, poll_interval: float = 0.5
) -> tuple[str, dict[str, Any]]:
    """Queue one plan and block until it appears in the queueserver history.

    Starts the queue when the manager is idle with items queued (autostart may be off,
    for example after another client's failed plan).

    Parameters
    ----------
    rm : REManagerAPI
        Connected RE Manager client.
    item : BPlan
        Plan to queue.
    timeout : float, optional
        Seconds to wait for the item to finish.
    poll_interval : float, optional
        Seconds between history polls.

    Returns
    -------
    tuple of (str, dict)
        The queue ``item_uid`` and the history item's ``result`` (``exit_status``, ``run_uids``, ``msg``, ...).

    Raises
    ------
    TrafficError
        If the item is rejected or finishes with an exit status other than ``completed``.
    TimeoutError
        If the item does not finish within ``timeout``.
    """
    name = item.to_dict()["name"]
    response = rm.item_add(item)
    if not response.get("success", False):
        raise TrafficError(f"queueserver rejected {item.to_dict()!r}: {response.get('msg')}")
    item_uid = response["item"]["item_uid"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in reversed(rm.history_get()["items"]):
            if entry.get("item_uid") == item_uid:
                result = entry.get("result", {})
                if result.get("exit_status") != "completed":
                    raise TrafficError(
                        f"item {item_uid} ({name}) finished with exit status {result.get('exit_status')!r}: "
                        f"{result.get('msg')}"
                    )
                return item_uid, result
        status = rm.status()
        if status["manager_state"] == "idle" and status["items_in_queue"] > 0:
            try:
                rm.queue_start()
            except RequestFailedError as exc:
                # Autostart (or another client) may start the queue between status() and queue_start().
                logger.debug("queue_start not needed: %s", exc)
        time.sleep(poll_interval)
    raise TimeoutError(f"item {item_uid} ({name}) did not finish within {timeout} s")


def run_traffic(
    rm: REManagerAPI,
    rounds: int,
    seed: int,
    points: int,
    c1_range: float = 3e-5,
    c2_range: float = 1e-4,
    mode: str = "scan",
    *,
    restore: bool = True,
    timeout: float = 600.0,
) -> list[dict[str, Any]]:
    """Run ``rounds`` user scans of ``dcm_c2.pitch`` at random ``dcm_c1.pitch`` offsets.

    Parameters
    ----------
    rm : REManagerAPI
        Connected RE Manager client.
    rounds : int
        Number of scans.
    seed : int
        Seed for the offsets and list-scan positions.
    points : int
        Points per scan.
    c1_range : float, optional
        Offsets are drawn from ``U(-c1_range, c1_range)`` (radians).
    c2_range : float, optional
        C2 pitch is scanned over ``[-c2_range, c2_range]`` (radians).
    mode : {"scan", "list"}, optional
        ``scan`` for an evenly spaced ``scan``; ``list`` for a ``list_scan`` over sorted random positions.
    restore : bool, optional
        Move ``dcm_c1.pitch`` back to 0 at the end (also on failure).
    timeout : float, optional
        Seconds to wait for each queued item.

    Returns
    -------
    list of dict
        One record per round: ``{"round", "offset", "item_uid", "run_uids"}``.

    Raises
    ------
    ValueError
        If ``mode`` is unknown or ``points`` < 1.
    TrafficError
        If a plan is rejected or fails.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if points < 1:
        raise ValueError(f"points must be >= 1, got {points}")
    rng = random.Random(seed)
    records: list[dict[str, Any]] = []
    try:
        for round_index in range(rounds):
            offset = rng.uniform(-c1_range, c1_range)
            run_item(rm, BPlan("mv", "dcm_c1.pitch", offset), timeout=timeout)
            if mode == "scan":
                plan = BPlan("scan", ["diag2"], "dcm_c2.pitch", -c2_range, c2_range, num=points)
            else:
                positions = sorted(rng.uniform(-c2_range, c2_range) for _ in range(points))
                plan = BPlan("list_scan", ["diag2"], "dcm_c2.pitch", positions)
            item_uid, result = run_item(rm, plan, timeout=timeout)
            records.append(
                {"round": round_index, "offset": offset, "item_uid": item_uid, "run_uids": list(result.get("run_uids", []))}
            )
    finally:
        if restore:
            run_item(rm, BPlan("mv", "dcm_c1.pitch", 0.0), timeout=timeout)
    return records


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; prints one line per round.

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
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--points", type=int, default=11)
    parser.add_argument("--c1-range", type=float, default=3e-5)
    parser.add_argument("--c2-range", type=float, default=1e-4)
    parser.add_argument("--mode", choices=MODES, default="scan")
    parser.add_argument("--no-restore", action="store_true", help="leave dcm_c1.pitch at the last offset")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--zmq-control-addr", default=DEFAULT_CONTROL_ADDR)
    parser.add_argument("--zmq-info-addr", default=DEFAULT_INFO_ADDR)
    args = parser.parse_args(argv)

    rm = REManagerAPI(zmq_control_addr=args.zmq_control_addr, zmq_info_addr=args.zmq_info_addr)
    try:
        records = run_traffic(
            rm,
            args.rounds,
            args.seed,
            args.points,
            c1_range=args.c1_range,
            c2_range=args.c2_range,
            mode=args.mode,
            restore=not args.no_restore,
            timeout=args.timeout,
        )
    finally:
        rm.close()
    for record in records:
        print(f"round {record['round']}: offset={record['offset']:+.3e} run_uids={','.join(record['run_uids'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
