# `QueueserverClient` changes queue autostart when constructed

## Problem

`QueueserverClient.__init__` calls `queue_autostart(autostart)`, with `autostart=True` by default. Creating a client, and so every `QueueserverAgent`, changes the state of a shared queueserver, and passing `autostart=False` turns autostart off for everyone.

## Proposal

Make no state-changing request in the constructor; whether the queue runs is a deployment decision. Document that the queue must be running or have autostart enabled, and enable it explicitly in the queueserver tutorial.

This is a breaking change: callers that relied on the implicit autostart must enable it themselves.

## Acceptance

Constructing a `QueueserverClient` or `QueueserverAgent` sends no state-changing request.
