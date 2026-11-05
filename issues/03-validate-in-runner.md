# Queueserver runner: validate suggestions and outcomes like the local plans

## Problem

`plans.optimize` checks suggestions with `_validate_suggestions` and outcomes with `_validate_outcomes` (every outcome has a unique `"_id"`, and the IDs match the suggestions) before ingesting. The queueserver runner ingests whatever the evaluation function returns, so unknown IDs reach the optimizer and missing ones leave trials pending.

## Proposal

Call the same validators in the runner: suggestions before submission, outcomes before ingestion. A failed check fails the acquisition and registers its suggestions as failed.

Builds on #76: the checks go into its runner loop, right after the evaluation function returns.

## Acceptance

Outcomes whose IDs differ from the suggestions' are not ingested, and the run fails with the validator's error.
