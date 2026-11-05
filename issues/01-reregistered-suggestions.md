# `sample_suggestions` and `submit_suggestions` re-register suggestions that already have an `"_id"`

## Problem

With a `CanRegisterSuggestions` optimizer, `plans.sample_suggestions` and `QueueserverOptimizationRunner.submit_suggestions` pass every suggestion to `register_suggestions`, including suggestions from `suggest()` that already carry an `"_id"`. Both docstrings say such suggestions are accepted. `AxOptimizer.register_suggestions` attaches a new trial for each one (see its TODO), so the original trials are never completed and stay pending.

## Proposal

Register only the suggestions without an `"_id"`; keep the others unchanged and in their original order.

## Acceptance

- Acquiring the output of `optimizer.suggest()` creates no new trials.
- A mix of suggestions with and without `"_id"` keeps its order.
