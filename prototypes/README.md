# Adaptive server prototypes

Two applications built on `bluesky_adaptive.server` and run against the simulated TST beamline (the XRT BMM
simulation in `tst-sim-tools/`). They measure how much of an adaptive server bluesky-adaptive can provide, and how
much each application still has to write. [OVERLAP.md](OVERLAP.md) has the results.

| Path | Contents |
|---|---|
| `alignment_server/` | Beamline alignment. Bayesian optimization (Ax) maximizes the Diag2 flux over `dcm_c2.pitch` and `m1.pitch` while keeping the beam near the centre of the screen. Catalog: the `ax-bo` optimizer and the shared `image-metrics` evaluation. |
| `passive_server/` | Passive learning. A small MLP trained by SGD learns the Diag2 flux as a function of `dcm_c2.pitch`, conditioned on `dcm_c1.pitch`, from its own runs and from runs by other users. Catalog: the `mlp-sgd` optimizer, `image-metrics`, the `baseline-signals` context and the `bluesky-scans` ingestion. |
| `common/` | Code both applications use: the `image-metrics` evaluation, Tiled helpers, settings and demo helpers. |
| `deploy/` | The simulated beamline: an EPICS IOC, redis, Tiled and a queueserver. It also has `traffic.py`, which plays the other users. See [deploy/README.md](deploy/README.md). |
| `tests/` | Unit tests of the MLP and the image metrics. |
| `OVERLAP.md` | What the two applications share, what each had to write, and what bluesky-adaptive should provide. |

## Run

All commands run from the repository root.

1. Start the beamline in the order given in [deploy/README.md](deploy/README.md): the IOC, redis, Tiled, the
   queueserver, then `open_env.py`.

1. Start each server in its own terminal:

   ```bash
   pixi run -e integration uvicorn prototypes.alignment_server.app:app --port 8100
   pixi run -e integration uvicorn prototypes.passive_server.app:app --port 8200
   ```

   Each serves its OpenAPI documentation at `/docs`, for example <http://localhost:8100/docs>.

1. Run the demos one at a time. Each prints what it does and exits with status 0 only when all its checks pass.

   ```bash
   pixi run -e integration python -m prototypes.passive_server.demo
   pixi run -e integration python -m prototypes.alignment_server.demo
   ```

### Passive demo

The passive demo takes about 4 minutes. Options: `--rounds`, `--points`, `--seed`, `--tolerance`.

1. Create a contextual agent from `passive_server/spec.json` and subscribe it to every completed run. Check that
   `/suggest` without a context is rejected with 422.
1. Play other users: 10 scans of `dcm_c2.pitch`, 11 points each, at random `dcm_c1.pitch` offsets.
1. Check that the agent ingested every one of those runs.
1. Suggest in the context of the runs with the lowest and the highest offset. Check that each top suggestion is
   within 15 µrad of that run's measured best `dcm_c2.pitch`.
1. Run the agent for 3 iterations of 2 points, starting from the context of the last traffic run. Check that
   each later iteration uses its own previous run as context, and that the agent's own runs are never offered to
   its ingestion.

### Alignment demo

The alignment demo takes about 80 seconds. Options: `--iterations`, `--stop-iterations`.

1. Create an agent from `alignment_server/spec.json` and run 15 iterations of 1 point.
1. Print the best points.
1. Ask `/suggest` for 2 points. Acquire the first with `/submit-suggestions`, together with the nominal position.
   Acquire the second on the client side and report it with `/ingest`.
1. Start a 10-iteration loop and `/stop` it while a plan is in flight. Check that exactly the in-flight run is
   ingested and that the agent queues nothing after the stop.

Both demos move the axes they changed back to 0 at the end, also on failure. An axis left off 0 is a condition
that other users' runs do not record as moved. If the passive agent is still subscribed while the alignment demo
runs, it rejects every alignment run, because those runs move `m1-pitch` and its problem does not model it.

## Settings

Both servers read their settings from environment variables (`prototypes/common/settings.py`):

| Variable | Default | Meaning |
|---|---|---|
| `BA_QS_CONTROL` | `tcp://localhost:60615` | RE Manager control address |
| `BA_QS_INFO` | `tcp://localhost:60625` | RE Manager info address |
| `TILED_URI` | `http://localhost:8000` | Tiled server that the `TiledWriter` writes to |
| `TILED_API_KEY` | `secret` | Tiled API key |
| `BA_STORAGE_DIR` | `$PROTOTYPE_ROOT/servers` | Parent directory of each server's storage (optimizer checkpoints) |
| `PROTOTYPE_ROOT` | `/tmp/bluesky-adaptive-prototypes` | Deployment state root |
| `BA_LOG_LEVEL` | `INFO` | Log level of the servers |

The alignment server submits plans as the queueserver user `bluesky-adaptive-alignment`, and the passive server
as `bluesky-adaptive-passive`.

## Use the API directly

```bash
curl -s -X POST localhost:8100/api/v1/agents -H 'content-type: application/json' \
  -d @prototypes/alignment_server/spec.json
curl -s -X POST localhost:8100/api/v1/agents/<id>/run -H 'content-type: application/json' -d '{"iterations": 5}'
curl -s localhost:8100/api/v1/agents/<id>/history
```

The API reference (`docs/source/reference/server.rst`) describes every endpoint.

## Tests

```bash
pixi run -e integration pytest prototypes/tests
```
