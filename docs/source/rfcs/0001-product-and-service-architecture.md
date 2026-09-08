# RFC 0001: Bluesky Adaptive Product and Service Architecture

| Field | Value |
| --- | --- |
| Status | Proposed |
| Created | 2026-09-08 |
| Scope | Rebuild of `bluesky-adaptive`, migration of Blop, and optional optimizer service |
| Audience | Bluesky Adaptive maintainers, beamline developers, and service operators |

## Summary

Bluesky Adaptive connects an ask/tell optimizer to Bluesky acquisition. It can do that in-process or across HTTP, and it can optionally close the loop through QueueServer.

The project will move the current Blop implementation into `bluesky-adaptive` and retire the existing `bluesky-adaptive` implementation. Blop's optimizer, acquisition, evaluation, and Bluesky-plan protocols become the library foundation. The existing distributed-agent hierarchy, adjudicators, and custom variable-oriented server are not retained.

The optional service hosts one trusted, configured optimizer. Its primary interface exposes `suggest` and `ingest` operations. When configured with a QueueServer runner, document source, and evaluation function, it may also execute a bounded optimization operation by repeatedly suggesting points, submitting an approved acquisition plan, evaluating the resulting Bluesky run, and ingesting the outcome.

The unique product responsibilities are deliberately narrow:

1. expose the optimizer protocol over HTTP;
2. connect that protocol to QueueServer acquisition when requested; and
3. preserve identity and recover state across optimizer suggestions, QueueServer items, Bluesky runs, and evaluated outcomes.

QueueServer remains authoritative for hardware execution. Tiled remains authoritative for experimental data. Optimizer backends remain authoritative for model and trial state.

## Motivation

Blop already provides the useful algorithm-facing decomposition:

```text
suggest -> acquire -> evaluate -> ingest
```

It also supports local RunEngine execution, Ax and Xopt backends, custom acquisition plans, manual observations, checkpointing, and experimental QueueServer operation.

The existing `bluesky-adaptive` service instead exposes arbitrary registered variables and methods through a custom worker process, pipe-based JSON-RPC transport, FastAPI application, and optional IOC. Its service API is not aligned with the optimization contracts being moved from Blop. Retaining that architecture would preserve complexity without preserving a useful product boundary.

A service is still valuable for several reasons:

- optimizer dependencies and mutable state can live outside the RunEngine process;
- an external client can use an optimizer through a small suggest/ingest protocol;
- the same service can optionally drive QueueServer without changing optimizer semantics;
- model state can survive client and RunEngine restarts; and
- an optimizer failure does not need to affect normal beamline operation.

The service must not grow into a general autonomous-laboratory platform, workflow engine, data catalog, QueueServer replacement, or remote-object server.

## Product statement

> Bluesky Adaptive provides common protocols and Bluesky plans for adaptive acquisition. Its optional service makes one configured optimizer remotely accessible through suggest/ingest operations. When configured with QueueServer and an evaluation function, that service can execute a bounded closed loop, correlating each suggestion with the QueueServer item, Bluesky run, and observed result, and recovering safely after interruption.

## Goals

- Move Blop's supported behavior into the `bluesky-adaptive` package.
- Preserve direct, in-process use through normal Python and Bluesky plans.
- Expose a backend-neutral optimizer protocol over HTTP.
- Allow direct HTTP clients to suggest points and ingest outcomes without using QueueServer.
- Optionally execute a bounded optimization loop through QueueServer.
- Support Ax first without making the service Ax-specific.
- Preserve stable identities across optimizer, queue, run, and evaluation boundaries.
- Make duplicate messages and service restarts recoverable through idempotent processing and reconciliation.
- Keep all hardware changes inside approved Bluesky plans.
- Reuse QueueServer, Bluesky HTTP Server, document transports, Tiled, and backend-native persistence.
- Keep the service independently deployable from the beamline control API.

## Non-goals

- Multi-tenant optimizer hosting.
- Runtime creation or upload of optimizer definitions.
- A campaign-management system.
- More than one active automatic operation per service.
- Concurrent mutation of an optimizer by direct clients and the automatic runner.
- Generic workflow scheduling or multi-instrument orchestration.
- A new authentication or authorization system.
- A new scientific data API or catalog.
- A generic experiment-tracking database.
- An operator GUI.
- Arbitrary remote Python execution or generic method/property registration.
- Direct PV writes or replacement of control-system interlocks.
- Exactly-once distributed transactions across QueueServer, document transport, Tiled, and optimizer storage.
- Sub-millisecond or control-system-level feedback.

