"""
Drive one ``ax-bo`` agent through the alignment server's HTTP API.

1. Create an agent from ``spec.json`` and run the optimization loop, printing each iteration.
2. Print the best points.
3. Ask for two suggestions. Acquire the first with ``/submit-suggestions``, together with the nominal
   position as a manual point; acquire the second on the client side and report it with ``/ingest``.
4. Start another loop, stop it while a plan is in flight, and check that the agent queued nothing more.
5. Move the DOF actuators back to 0, also on failure, so later runs by other users start from the nominal beamline.

Start the deployment (``prototypes/deploy/README.md``) and the server first::

    pixi run -e integration uvicorn prototypes.alignment_server.app:app --port 8100
    pixi run -e integration python -m prototypes.alignment_server.demo
"""

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from bluesky_queueserver_api import BPlan

from prototypes.common.demo import FINAL_STATES, DemoError, call, follow, re_manager, server_user
from prototypes.common.image_metrics import ImageMetricsParams, event_outcomes_from_run
from prototypes.common.tiled_data import run_client
from prototypes.deploy.traffic import run_item

SPEC_PATH = Path(__file__).with_name("spec.json")
SERVER = "alignment"


class IterationPrinter:
    """Print one line per acquired point: DOF values in µrad and the outputs."""

    def __init__(self, dofs: Sequence[str], outputs: Sequence[str]):
        self.dofs = list(dofs)
        self.outputs = list(outputs)

    def position(self, point: Mapping[str, Any]) -> str:
        """Format the DOF values of a point."""
        return " ".join(f"{name}={point[name] * 1e6:+6.1f}urad" for name in self.dofs)

    def outcome(self, point: Mapping[str, Any]) -> str:
        """Format the outputs of a point."""
        return " ".join(f"{name}={point[name]:.1f}" for name in self.outputs if name in point)

    def __call__(self, record: dict[str, Any]) -> None:
        """Print an iteration record from the agent's history."""
        outcomes = {point.get("_id"): point for point in record.get("points") or []}
        for suggestion in record["suggestions"]:
            point = outcomes.get(suggestion.get("_id"))
            result = self.outcome(point) if point is not None else f"error: {record['error']}"
            run = str(record["run_uid"])[:8]
            print(
                f"  iteration {record['iteration']:2d} run {run} _id {suggestion.get('_id')!s:>3} "
                f"{self.position(suggestion)}  {result}"
            )


