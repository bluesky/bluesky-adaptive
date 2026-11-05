# `optimize` does not record or checkpoint the last iteration when the stopping criterion fires

## Problem

`plans.optimize` checks `should_stop()` after `optimize_step` has ingested the outcomes, but before `read_step` and `_maybe_checkpoint`. When the check returns `True`, the plan returns, so the final ingested iteration is neither recorded nor checkpointed. With a stopping criterion that fires after the first iteration, the optimizer ingests once and the `optimize` run contains no events.

`optimize_in_run` records before the check, but also returns before checkpointing.

## Proposal

In both plans, record and checkpoint before checking the stopping criterion.

## Acceptance

When the stopping criterion fires after iteration N, iteration N is recorded, and the optimizer is checkpointed if N falls on the checkpoint interval.