## Terminology

### Optimizer

A stateful object implementing the required Blop optimizer protocol:

```python
suggestions = optimizer.suggest(n)
optimizer.ingest(outcomes)
```

Optional capability protocols may support failure registration, manual suggestion registration, checkpointing, stopping criteria, and best-point queries.

### Suggestion

A parameterization proposed by the optimizer with a stable suggestion UID. The HTTP representation keeps identity separate from parameter values:

```json
{
  "uid": "suggestion-uid",
  "parameters": {
    "kbv_pitch": 0.001,
    "kbh_pitch": -0.002
  }
}
```

The service adapter may translate this representation to Blop's internal mapping and `_id` convention.

### Observation

The evaluated outcome associated with one suggestion:

```json
{
  "suggestion_uid": "suggestion-uid",
  "values": {
    "fwhm": 14.2,
    "intensity": 18200.0
  }
}
```

### Operation

One bounded invocation of the optional automatic QueueServer loop. Repeated operations continue from the same optimizer state. An operation is an asynchronous service job, not a scientific campaign or optimizer owner.

### Acquisition

Execution of an approved Bluesky plan for one suggestion or batch. An acquisition is identified by its QueueServer item UID and resulting Bluesky run UID or UIDs.

### Evaluation function

A trusted callable that transforms a completed acquisition and its suggestions into observations suitable for `optimizer.ingest`.

## System boundaries

```text
                         Direct HTTP client
                         suggest / ingest
                                |
                                v
                       Optimizer service
                    +----------------------+
                    | Optimizer            |
                    | Optional runner      |
                    | Recovery cursor      |
                    +----------+-----------+
                               |
                        QueueServer API
                               |
                       approved BPlan only
                               |
                         RunEngine worker
                               |
                        Bluesky documents
                          /           \
                 document source     Tiled
                          \           /
                           evaluation
                               |
                             ingest
```

The service is independently deployable. A failure or upgrade of the optimizer service must not prevent ordinary QueueServer or RunEngine use.

## Core library architecture

The rebuilt package retains Blop's composition model:

- `Optimizer` suggests and ingests points.
- `AcquisitionPlan` performs experiment-specific control and acquisition.
- `EvaluationFunction` transforms acquired data into optimizer outcomes.
- `OptimizationProblem` composes those pieces for in-process execution.
- Bluesky plans implement local optimization loops.
- backend adapters implement Ax, Xopt, or future optimizers.

The service layer depends on these protocols. The core library does not depend on FastAPI, QueueServer, Kafka, or Tiled.

## Service construction

A deployment constructs the service from trusted Python configuration:

```python
optimizer = AxOptimizer(...)

runner = QueueServerRunner(
    qserver=qserver_client,
    document_source=document_source,
    acquisition=acquisition_plan_factory,
    evaluate=evaluation_function,
)

app = build_app(optimizer=optimizer, runner=runner)
```

`runner` is optional. Without it, the service exposes only remote optimizer operations.

The service API does not accept import paths, arbitrary plan names, Python source, optimizer definitions, device objects, credentials, or evaluator code. Changes to those objects require a reviewed deployment change.

## HTTP API

The exact URL layout may change during implementation, but the behavioral surface is constrained by this RFC.

### Always available

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/health` | Process liveness; no downstream mutation. |
| `GET` | `/state` | Optimizer identity, revision, capabilities, pending suggestion IDs, checkpoint identity, and active operation summary. |
| `POST` | `/suggest` | Generate one or more suggestions. |
| `POST` | `/ingest` | Ingest observations associated with suggestion UIDs. |
| `POST` | `/fail` | Register failed suggestions when supported. |
| `GET` | `/best` | Return observed best point or Pareto set when supported. |
| `POST` | `/checkpoint` | Request a checkpoint when supported. |

### Available when a runner is configured

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/optimize` | Start one bounded automatic operation and return `202 Accepted` with an operation UID. |
| `GET` | `/operations/{uid}` | Return operation state and current correlation identifiers. |
| `POST` | `/operations/{uid}/stop` | Finish the active acquisition and submit no additional work. |

