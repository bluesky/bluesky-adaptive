"""
Drive one ``mlp-sgd`` agent through the passive learning server's HTTP API.

1. Create a contextual agent from ``spec.json`` and subscribe it to every completed run.
2. Play other users: scans of ``dcm_c2.pitch`` at random ``dcm_c1.pitch`` offsets (``prototypes.deploy.traffic``).
3. Show what ingestion learned from those runs.
4. Suggest in the context of two of the runs and compare with each run's measured best position.
5. Run the agent itself from the context of the last run, and show how the context chains through its own runs.

Start the deployment (``prototypes/deploy/README.md``) and the server first::

    pixi run -e integration uvicorn prototypes.passive_server.app:app --port 8200
    pixi run -e integration python -m prototypes.passive_server.demo
"""

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from bluesky_queueserver_api import BPlan

from prototypes.common.demo import DemoError, call, follow, re_manager
from prototypes.common.image_metrics import ImageMetricsParams, event_outcomes_from_run
from prototypes.common.tiled_data import run_client, stream_column
from prototypes.deploy.traffic import run_item, run_traffic

SPEC_PATH = Path(__file__).with_name("spec.json")
SERVER = "passive"
CONTEXT_AXIS = "dcm_c1.pitch"
"""Device the traffic offsets; the demo leaves the last offset in place for the agent's own runs, then restores it."""


def wait_for_ingestion(
    client: httpx.Client, agent_id: str, run_uids: Sequence[str], timeout: float = 120.0
) -> dict[str, Any]:
    """Poll the agent's ingestion status until it has a record for every run in ``run_uids``."""
    deadline = time.monotonic() + timeout
    while True:
        status = call(client, "GET", f"/agents/{agent_id}/ingestion")
        seen = {record["run_uid"] for record in status["runs"]}
        missing = [uid for uid in run_uids if uid not in seen]
        if not missing:
            return status
        if time.monotonic() > deadline:
            raise DemoError(f"ingestion has no record of runs {missing} after {timeout:.0f} s")
        time.sleep(1.0)


def measured_best(uid: str, dof: str, objective: str, metrics: ImageMetricsParams) -> tuple[float, float]:
    """Return the DOF readback and objective value of the run's event with the largest objective."""
    run = run_client(uid)
    positions = stream_column(run, metrics.stream, dof)
    values = [row[objective] for row in event_outcomes_from_run(run, metrics)]
    best = int(np.argmax(values))
    return float(positions[best]), float(values[best])


