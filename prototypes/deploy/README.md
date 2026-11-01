# Prototype deployment: TST XRT BMM simulation

Local stack for the bluesky-adaptive server prototypes: a caproto XRT IOC, redis, a
Tiled server with websocket streaming, and a bluesky-queueserver RE Manager whose
worker runs ophyd-async devices from `tst-sim-tools/` and writes every run to Tiled.

All commands run from the repository root. `tst-sim-tools/` must be a checkout of
[NSLS2/tst-sim-tools](https://github.com/NSLS2/tst-sim-tools) with its `ioc` and `qs`
pixi environments installed, and including the `TST_DATA_DIR` hook in
`src/tst_sim_tools/startup/devices.py`. Redis comes from this directory's own pixi
workspace:

```bash
pixi install --manifest-path prototypes/deploy/pixi.toml
```

## Start order

Each script runs its service in the foreground (`exec`); use one terminal per service.

| Order | Command | Environment | Port(s) | Ready when the log shows |
|---|---|---|---|---|
| 1 | `prototypes/deploy/start-ioc.sh` | tst `ioc` | CA 5064 (TCP/UDP, 127.0.0.1) | `Server startup complete.` |
| 2 | `prototypes/deploy/start-redis.sh` | `prototypes/deploy` | 6379 | `Ready to accept connections` |
| 3 | `prototypes/deploy/start-tiled.sh` | repo `integration` | 8000 | `Uvicorn running on http://127.0.0.1:8000` |
| 4 | `prototypes/deploy/start-qs.sh` | tst `qs` | ZMQ 60615 (control), 60625 (info) | `ZeroMQ server is waiting on tcp://127.0.0.1:60615` |
| 5 | `pixi run -e integration python prototypes/deploy/open_env.py` | repo `integration` (or tst `qs`) | — | prints plans and devices, exits 0 |

`open_env.py` enables the console monitor, opens the RE Worker environment (about 5 s),
and prints the worker console and exits 1 if it fails. It then sets
`queue_mode_set(ignore_failures=True)` and `queue_autostart(True)`, so queued plans start
immediately and one failed plan does not stall the queue for other clients. Finally it
lists the allowed plans and the device paths with their movable/readable/flyable flags.
Pass `--all-devices` to list every nested component.

Plans exposed by `qs_startup.py`:

- `adaptive_acquire(suggestions, actuators, sensors, md)`, the acquisition contract of the adaptive servers;
- `count`, `scan`, `list_scan`, `rel_scan`, `grid_scan`;
- `mv`.

Devices are listed to unlimited depth. Movable paths include `m1.pitch`, `dcm_c1.pitch`
and `dcm_c2.pitch`; the screens `diag1`, `diag2`, `nano_bpm`, `xas_det` and `xrd_det`
are readable and flyable.

## Traffic ("other users")

```bash
pixi run -e integration python -m prototypes.deploy.traffic --rounds 10 --seed 0 --points 11 [--mode list] [--no-restore]
```

Each round moves `dcm_c1.pitch` to a random offset in ±30 µrad, then scans
`dcm_c2.pitch` over ±100 µrad with `diag2`. The offset appears in the run's `baseline`
stream as `dcm_c1-pitch`. At the end `dcm_c1.pitch` returns to 0 unless `--no-restore`
is given. From Python, call `prototypes.deploy.traffic.run_traffic(rm, rounds, seed, points, ...)`.

## Environment variables

| Variable | Default | Used by |
|---|---|---|
| `PROTOTYPE_ROOT` | `/tmp/bluesky-adaptive-prototypes` | all scripts: state root |
| `TST_DATA_DIR` | `$PROTOTYPE_ROOT/data` | qs (detector HDF5 path provider), tiled (`readable_storage`) |
| `TILED_URI` | `http://localhost:8000` | qs (TiledWriter); also read by the prototype servers |
| `TILED_API_KEY` | `secret` | tiled (single-user key), qs (TiledWriter) |
| `TILED_PORT` | `8000` | tiled |
| `REDIS_PORT` | `6379` | redis, qs |
| `TST_SIM_TOOLS` | `<repo>/tst-sim-tools` | ioc, qs |
| `EPICS_CA_AUTO_ADDR_LIST` / `EPICS_CA_ADDR_LIST` | `NO` / `127.0.0.1` | ioc, qs |
| `EPICS_CAS_INTF_ADDR_LIST` | `127.0.0.1` | ioc |

State under `$PROTOTYPE_ROOT`:

| Path | Contents |
|---|---|
| `data/YYYY/MM/DD/*.h5` | detector frames written by the IOC |
| `tiled/` | `config.yml`, generated at each start; `catalog.db`; `data/`; `tabular.duckdb` |
| `qs/` | `user_group_permissions.yaml`, copied from this directory; `existing_plans_and_devices.yaml`, regenerated when the environment opens |
| `redis/` | redis working directory, no persistence |

Delete `$PROTOTYPE_ROOT` to reset everything.

## Reading runs from Tiled

```python
from tiled.client import from_uri

client = from_uri("http://localhost:8000", api_key="secret")
run = client[uid]
run.metadata["start"]["blop_correlation_uid"]  # adaptive_acquire md
run["primary"]["diag2_image"].read()  # (N, 1, 256, 256) float64, external HDF5
run["primary"]["dcm_c2-pitch"].read()  # (N,) actuator readback
run["baseline"]["dcm_c1-pitch"].read()  # (2,) start/end baseline readings
```

Image keys are `<screen>_image`. Signal keys are ophyd names (`dcm_c2-pitch`), while
queueserver arguments use device paths (`dcm_c2.pitch`).

## Stopping

Press Ctrl-C in each terminal, in reverse order: qs, tiled, redis, ioc. Close the RE
Worker environment first if you want a clean worker shutdown:
`REManagerAPI().environment_close()`. Each `pixi run` wrapper forwards the signal to
its child, so check that nothing is left behind:

```bash
pgrep -af 'xrt_xml_ioc|redis-server|tiled serve|start-re-manager'
```

## Pitfalls found

- **`tiled serve catalog --cache memory` fails without `--temp` in tiled 0.2.20.** It raises
  `UnboundLocalError: StreamingCacheConfig`. `start-tiled.sh` therefore generates a
  config file with `streaming_cache: {uri: memory}` and uses `tiled serve config`. The
  in-memory cache requires a single server process.
- **Tiled prints a random API key** at startup when `single_user_api_key` is set only in
  the config file, which is misleading. The script passes `--api-key` on the command line.
- **`scan` takes `num` as a keyword-only argument.** Submit
  `BPlan("scan", ["diag2"], "dcm_c2.pitch", lo, hi, num=5)`.
- **Plan parameters must stay unannotated.** The queueserver converts device-path strings
  (`dcm_c2.pitch`) to devices only for unannotated parameters, so `adaptive_acquire` has
  no type annotations.
- **`adaptive_acquire` stages detectors inside the run**, so the HDF5 files are closed
  before the stop document. Built-in plans stage outside the run. In practice both are
  readable from another client within about 1 s of Tiled's stop update; see the
  measurements below. `HDF5_USE_FILE_LOCKING=FALSE` was not needed.
- **`queue_start()` can race with autostart.** It raises `RequestFailedError: RE Manager is busy` when the queue starts between `status()` and the call. `traffic.run_item`
  ignores that error.
- **The console monitor** only captures lines published after `console_monitor.enable()`,
  so `open_env.py` enables it before opening the environment.
- **Baseline streams have two readings per run**, one at start and one at end. Use the
  first one as the run's condition.
- `pixi.lock` is ignored by the root `.gitignore`, so the deploy workspace re-solves on
  the first install.

## Measurements (this machine)

| Run | Submit → stop update | Stop update → primary readable from another client |
|---|---|---|
| `adaptive_acquire`, 2 points, `diag2` | 4.9–5.8 s | 0.94–1.17 s, first attempt |
| `scan`, 5 points, `diag2` | 9.6 s | 0.57 s, first attempt |

Both runs needed only one read attempt; the time is the HTTP read of the image stack
and the scalar column. One XRT trace takes about 1.8 s per event.
