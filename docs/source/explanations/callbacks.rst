Optimization Callbacks
======================

Bluesky Adaptive callbacks are for observing optimization progress. They let users attach
side effects such as console output, file logging, dashboards, or notifications
without putting reporting logic inside the optimizer, acquisition plan, or
evaluation function.

Why callbacks exist
-------------------

Optimization can be a long-running activity, and users often need to know which
points were suggested, which acquisition run evaluated them, what outcomes came
back, and whether the run finished cleanly. These concerns are related to the
workflow, but they do not define the workflow. A callback sits beside the
optimization and reacts to the Bluesky documents it emits.

The built-in :class:`bluesky_adaptive.callbacks.logger.OptimizationLogger` follows this
model. It formats optimization progress for the console, but it does not choose
points, move hardware, or compute objectives.

How Bluesky Adaptive emits documents
------------------------------------

Bluesky Adaptive builds on the standard Bluesky callback model: callbacks receive
``start``, ``descriptor``, ``event``, and ``stop`` documents. Each call to
:func:`bluesky_adaptive.plans.optimize`, :func:`bluesky_adaptive.plans.sample_suggestions`, or
:func:`bluesky_adaptive.plans.acquire_baseline` composes one *record run* and sends its documents to
the callbacks passed in ``callbacks``. An agent passes the callbacks registered with
:meth:`bluesky_adaptive.ax.Agent.subscribe`.

The record run and the acquisition runs are related but distinct. The RunEngine executes the
acquisition plan, and each acquisition run records what happened at the beamline for the suggested
points. The record run records the optimization-facing summary of each evaluated point: its
suggestion identifier, the acquisition UID, the parameter values, and the outcomes returned by the
evaluation function. Record runs do not pass through the RunEngine, so callbacks subscribed with
``RE.subscribe`` receive only the acquisition runs. To store both in Tiled, subscribe the same
``TiledWriter`` to the RunEngine and to the agent; the record run carries the problem's optimization
UID in ``start["bluesky-adaptive"]["optimization_uid"]``.

A callback that raises does not keep documents from the other callbacks: every callback receives every
document, then the plan raises the first exception. Steps are recorded after the optimizer ingests
their outcomes, so a failing callback ends the optimization without losing the step it interrupted.

:func:`bluesky_adaptive.plans.optimize_in_run` is different: it runs the whole optimization inside
one RunEngine run and writes the summary to that run's ``optimization`` stream.

What a callback sees
--------------------

A record run contains the same document types as any other Bluesky run, but the contents are
optimization-focused.

``start``
    Run-level context such as the plan name, optimizer description, actuator names, sensor
    names, the optimization UID, and the plan arguments (``iterations`` and ``n_points`` for
    :func:`~bluesky_adaptive.plans.optimize`, the points for the other plans).

``descriptor``
    One ``primary`` stream. Its ``object_keys`` name the parameter fields under ``"parameters"``
    and the outcome fields under ``"outcomes"``.

``event``
    One event per evaluated point, holding ``adaptive_suggestion_id`` (the suggestion's ``_id`` as
    a string), ``adaptive_iteration`` (``-1`` outside the :func:`~bluesky_adaptive.plans.optimize`
    loop), ``adaptive_acquisition_uid``, the parameter values, and the outcome values.

    ``adaptive_acquisition_uid`` is the value the acquisition plan returned, such as the run UID from
    :func:`bluesky_adaptive.plans.default_acquire`. Values that are not strings are stored as their
    ``repr``; the evaluation function still receives the original value.

``stop``
    Completion status and reason, useful for summaries or cleanup. A failed step stops the run
    with ``"fail"`` and a reason naming the iteration and the suggestion IDs.

What belongs in a callback
--------------------------

Callbacks are best suited to observation and side effects:

- Formatting progress for a console or notebook.
- Writing optimization summaries to a file or database.
- Updating a dashboard or live display.
- Sending a notification when an optimization starts, stops, or reaches a
  notable outcome.
- Collecting lightweight statistics from emitted parameter and outcome values.

Callbacks should generally avoid changing the optimization state, moving
hardware, or computing objective values. Those responsibilities belong
elsewhere. If a value changes what the optimizer learns, it belongs in the
evaluation function. If a step changes what happens at the beamline, it belongs
in the acquisition plan. If it changes what point should be sampled next, it
belongs in the optimizer.

Writing custom callbacks
------------------------

Custom callbacks are ordinary Bluesky callbacks. They should rely on the
optimization documents emitted by Bluesky Adaptive, rather than on details of how the data
were acquired or how the outcomes were computed. For a runnable example, see
:doc:`/how-to-guides/custom-callbacks`.