`POST /optimize` requires a finite iteration or trial budget. Backend stopping criteria may terminate earlier but do not replace the hard bound.

### Mutation semantics

- The service serializes all optimizer mutations with one lock.
- Every successful mutation increments an optimizer revision.
- Mutation requests may include an expected revision; stale revisions return `409 Conflict`.
- Mutation requests accept an idempotency key.
- Direct `suggest`, `ingest`, `fail`, and `checkpoint` requests return `409 Conflict` while an automatic operation is active.
- Read-only requests remain available during automatic operation.
- The service exposes no generic variable, attribute, method, or task endpoint.

## Automatic execution loop

The runner uses the same application-layer `suggest`, `ingest`, and failure methods as direct HTTP requests. It must not bypass validation, locking, revision updates, or checkpoint behavior by calling the backend through a separate path.

For each iteration:

1. Call `suggest(n_points)`.
2. Persist the suggestion and operation cursor.
3. Construct one trusted `BPlan` containing the suggestions and correlation metadata.
4. Submit the plan through the supported QueueServer API.
5. Persist the returned QueueServer item UID.
6. Wait for a matching Bluesky start/stop pair.
7. Confirm successful acquisition completion.
8. Wait for required acquisition data to become available when storage is eventually consistent.
9. Call the configured evaluation function.
10. Validate one outcome for each successfully acquired suggestion.
11. Call `ingest` or the failure capability as appropriate.
12. Checkpoint backend state.
13. Continue until the hard budget, backend stopping criterion, stop request, or error terminates the operation.

At most one acquisition batch may be outstanding.

## Runner state machine

```text
IDLE
  -> SUGGESTING
  -> SUBMITTING
  -> WAITING_FOR_RUN
  -> EVALUATING
  -> INGESTING
  -> CHECKPOINTING
  -> IDLE
```

Operation terminal states are:

```text
COMPLETED
FAILED
STOPPED
```

`STOPPED` means that a stop-after-current request was honored. It does not imply that an active RunEngine plan was aborted.

Queue removal, RunEngine pause, stop, abort, and halt remain explicit QueueServer operations outside this endpoint unless a later RFC specifies otherwise.

## Identity and correlation

Every automatic acquisition carries versioned metadata:

```json
{
  "bluesky_adaptive": {
    "schema_version": 1,
    "service_uid": "...",
    "operation_uid": "...",
    "suggestion_uids": ["..."]
  }
}
```

The service also records the QueueServer item UID and resulting Bluesky run UID. Evaluation and ingestion match by suggestion UID, never by list position or timing.

Suggestion identity must survive backend checkpoint and reload. A backend adapter may expose a stable, JSON-safe backend trial identifier directly or persist a reversible mapping between the service UID and backend trial identifier. Pending suggestions must remain addressable after restart; an in-memory-only mapping is invalid.

A completion consumer ignores unrelated runs, including runs from another adaptive service. Duplicate documents are safe to process repeatedly.

## Persistence and sources of truth

The system deliberately avoids a universal trial database.

| State | Authoritative system |
| --- | --- |
| Model configuration, pending/completed optimizer trials, fitted model | Optimizer backend persistence |
| Submitted, queued, running, and completed plans | QueueServer queue and history |
| Experimental documents and acquired data | Tiled and the Bluesky document stream |
| Current automatic-operation transition and cross-system identifiers | Service recovery cursor |

Ax deployments should use Ax's supported SQL persistence. Other backends should use their supported checkpoint format.

The service recovery cursor contains only:

- service UID;
- optimizer revision and checkpoint identity;
- active operation UID and state;
- stop-requested flag;
- suggestion UIDs and parameter payloads for the active batch;
- QueueServer item UID; and
- Bluesky run UID when known.

The cursor is written transactionally before and after externally visible side effects. The storage implementation must support atomic update and durable restart. The initial implementation may use a single-process relational store with SQLite as the development default and PostgreSQL for deployments that require networked storage.

## Recovery semantics

The service does not claim exactly-once distributed execution. It guarantees stable identities, idempotent handlers, and reconciliation against authoritative systems.

### Suggested but not submitted

If the optimizer contains pending suggestions but QueueServer has no corresponding item, the service either reuses the persisted suggestions or explicitly marks them abandoned before requesting replacements. It never silently asks for another set while leaving unknown pending trials.

