# Serialize optimizer access between the queueserver runner and other callers

## Problem

The runner calls `suggest`, `ingest`, `checkpoint`, and `register_failures` from its own thread (the document callback today, the runner's worker after #76). Meanwhile, users call the same optimizer from other threads: through `QueueserverAgent` methods, through the backend's own API (for example `agent.ax_client`), or from a server's endpoints. Ax's `Client` is not documented as thread-safe.

## Proposal

Add `QueueserverOptimizationRunner(..., optimizer_lock=None)`: a reentrant lock held around every optimizer call. If none is given, the runner creates one and exposes it as `runner.optimizer_lock` so other code can share it. `QueueserverAgent` holds the lock in its own optimizer methods; code that uses the backend's API directly holds it explicitly (`with agent.optimizer_lock: ...`).

Builds on #76: the lock wraps the optimizer calls in its runner loop.

## Acceptance

A call to the optimizer from another thread never overlaps a call from the runner.
