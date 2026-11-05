# Reusable server components for bluesky-adaptive

Provide an optional `server` extra containing mountable FastAPI routers and a small optimization manager. Extract the working service in stages.

An **agent** is saved configuration. An **optimization** is one execution session with its own configuration snapshot, live optimizer, history, and result. Starting a new session starts fresh; continuing previous learning is explicit.

| Component | Recommendation |
| --- | --- |
| Agent routes | Upstream config CRUD, validation, and starting optimizations. Include suggest/acquire/ingest when the client needs them. |
| Optimization routes | Upstream list/detail, status, trials, run references, and stop. |
| WebSocket route | Add optional snapshots and lifecycle events after the basic HTTP API. |
| Health and capabilities | Supply basic routes; applications provide dependency checks and registered capabilities. |
| Analysis-method catalog | Supply the catalog mechanism; keep Ax-specific choices in the Ax backend. |
| Detector objective catalog | Keep application-owned. |
| Beam profiles, baseline views, motor history | Keep in the beamline service initially. |
| Alignment handoff and queue scheduling | Keep application-owned, including queue restart policy and time estimates. |

**Extract the manager with the routers.** It builds agents, admits operations, starts and stops execution, records results, and exposes status. Preserve the existing one-active-optimization policy initially. On restart, mark unfinished sessions interrupted; do not resume hardware work automatically.

**Fix the shared runner first:**

- Add an iteration callback after successful ingestion, eliminating the need to poll Ax for completed trials. Keep a separate evaluation-result callback when observations must be saved before ingestion.
- Handle correlation routing, rejected submissions, acquisition failures without stop documents, graceful stopping, and listener cleanup in the QueueServer integration.
- Serialize optimizer access across the runner, manual endpoints, and model inspection.
- Expose public Ax configuration for supported generation strategies and acquisition functions.

**Keep three application dependencies:** an agent factory, an optimization repository, and any beamline-specific routers. Evaluator registration can initially live inside the factory. FastAPI wiring and beamline policy remain in the application.

Offer the existing Tiled repository and detector evaluators as optional adapters. Keep raw-data loading separate from optimization-record storage, even when both use Tiled. Define only the repository operations the manager uses; another storage backend is unnecessary initially.

Before extraction, remove float-only DOF assumptions from trial storage. Expose resume only when a backend supplies suitable restoration behavior: replaying observations into Ax is not universal restoration. Automatic acquisition retries remain application policy.

**Implementation order:** runner fixes → manager and basic routers → optional Tiled adapters and WebSocket support. Keep the beamline service as the integration example.