### Submitted but completion unknown

The service queries QueueServer queue and history using the item UID and operation metadata. It does not resubmit while the original item's state can still be established.

### Acquisition completed but not evaluated

The service locates the run by correlation metadata or persisted run UID, waits for required data, and re-runs the idempotent evaluator.

### Evaluation completed but ingestion uncertain

The service checks backend trial state by suggestion UID. An already-completed trial is not ingested again.

### Missed document messages

Kafka may replay documents. ZMQ cannot. In either case, QueueServer history and Tiled provide reconciliation after restart or message loss.

## Failure semantics

- QueueServer rejection is returned without alteration and no acquisition is assumed.
- A failed or aborted acquisition is registered with a failure-aware optimizer when supported.
- Evaluation errors preserve their original exception context and terminate the operation unless the configured policy explicitly permits retry.
- Partial batch success is represented per suggestion when the acquisition and evaluator can establish it; otherwise the whole active batch is failed.
- Missing or scientifically invalid measurements are not silently converted into arbitrary numerical penalties.
- A backend checkpoint failure terminates the operation before another suggestion is requested.
- The service never silently swallows transport, storage, model, or control errors.

## QueueServer and control-system safety

QueueServer remains the sole remote hardware control boundary.

The service:

- submits only a configured, named acquisition plan;
- passes only configured devices and JSON-serializable parameters;
- relies on QueueServer allowed-plan/device and parameter validation;
- does not execute arbitrary QueueServer functions or scripts;
- does not write PVs directly;
- does not open, close, or destroy worker environments implicitly;
- does not change QueueServer permissions or autostart policy;
- does not clear or reorder unrelated queue items;
- does not start an idle queue unless that behavior is explicitly configured outside this service;
- uses finite operation budgets; and
- treats optimizer bounds and outcome constraints as search policy, not hardware safety interlocks.

The service's QueueServer credential must use the least-privileged user group capable of submitting the configured acquisition plan and reading required status/history.

## Existing services and libraries

This project provides only the missing optimizer-to-Bluesky bridge.