def main(argv: list[str] | None = None) -> int:
    """Run the demo; returns the exit status."""
    parser = argparse.ArgumentParser(description="Drive the passive learning server through its HTTP API.")
    parser.add_argument("--url", default="http://localhost:8200", help="server URL")
    parser.add_argument("--rounds", type=int, default=10, help="traffic scans")
    parser.add_argument("--points", type=int, default=11, help="points per traffic scan")
    parser.add_argument("--seed", type=int, default=0, help="seed of the traffic offsets")
    parser.add_argument(
        "--tolerance", type=float, default=15e-6, help="allowed distance of a top suggestion from the run's measured best"
    )
    args = parser.parse_args(argv)
    spec = json.loads(SPEC_PATH.read_text())
    dof = spec["dofs"][0]["name"]
    feature = spec["context"]["params"]["signals"][0]
    objective = spec["objectives"][0]["name"]
    metrics = ImageMetricsParams.model_validate(spec["evaluation"]["params"])
    checks: dict[str, bool] = {}

    rm = re_manager(SERVER)
    try:
        with httpx.Client(base_url=f"{args.url}/api/v1", timeout=120.0) as client:
            for summary in call(client, "GET", "/queueservers"):
                print(f"queueserver {summary['name']}: run feed {summary['run_feed']}")
            agent = call(client, "POST", "/agents", json=spec)
            agent_id = agent["id"]
            print(f"agent {agent_id}: context features {agent['context_features']}, capabilities {agent['capabilities']}")
            print(f"/ingestion/subscribe -> {call(client, 'POST', f'/agents/{agent_id}/ingestion/subscribe')}")
            response = client.post(f"/agents/{agent_id}/suggest", json={"n_points": 1})
            print(f"/suggest without context -> {response.status_code}: {response.json().get('detail')}")
            checks["/suggest without context is rejected with 422"] = response.status_code == 422

            print(f"\n1. traffic: {args.rounds} scans of {dof} at random {feature} offsets")
            records = run_traffic(rm, args.rounds, args.seed, args.points, restore=False)
            for record in records:
                print(
                    f"  round {record['round']}: {feature}={record['offset'] * 1e6:+6.1f}urad, run {record['run_uids'][0]}"
                )
            traffic_uids = [record["run_uids"][0] for record in records]

            print("\n2. /ingestion")
            status = wait_for_ingestion(client, agent_id, traffic_uids)
            by_uid = {record["run_uid"]: record for record in status["runs"]}
            for uid in traffic_uids:
                record = by_uid[uid]
                print(
                    f"  run {uid[:8]} ({record['source']}): {record['status']}, {record['n_points']} points "
                    f"{record['error'] or ''}"
                )
            print(f"  counts {status['counts']}, backlog {status['backlog']}")
            ingested = [uid for uid in traffic_uids if by_uid[uid]["status"] == "ingested"]
            checks[f"{len(traffic_uids)} traffic runs ingested"] = len(ingested) == len(traffic_uids)

            print(f"\n3. /suggest in the context of two runs, against each run's measured best {dof}")
            lowest = min(records, key=lambda record: record["offset"])
            highest = max(records, key=lambda record: record["offset"])
            print(f"  offsets differ by {(highest['offset'] - lowest['offset']) * 1e6:.1f} urad")
            for record in (lowest, highest):
                uid = record["run_uids"][0]
                best_position, best_value = measured_best(uid, dof, objective, metrics)
                # Two points: ε-exploration replaces round(0.2 * 2) = 0 of them, so the first is the model's best.
                answer = call(client, "POST", f"/agents/{agent_id}/suggest", json={"n_points": 2, "context": uid})
                top = answer["suggestions"][0][dof]
                close = abs(top - best_position) <= args.tolerance
                print(
                    f"  run {uid[:8]} {feature}={answer['context'][feature] * 1e6:+6.1f}urad: measured best "
                    f"{dof}={best_position * 1e6:+6.1f}urad ({objective}={best_value:.0f}), "
                    f"suggested {top * 1e6:+6.1f}urad -> {'within' if close else 'outside'} {args.tolerance * 1e6:.0f} urad"
                )
                checks[f"top suggestion near the measured best of run {uid[:8]}"] = close

            print(f"\n4. /run 3 iterations of 2 points, starting from the context of run {traffic_uids[-1][:8]}")
            call(
                client,
                "POST",
                f"/agents/{agent_id}/run",
                json={"iterations": 3, "n_points": 2, "context": traffic_uids[-1]},
            )

            def show(record: dict[str, Any]) -> None:
                outcomes = {point["_id"]: point for point in record["points"] or []}
                context = {name: f"{value * 1e6:+.1f}urad" for name, value in (record["context"] or {}).items()}
                print(
                    f"  iteration {record['iteration']}: context of run {str(record['context_uid'])[:8]} {context} "
                    f"-> run {str(record['run_uid'])[:8]}"
                )
                for suggestion in record["suggestions"]:
                    point = outcomes.get(suggestion["_id"], {})
                    measured = point.get(objective, float("nan"))
                    in_run = point.get(feature, float("nan")) * 1e6
                    print(
                        f"    {dof}={suggestion[dof] * 1e6:+6.1f}urad {objective}={measured:.0f} "
                        f"{feature} in run={in_run:+.1f}urad"
                    )

            final, _ = follow(client, agent_id, show)
            print(f"  -> {final['state']}")
            own = call(client, "GET", f"/agents/{agent_id}/history")["iterations"]
            chained = bool(own) and own[0]["context_uid"] == traffic_uids[-1]
            chained = chained and all(own[k]["context_uid"] == own[k - 1]["run_uid"] for k in range(1, len(own)))
            checks["/run completes 3 iterations whose context chains through its own runs"] = (
                final["state"] == "completed" and len(own) == 3 and chained
            )
            status = call(client, "GET", f"/agents/{agent_id}/ingestion")
            offered = {record["run_uid"] for record in status["runs"]}
            checks["the agent's own runs are not offered to its ingestion"] = not offered & {r["run_uid"] for r in own}
            print(f"  ingestion counts {status['counts']}")
    finally:
        run_item(rm, BPlan("mv", CONTEXT_AXIS, 0.0))
        rm.close()

    print("\nchecks:")
    for name, passed in checks.items():
        print(f"  {'OK  ' if passed else 'FAIL'} {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
