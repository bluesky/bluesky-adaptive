Proposal: use event-model directly as the event schema. Each optimization loop composes its own run with `event_model.compose_run` and passes `(name, doc)` to ordinary Bluesky callbacks. There's no RunEngine involvement and no inferred readables. Existing consumers (`TiledWriter`, `RunRouter`, 0MQ/Kafka publishers, `OptimizationLogger`) work as they are, and local plans and the Queue Server runner emit the same documents.

The loops emit, not the agent: only the loop sees iteration boundaries, stopping reasons, and failures. Agents just hold the callbacks and the optimization UID.

- **One run per call** to `optimize` or the Queue Server runner's `run`, and likewise for the other loops. The stop document is `success` (with a reason for a stopping criterion or a graceful stop), `abort`, or `fail`.
  - Loops stop at the first failed acquisition, so a `fail` stop document's reason names the iteration, the suggestion IDs, and the error.
- **An optimization is a key, not a run.** Every record run and every raw-data run (via the acquisition plan's `md`) carries the same `blop_optimization_uid`. Agents generate it once; continuing in a new process passes it explicitly.
- **One stream, `primary`,** so the analysis table stays where it is today. It has one event per evaluated point, emitted after validation and before ingestion: `_id`, `_iteration`, `_acquisition`, the parameters, and the outcomes. Points still being acquired appear in their raw-data runs' `blop_suggestions`.
- **Reserve the `_` prefix** for bookkeeping fields. Reject parameter and outcome names that start with `_`, and reject a parameter and an outcome that share a name.
- **Self-description comes from the optimizer**, which decides what suggestions contain and which outcomes it needs. Evaluation functions stay plain callables.
  - `Describable` is a small protocol: `describe() -> dict[str, DataKey]`, the `describe()` half of `Readable`, covering parameters and outcomes. It declares the fields before the first event, and outcomes are checked against it before ingestion. Without it, the first point defines the fields.
  - The optimizer's `bluesky.protocols.Configurable` methods are recorded as the descriptor's `configuration`.
  - Run-wide facts (problem metadata, loop arguments, `blop_optimization_uid`) go in the start document. Everything is read when each run starts.
- **Callbacks are synchronous and their exceptions propagate**, so a failing persistence callback prevents ingestion. A callback that needs isolation uses a queue or a publisher.

event-model fixes a stream's keys with its first descriptor, so outcome keys must not change within a run. Non-scalar acquisition UIDs are stored as strings.

This replaces `read_step`, `InferredReadable`, `Source`, `readable_cache`, the outer `optimize` and `sample_suggestions` runs and their run keys, `OptimizationCallbackRouter`, and `_PrimaryStreamFilter`. It is a breaking change: records move from the outer `optimize` run to their own runs, and the bookkeeping fields are renamed (`suggestion_ids` becomes `_id`, `iteration` becomes `_iteration`, `acquisition_uid` becomes `_acquisition`).