- [Bluesky QueueServer](https://blueskyproject.io/bluesky-queueserver/) owns plan validation, permissions, queue state, worker state, and RunEngine control.
- [Bluesky HTTP Server](https://blueskyproject.io/bluesky-httpserver/) provides the supported remote QueueServer API and its authentication/authorization facilities.
- Bluesky Kafka or ZMQ carries run documents.
- [Tiled](https://blueskyproject.io/tiled/) stores and serves experimental data.
- Ax, Xopt, and future backend adapters own optimization algorithms and backend state.
- Facility OIDC or reverse-proxy infrastructure authenticates users to the standalone service.
- Standard OpenTelemetry and Prometheus integrations provide service telemetry.

The adaptive service is not loaded into the Bluesky HTTP Server process because optimizer dependencies, GPU memory, long-running callbacks, and model failures should not increase the blast radius of the beamline control API.

## Deployment model

A service process hosts one optimizer and at most one active operation. Production runs one application worker; multiple Uvicorn workers would create competing in-memory optimizer objects and are unsupported.

Blocking backend, QueueServer, or Tiled calls must not block the FastAPI event loop. They run in a controlled worker thread or through native async clients. The optimizer mutation lock remains process-local because only one application worker is permitted.

Multiple independent adaptive capabilities are deployed as independently configured services, for example:

```text
/adaptive/bmm-beam-recovery
/adaptive/lix-crl-alignment
```

A future RFC may introduce multi-optimizer hosting if operating independent deployments becomes a demonstrated burden.

## Observability

Structured logs include, when applicable:

- service UID;
- operation UID;
- suggestion UID;
- QueueServer item UID;
- Bluesky run UID;
- optimizer revision;
- runner state; and
- error type and context.

`GET /state` and `GET /operations/{uid}` provide authoritative service-level status. QueueServer and Tiled remain authoritative for their respective resources. The service does not scrape console logs to infer QueueServer state.

No custom monitoring protocol or dashboard is part of this RFC.

## Migration plan

### Stage 1: Blop parity

- Replace the current package implementation with the current Blop source.
- Rename the package and imports to `bluesky_adaptive`.
- Move Blop tests, simulations, documentation, and optional dependency structure.
- Restore complete supported Blop behavior before redesigning service APIs.

### Stage 2: Protocol service

- Implement the single-optimizer service without QueueServer runner support.
- Support direct `suggest`, `ingest`, `fail`, `best`, state, revision, and checkpoint behavior.
- Verify restart using backend-native persistence.

### Stage 3: Simulated automatic runner

- Add the runner state machine using a simulated QueueServer/Tiled stack.
- Implement correlation, bounded operation, stop-after-current, and failure handling.
- Inject crashes at every state transition and prove recovery without duplicate acquisition or ingestion.

### Stage 4: NSLS-II integration

- Use the supported NSLS-II document transport and QueueServer HTTP deployment.
- Validate one optics-alignment or beam-recovery configuration in simulation, then recommendation-only mode, then supervised bounded operation.
- Do not run automated verification against live hardware.

### Stage 5: Blop retirement

- Migrate known downstream callers.
- Publish a final Blop release pointing users to `bluesky-adaptive`.
- Archive Blop after the replacement passes its beamline acceptance gates.
- Do not keep a permanent `blop` compatibility namespace in the new project.

## Verification and acceptance criteria

### Direct optimizer API

- Suggestions round-trip without loss of identity or parameter values.
- Observations are matched by UID, never position.
- Duplicate ingestion is rejected or treated idempotently according to backend state.
- Invalid or stale revisions return `409 Conflict`.
- Backend errors retain actionable detail.
- Checkpoint and restart preserve optimizer state.

### Automatic operation

- A bounded simulated optimization completes through a real QueueServer and Tiled integration stack.
- Foreign and duplicate run documents do not alter the operation.
- QueueServer rejection propagates unchanged.
- Failed and aborted runs do not become successful observations.
- Stop-after-current submits no further work.
- Direct mutation requests conflict while automatic operation is active.
- Restart recovery works from every runner state.
- No test depends on a live beamline, production QueueServer, or production Tiled service.

### Packaging and operations

- Importing the core package does not import FastAPI, QueueServer, Tiled, Kafka, Ax, Xopt, or Torch unless the corresponding feature is used.
- The service cannot start with multiple application workers.
- Secrets and service endpoints come from deployment configuration and are never included in API responses or logs.
- Normal beamline QueueServer use remains available when the adaptive service is stopped.

## Alternatives considered

### Preserve the existing `bluesky-adaptive` server

Rejected. Its generic variables, arbitrary registered methods, custom worker process, pipe JSON-RPC layer, and IOC mirroring do not express Blop's optimizer protocols and create unnecessary control surfaces.

### Model campaigns and multiple optimizer instances

Rejected for the initial product. One configured optimizer and one bounded operation are sufficient. Multiple deployments provide isolation without building tenancy, ownership, scheduling, and instance-registry machinery.

### Embed the service in Bluesky HTTP Server

Rejected for production. Sharing a process would couple optimizer dependencies and failures to the beamline control API. A reusable `APIRouter` remains acceptable for development, but the supported deployment is standalone.

### Use a general workflow platform

Rejected. MADSci, HELAO, Temporal, and similar systems solve broader multi-service or autonomous-laboratory problems. They would duplicate QueueServer and Tiled responsibilities for the target beamline use case.

### Use Optuna or MLflow as the service foundation

Rejected. Optuna provides study storage and distributed optimization but would replace rather than serve Blop's Ax/Xopt protocol adapters. MLflow provides experiment tracking, not Bluesky acquisition orchestration.

### Maintain a central adaptive trial database

Rejected. It would duplicate optimizer, QueueServer, and Tiled state and create a fourth source of truth. Only the cross-system recovery cursor is unique to this service.

### Guarantee exactly-once execution

Rejected as an invalid distributed-systems claim. Stable identities, idempotent handlers, and reconciliation provide the required behavior without pretending the participating services share a transaction.

## Deferred decisions

These are implementation details rather than product-boundary decisions:

- final URL naming and API version prefix;
- the first production document transport adapter;
- the relational schema and migration tool for the recovery cursor;
- whether direct historical-data seeding is part of the initial HTTP API;
- the precise representation of metric uncertainty in the wire schema; and
- whether a future service may host multiple optimizers.

Any decision that expands the product into campaign management, general workflow orchestration, arbitrary remote execution, or direct hardware control requires a separate RFC.
