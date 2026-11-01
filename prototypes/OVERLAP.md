# Overlap: beamline alignment and passive learning on one bluesky-adaptive core

Two servers were built on `bluesky_adaptive.server`, as specified in `docs/design/server-mvp.md`:

- the **alignment server** (`alignment_server/`) runs Bayesian optimization (Ax) over `dcm_c2.pitch` and
  `m1.pitch`;
- the **passive learning server** (`passive_server/`) trains an MLP surrogate by SGD, conditioned on
  `dcm_c1.pitch` read from each run's baseline. It learns from runs by other users as well as its own.

This file records what the two applications share, what each one had to write, which core changes they
forced, and what bluesky-adaptive should provide.

## Summary

- **The two applications differ only in their catalog entries.** The alignment application is 63 code lines,
  51 of which wrap the library's `AxOptimizer`. The passive application is 494 code lines: 399 for the MLP,
  28 for the context function, 50 for the run ingestor and 17 for wiring. Both share 289 code lines in
  `common/`, mostly the `image-metrics` evaluation.
- **The core is 2,242 code lines**: `server/` 1,521, `runs.py` 231 and `queueserver.py` 490. Both
  applications use all of it except the context and ingestion parts. Those parts are at least 244 code lines
  (11 %), and only the passive application uses them.
- **Neither application forced a core change.** The last core edit was made before the alignment
  application was written. The passive application was built afterwards, strictly against that core. One
  caveat: the plan specified context and ingestion up front, so this result shows that the core's contracts
  held for both applications. It does not show that a core designed for alignment alone would have served
  passive learning.
- **Both servers run against one queueserver and one Tiled server without interfering.** Each server
  routes its own runs to its agents. The passive agent's ingestion rejected all 19 runs of the alignment
  demo, because those runs move an axis its problem does not model.