def acquire_on_client(spec: Mapping[str, Any], suggestion: Mapping[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    """
    Acquire one suggestion with the beamline's plan and compute its outcomes, as a client with its own analysis would.

    Parameters
    ----------
    spec : Mapping[str, Any]
        The agent spec; gives the actuators, sensors and metrics.
    suggestion : Mapping[str, Any]
        A suggestion from ``/suggest``.
    timeout : float
        Seconds to wait for the run to be readable from Tiled.

    Returns
    -------
    dict[str, Any]
        The point to ingest: the suggestion's ``_id`` and the outputs.
    """
    params = ImageMetricsParams.model_validate(spec["evaluation"]["params"])
    actuators = [dof["actuator"] for dof in spec["dofs"]]
    rm = re_manager(SERVER)
    try:
        _, result = run_item(rm, BPlan("adaptive_acquire", [dict(suggestion)], actuators, spec["sensors"]))
    finally:
        rm.close()
    uid = result["run_uids"][0]
    # The queueserver reports the plan finished before Tiled may have the run's stop document.
    deadline = time.monotonic() + timeout
    while True:
        try:
            run = run_client(uid)
            if run.metadata.get("stop"):
                rows = event_outcomes_from_run(run, params)
                return {"_id": suggestion["_id"], **rows[0]}
        except KeyError:
            pass
        if time.monotonic() > deadline:
            raise DemoError(f"run {uid} is not readable from Tiled after {timeout:.0f} s")
        time.sleep(0.5)


def server_items(user: str) -> set[str]:
    """Return the UIDs of the queue and history items submitted as ``user``."""
    rm = re_manager(SERVER)
    try:
        items = rm.history_get()["items"] + rm.queue_get()["items"]
    finally:
        rm.close()
    return {item["item_uid"] for item in items if item.get("user") == user}


def stop_mid_run(client: httpx.Client, agent_id: str, iterations: int, printer: IterationPrinter, shown: int) -> bool:
    """
    Start a loop, stop it while the second plan is in flight, and check what happened.

    Returns
    -------
    bool
        Whether the agent ingested the in-flight run and queued nothing after the stop.
    """
    status = call(client, "POST", f"/agents/{agent_id}/run", json={"iterations": iterations, "n_points": 1})
    operation_id = status["operation"]["id"]
    while True:
        status = call(client, "GET", f"/agents/{agent_id}")["status"]
        records = [
            r for r in call(client, "GET", f"/agents/{agent_id}/history")["iterations"] if r["operation"] == operation_id
        ]
        if records and status["in_flight"] is not None:
            break
        if status["state"] in FINAL_STATES:
            raise DemoError(f"the loop ended ({status['state']}) before it could be stopped")
        time.sleep(0.2)
    done_before = len(records)
    user = server_user(SERVER)
    # Earlier agents of this server submit as the same user, so only items that appear after this point count.
    before_stop = server_items(user)
    stopped = call(client, "POST", f"/agents/{agent_id}/stop")
    print(f"  /stop with iteration {status['in_flight']['iteration']} in flight -> {stopped['state']}")
    status, shown = follow(client, agent_id, printer, shown=shown)
    print(f"  -> {status['state']}")
    history = call(client, "GET", f"/agents/{agent_id}/history")["iterations"]
    records = [r for r in history if r["operation"] == operation_id]
    after_stop = records[done_before:]
    ingested_after_stop = [r for r in after_stop if r["run_uid"] is not None and r["error"] is None]
    # Give a wrongly submitted plan time to show up before looking for it.
    time.sleep(3.0)
    recorded = {r["item_uid"] for r in history}
    unrecorded = sorted(server_items(user) - before_stop - recorded)
    print(
        f"  iterations before /stop: {done_before}; recorded after /stop: {len(after_stop)}, "
        f"ingested: {len(ingested_after_stop)}"
    )
    print(f"  plans by {user!r} queued after /stop that the agent did not record: {unrecorded or 'none'}")
    expected = 1 if stopped["state"] == "stopping" else 0
    return status["state"] == "stopped" and len(ingested_after_stop) == expected and not unrecorded


def main(argv: list[str] | None = None) -> int:
    """Run the demo; returns the exit status."""
    parser = argparse.ArgumentParser(description="Drive the alignment server through its HTTP API.")
    parser.add_argument("--url", default="http://localhost:8100", help="server URL")
    parser.add_argument("--iterations", type=int, default=15, help="iterations of the first loop")
    parser.add_argument("--stop-iterations", type=int, default=10, help="iterations of the loop that is stopped")
    args = parser.parse_args(argv)
    spec = json.loads(SPEC_PATH.read_text())
    dofs = [dof["name"] for dof in spec["dofs"]]

    rm = re_manager(SERVER)
    try:
        with httpx.Client(base_url=f"{args.url}/api/v1", timeout=120.0) as client:
            for summary in call(client, "GET", "/queueservers"):
                print(f"queueserver {summary['name']}: run feed {summary['run_feed']}")
            agent = call(client, "POST", "/agents", json=spec)
            agent_id = agent["id"]
            printer = IterationPrinter(dofs, agent["outputs"])
            print(f"agent {agent_id}: entries {agent['entries']}, capabilities {agent['capabilities']}")

            print(f"\n1. /run: {args.iterations} iterations of 1 point")
            call(client, "POST", f"/agents/{agent_id}/run", json={"iterations": args.iterations, "n_points": 1})
            status, shown = follow(client, agent_id, printer)
            print(f"  -> {status['state']}")
            if status["state"] != "completed":
                raise DemoError(f"the loop ended {status['state']}: {status['operation']}")

            print("\n2. /best-points")
            for best in call(client, "GET", f"/agents/{agent_id}/best-points"):
                means = {name: value[0] if isinstance(value, list) else value for name, value in best["outcomes"].items()}
                print(f"  _id {best['_id']} {printer.position(best['parameters'])}  {printer.outcome(means)}")

            print("\n3. /suggest 2 points; /submit-suggestions the first with the nominal position; /ingest the second")
            first, second = call(client, "POST", f"/agents/{agent_id}/suggest", json={"n_points": 2})["suggestions"]
            nominal = dict.fromkeys(dofs, 0.0)
            call(client, "POST", f"/agents/{agent_id}/submit-suggestions", json={"suggestions": [first, nominal]})
            status, shown = follow(client, agent_id, printer, shown=shown)
            print(f"  -> {status['state']}")
            point = acquire_on_client(spec, second)
            call(client, "POST", f"/agents/{agent_id}/ingest", json={"points": [point]})
            print(f"  client-side _id {second['_id']} {printer.position(second)}  {printer.outcome(point)}: ingested")

            print(f"\n4. /run {args.stop_iterations} iterations; /stop after the first")
            stopped_cleanly = stop_mid_run(client, agent_id, args.stop_iterations, printer, shown)
            print(f"  stop semantics: {'OK' if stopped_cleanly else 'FAILED'}")
    finally:
        # An axis left off zero is a condition other users' runs do not record as moved, e.g. for passive learning.
        run_item(rm, BPlan("mv", *[value for dof in spec["dofs"] for value in (dof["actuator"], 0.0)]))
        rm.close()
    return 0 if stopped_cleanly else 1


if __name__ == "__main__":
    sys.exit(main())