- **The friction is at the edges of the core**: two names per device, plan annotations, Tiled client
  behavior, and the duplicated per-event mapping in evaluation and ingestion. See
  [Recommendations](#recommendations).

## Method

- **Order.** The core came first: `protocols.py`, `runs.py`, the `queueserver.py` runner and
  `bluesky_adaptive.server`. The alignment application came next, then the passive application. The session
  transcript dates every edit, which gives the timeline in [Core changes](#core-changes-the-applications-forced).
- **Code lines.** These are lines that are not blank, comments or docstrings, counted with `ast` and
  `tokenize`. For JSON, YAML, TOML and shell files, they are the non-blank lines. "Passive-only" core lines
  count only whole symbols that exist for context or ingestion. Context and ingestion branches inside shared
  methods are not counted: by a grep for "context" or "ingestion", there are about 120 more such lines.
- **End-to-end smoke.** The stack is the one in `deploy/`: the XRT BMM simulation IOC, redis, Tiled 0.2.20
  and a queueserver whose worker runs ophyd-async devices. Both servers were started fresh with complete log
  files. The passive demo ran first, then the alignment demo, with the passive agent still subscribed.

## Where each concern lives

| Concern | Alignment | Passive | Lives in | Code lines |
|---|---|---|---|---|
| HTTP endpoints and error mapping | uses | uses | core `server/app.py` | 220, of which 23 for ingestion only |
| Agent registry: creation checks, operations, history | uses | uses | core `server/agents.py` | 492, of which 20 for context only |
| Problem schema and linear expression parser | uses | uses | core `server/problem.py` | 321 |
| Catalog entry types | optimizer, evaluation | also context, ingestion | core `server/catalog.py` | 69, of which 22 passive-only |
| Settings, queueserver connections, plan and device checks | uses | uses | core `server/settings.py`, `server/connections.py` | 38 + 105 |
| Point validation and JSON coercion | uses | uses | core `server/points.py` | 77 |
| Exports and error types | uses | uses | core `server/__init__.py`, `server/errors.py` | 38 + 10 |
| Learning from runs the agent did not submit | — | uses | core `server/ingestion.py` | 151 |
| Plan submission, correlation, watchdog, stop, context chaining | uses | uses | core `queueserver.py` | 490, of which 12 for context only |
| Completed runs from Tiled, routed to the submitting agent | uses | uses | core `runs.py` | 231, of which 8 for subscribers only |
| Context protocols (`ContextAware`, `ContextFunction`) | — | uses | core `protocols.py` | 9 |
| Evaluation: screen metrics per event, mapped to suggestion `_id`s | uses | uses | shared `common/image_metrics.py` | 177: 136 metrics, 41 entry |
| Tiled run access | uses | uses | shared `common/tiled_data.py` | 45 |
| Settings from environment variables | uses | uses | shared `common/settings.py` | 28 |
| Optimizer | `ax-bo`, wraps `AxOptimizer` | `mlp-sgd`, MLP surrogate | application | 51 / 399 |
| Context function | — | `baseline-signals` | application | 28 |
| Run ingestor | — | `bluesky-scans` | application | 50 |
| `create_app` call, imports, logging | uses | uses | application | 12 / 17 |
| Demo problem (`spec.json`) | uses | uses | application | 23 / 14 |
| Demo client | uses | uses | application demos and shared `common/demo.py` | 140 / 142, plus 39 |
| Simulated beamline and traffic | uses | uses | `deploy/` | 313 |

### Line counts

| Layer | Lines | Code lines |
|---|---|---|
| Core: `server/` | 2,718 | 1,521 |
| Core: `runs.py` (new) | 403 | 231 |
| Core: `queueserver.py` (+570 / −278 against v1.0.0) | 813 | 490 |
| Core: `protocols.py` additions | +71 | 9 |
| Shared application code: `common/` | 835 | 289 |
| Alignment application: `app.py`, `spec.json` | 184 | 86 |
| Passive application: `app.py`, `mlp.py`, `spec.json` | 896 | 508 |
| Alignment demo | 215 | 140 |
| Passive demo | 181 | 142 |
| Deployment: `deploy/` | 519 | 313 |
| Library and server tests (new and rewritten) | 2,111 | 1,555 |
| Prototype tests | 342 | 281 |

These are the core parts that only the passive application exercises. All are whole symbols:

| Part | Symbols | Code lines |
|---|---|---|
| Context | `ContextAware`, `ContextFunction`, the runner's `is_contextual` and `_context_for`, `ContextEntry`, `_ContextLookup`, `ManagedAgent._check_context` | 48 |
| Ingestion | `server/ingestion.py`, `RunIngestor`, `IngestionEntry`, `RunRouter.subscribe`/`unsubscribe`, the four ingestion endpoints | 196 |

## Core changes the applications forced

**None.** The session transcript shows no edit to `server/`, `runs.py`, `queueserver.py`, `protocols.py` or
`ax/queueserver_agent.py` after the alignment application was written. The test subagents changed only test
files and reported no library bugs.

| Step | Time (UTC) |
|---|---|
| Last core edit: a docstring caveat on `TiledRunFeed.state` | 18:54 |
| Alignment `app.py` written | 18:56 |
| Alignment server started, first alignment demo | 18:58 |
| Passive `app.py` and `spec.json` written | 19:02 |
| Passive server started | 19:05 |

Before either application existed, the core was already shaped by what the applications would need:

- **`OptimizerEntry.no_data_errors`.** Each backend declares its own "no best point yet" exceptions, so the
  core can map them to 409 without knowing Ax. Only `ax-bo` sets it, to
  `(AssertionError, DataRequiredError, UnsupportedError)`. The MLP raises `ValueError`, which the core maps
  anyway.
- **DOF `type` defaults to `"range"`.** Clients do not have to spell it out.
- **The linear parser's hint for a binary minus without spaces.** Before the hint, the parser said only
  `unknown name 'dcm_c2-pitch-m1-pitch'`.
- **A caveat in the `TiledRunFeed.state` docstring.** It came from the Tiled feed integration test; see
  friction point 3.

The smoke runs forced two changes, both outside the core:

1. **The alignment demo now moves its axes back to 0 at the end.** Before this, it left `m1.pitch` at
   −54.4 µrad. The next passive demo then measured a collapsed beam: its traffic scan at `dcm_c1-pitch`
   −14.5 µrad peaked at a `diag2_total` of 18, against 43k for the same scan with `m1.pitch` at 0. All six of
   its checks still passed. See friction point 9.
1. **The alignment demo's stop check now counts only plans queued after `/stop`.** Every agent of a server
   submits as the same queueserver user, so the old check counted 18 plans of earlier agents as unrecorded.

## Friction points

### 1. Two names per device

- The queueserver takes device paths (`dcm_c2.pitch`). Everything recorded uses the ophyd name
  (`dcm_c2-pitch`): data keys, suggestion keys, Tiled columns and the start document's `motors`.
- A DOF spec therefore carries both: `{"name": "dcm_c2-pitch", "actuator": "dcm_c2.pitch"}`. The core checks
  the paths against the queueserver's allowed devices (`AgentRegistry._check_devices`).
- Sensors are given only as paths, so the application code converts them with `sensor.replace(".", "-")`, in
  `common/image_metrics.py` (`build_image_metrics`) and in `passive_server/app.py`
  (`BlueskyScansIngestor.__init__`). That rule holds only for ophyd-async's default child naming.
- Image data keys depend on the detector class and the ophyd-async version. The deployment's screens record
  `diag2_image`, while the ophyd-async 0.16 `SimBlobDetector` used to develop the evaluation records `diag2`.
  The evaluation therefore matches both forms (`_sensor_matches`).

### 2. Plan annotations

- The queueserver converts device-name strings to devices only for parameters without a type annotation, or
  with a custom `parameter_annotation_decorator` annotation that lists the devices
  (`bluesky_queueserver/manager/profile_ops.py:956-958`).
- `bluesky_adaptive.plans.default_acquire` annotates `actuators: Sequence[Actuator]` (`plans.py:46-54`), so
  through the queueserver it would not receive devices. This was not exercised here.
- The deployment therefore defines its own unannotated `adaptive_acquire` (`deploy/qs_startup.py`).
- The core's plan check (`connections.acquisition_contract_problems`) looks only at parameter names and kinds.
  An annotated acquisition plan would pass agent creation.

### 3. Tiled client behavior

Each of these needed code in `TiledRunFeed` (`runs.py`) or the deployment:

- **Callbacks are held by weak reference** (`tiled/client/stream.py:231`, `weakref.WeakMethod`). A lambda or
  local function is dropped silently, so the feed registers bound methods of itself.
- **Callbacks run through `executor.submit`** (`stream.py:172`), and their exceptions vanish with the
  future. The feed catches and logs inside every callback.
- **`start_in_thread` waits for the connection with no timeout** (`stream.py:509`). The feed instead runs
  `start()` in its own thread with a reconnect loop. Each attempt gets a fresh executor, because a
  subscription shuts its executor down when it disconnects.
- **bluesky-tiled-plugins' `subscribe_to_streams` never reports a run's end.** It registers only
  `child_created` and `new_data` (`bluesky_tiled_plugins/streaming.py:233, 253`). Run completion needs
  Tiled's own container subscription: `child_metadata_updated`, whose metadata holds both `start` and `stop`.
- **There is no reliable "connected" state.** Tiled sets its private `_connected_event` once and never clears
  it. `TiledRunFeed.state` therefore reports `"subscribed"` before the websocket connects, and runs that stop
  in that window are missed unless `replay=True`. The Tiled feed integration test needs `replay=True`.
- **Server side** (Tiled 0.2.20):
  - `tiled serve catalog --cache memory` without `--temp` raises `UnboundLocalError: StreamingCacheConfig`.
  - With the API key only in the config file, Tiled prints a random key at startup.
  - The in-memory streaming cache needs a single server process, and replay does not survive a restart.
- **The queueserver reports a plan finished before Tiled may have its stop document.** The alignment demo's
  client-side acquisition polls Tiled for the run.

Once the stop update arrived, data was readable after 0.57-1.17 s, and the first read succeeded every time.

### 4. Ax expression rules

Probe of Ax 1.3.1 through `Client.configure_experiment` and `configure_optimization`:

| Expression | Ax 1.3.1 |
|---|---|
| `dcm_c2-pitch + m1-pitch <= 0.5` | accepted |
| `dcm_c2-pitch-m1-pitch <= 0.5` | `ValueError: dcm_c2-pitch-m1-pitch does not exist in search space.` |
| `x + y < 0.5` | `UserInputError: Expected an inequality, found x + y < 0.5` |
| `E + y <= 0.5`; objective `E`; `pi >= 1`; objective `S` | `ValueError: … conflict with sympy's built-in names: E` (`pi`, `S`) |

- **The core parser enforces the backend-neutral part at `POST /agents`** (`server/problem.py`). Binary
  operators need whitespace around them, because names may contain `-` and `.`. Only `<=` and `>=` are
  allowed, and every name must exist. Its errors name the problem, for example
  `unsupported comparison '<'; use '<=' or '>='` or
  `unknown name 'dcm_c2-pitch-m1-pitch' … (binary '-' must be surrounded by whitespace)`.
- **SymPy names are an Ax rule, so they fail in the `ax-bo` build** with Ax's message, as a 422. A DOF named
  `S` that appears in no expression builds fine.
- **One parsed form serves both backends.** The alignment application renders
  `LinearConstraint.canonical()` for Ax, and the MLP evaluates the parsed constraints directly.

### 5. Best points carry a variance labeled as SEM

- Ax reports each metric of a best point as `(mean, variance)`:
  - model predictions return `covariance[metric][metric]` (`ax/service/utils/best_point_mixin.py:550-555`);
  - raw observations return the SEM squared (`ax/service/utils/best_point.py:259`: `v[1] * v[1]  # v[1] is sem`).
- bluesky-adaptive documents these values as "(value, sem) tuples" (`ax/optimizer.py:189`, `ax/agent.py:136`).
- The server passes outcome values through unchanged. Clients see `[mean, variance]` from Ax and plain
  numbers from the MLP, so the alignment demo has to branch on the type.

### 6. `accepts` needs the params and the problem

- **The design doc puts `accepts(start_doc)` on the entry.** The passive ingestor needs:

  - the entry's `plan_names`;
  - the problem's DOF names, which must match the run's `motors` exactly;
  - the problem's sensors, which must be among the run's `detectors`.

  `RunIngestor.accepts` is therefore a method of the built ingestor (`server/catalog.py`).

- **`accepts` returns a bool, so a rejection carries no reason.** All 19 rejected records in the smoke have
  `error: None`. Classifying them meant reading their start documents back from Tiled.

- **Ingestion outputs cannot always be declared from the params alone.** The outputs of `bluesky-scans`
  depend on the agent's evaluation, so `IngestionEntry.outputs` is optional and the check moves to build time.

### 7. Evaluation and ingestion are one computation

- Both call `event_outcomes_from_run(run, metrics)` (`common/image_metrics.py`), which produces one row per
  event. They differ only in how each row is labeled:
  - the evaluation maps row *i* to `start["blop_suggestions"][i]["_id"]` (`ImageMetricsEvaluation`);
  - the ingestor maps row *i* to the DOF readbacks of event *i* (`BlueskyScansIngestor.__call__`).
- `build_bluesky_scans` reuses the agent's evaluation params when its own `metrics` is unset.
- The per-event computation is 136 code lines, the evaluation adapter 41 and the ingestion adapter 50.

### 8. Context entries

- **`ContextEntry.features(params)` names the features before any run is seen.** The optimizer's input size
  depends on them.
- **`baseline-signals` is 28 lines with nothing beamline-specific.** It reads the run's `baseline` stream.
  That stream holds two readings per run, at start and end, hence `reduce: first | mean`.
- **The HTTP API takes run UIDs, and the history records the features** of every iteration.
- **A contextual agent requires a context.** `/suggest` without one answers 422: "agent 2c6784e0 is
  contextual; give 'context', the uid of the run whose conditions to suggest for".

### 9. Interleaving safety lives in `accepts`

- **Claims are per process.** Each server routes its own runs to its agents, and offers every other run to
  ingestion as unclaimed.
- **The motors check protects the passive agent.** `set(start["motors"]) == DOF names` rejected all 19
  alignment runs, whose motors are `dcm_c2-pitch` and `m1-pitch`.
- **An axis left off nominal by an earlier run still passes `accepts`,** because `motors` lists only the axes
  the run itself moved. This happened in this session: the traffic runs' baselines read
  `m1-pitch: [-54.4, -54.4]` µrad. The baseline records every axis, but `accepts` sees only the start
  document. The demos now restore their axes, but the core cannot detect the condition.

### 10. Other friction

- **Queueserver autostart.**
  - The old `QueueserverClient` enabled autostart as a side effect of construction. It is now a deployment
    setting: `QueueserverSettings.autostart`, or `deploy/open_env.py`.
  - The deployment also needs `queue_mode_set(ignore_failures=True)`. Otherwise a failed plan goes back to the
    front of the queue and turns autostart off.
  - `queue_start()` can race autostart and fail with `RequestFailedError: RE Manager is busy`.
- **A plan cannot be removed once it runs.** On `/stop` with the plan already running, `item_remove` fails,
  so the runner goes to `stopping` and ingests that run (2.2 s here).
- **Ax has three ways of saying "no best point yet":**
  - `UnsupportedError` when there are no trials;
  - `AssertionError` while trials are pending without data;
  - `AssertionError` when no point is feasible within the 95 % confidence interval.
- **`scan` takes `num` as a keyword-only argument.**
- **Near-empty screens give noisy centroids.** 47 stray pixels (total 0.0045, against 42k at the optimum)
  give a centroid of noise at (93.9, 85.9). A fully empty image gives the −1 penalty. Both are infeasible
  under the [116, 140] bounds; the screen's `threshold` parameter removes the noise.

## End-to-end results

Both demos exited 0.

| Alignment check | Result |
|---|---|
| 15 iterations of 1 point | completed in 49.5 s, about 3.3 s per iteration |
| Best `diag2_total` ≥ 0.9 × survey maximum (38,694) | 42,239.7 at `dcm_c2-pitch` −1.9 µrad, `m1-pitch` +3.3 µrad, centroid (125.4, 129.6) |
| `/suggest`, `/submit-suggestions`, `/ingest` | one 2-point run (the suggestion and the nominal position, 38,523.8); the client-side point was ingested |
| `/stop` while iteration 2 runs | `stopping`, then `stopped` 2.2 s later; one in-flight run ingested; nothing queued after the stop |

| Passive check | Result |
|---|---|
| `/suggest` without context | 422 |
| Ingestion of 10 traffic runs, 11 points each | all ingested |
| Top suggestion within 15 µrad of the run's measured best (offsets 35.1 µrad apart) | `dcm_c1-pitch` −14.5 µrad: best +20.0 µrad (43,235), suggested +17.1 µrad. `dcm_c1-pitch` +20.7 µrad: best −40.0 µrad (17,047), suggested −38.3 µrad |
| `/run` 3 × 2 from the last traffic run's context (+5.0 µrad) | completed; suggested −13.9 and −3.9 µrad, measured 34.7k and 31.3k; each iteration's context is the agent's previous run |
| Own runs not offered to its ingestion | ingestion counts unchanged |

After both demos:

- **Passive ingestion** counted 10 runs ingested (110 points) and 19 rejected: 18 alignment-agent runs and 1
  client-side `adaptive_acquire`, all rejected on their motors.
- **Both run feeds** were `subscribed`, with 32 runs routed each.
- **The complete server logs** (381 and 310 lines) contain no traceback, error or warning.
- **The tests pass:** 399 unit tests, 3 integration tests and 19 prototype tests.

## Recommendations

What bluesky-adaptive should provide, in order of value:

1. **Keep the core as built.** That is the `runs` feeds and router, the queueserver runner, and
   `bluesky_adaptive.server` with its catalogs, problem schema and parser, registry, ingestion manager and
   endpoints. Both applications used it unchanged, and an alignment server is a 63-line catalog on top of the
   library's `AxOptimizer`.
1. **Keep context and ingestion in the core.** Only the passive application exercised these 244 lines, but
   nothing in them is specific to the MLP or the beamline.
1. **Provide a "run to per-event rows" abstraction with two adapters**: rows labeled by suggestion `_id` for
   evaluation, and rows labeled by DOF readbacks for ingestion. Every evaluation would then also be an
   ingestor (friction point 7).
1. **Ship the beamline-independent entries the passive application wrote**: the `baseline-signals` context
   and a `bluesky-scans` ingestor parameterized by the per-event function (friction points 7 and 8).
1. **Ship a queueserver-ready acquisition plan,** either unannotated or annotated with
   `parameter_annotation_decorator`. Have the plan check reject acquisition plans whose `actuators` or
   `sensors` carry type annotations (friction point 2).
1. **Map device paths to data keys in one place in the core,** instead of `replace(".", "-")` in application
   code (friction point 1).
1. **Fix the "(value, sem)" docstrings to say variance,** and define the outcome shape that
   `GET /best-points` returns (friction point 5).
1. **Record why a run was rejected:** let `accepts` return a reason. Consider a baseline check of the axes a
   problem does not model against their nominal values (friction points 6 and 9).
1. **Build what the design doc still lists as out of scope here:** persistence and restart, OIDC,
   entry-version pinning, DOF bounds changes and controls for interleaving agents.
