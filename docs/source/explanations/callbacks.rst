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
:func:`bluesky_adaptive.plans.acquire_baseline`, and each ``run`` or ``submit_suggestions`` call of a
:class:`~bluesky_adaptive.ax.queueserver_agent.QueueserverAgent`, produces one *optimization run* summarizing the
evaluated points and sends its documents to the callbacks registered with
the agent's :meth:`subscribe <bluesky_adaptive.ax.Agent.subscribe>` or passed in the plan's
``callbacks`` argument.

Optimization runs are separate from the acquisition runs that the RunEngine produces when it
measures the points. Callbacks subscribed with ``RE.subscribe`` receive only the acquisition runs.
To store optimization runs in Tiled, subscribe the ``TiledWriter`` to the agent as well; see
:doc:`/how-to-guides/use-tiled`.

A callback that raises ends the optimization run, but the other callbacks still receive the rest
of it, including its ``stop`` document.

:func:`bluesky_adaptive.plans.optimize_in_run` is different: its optimization run is a RunEngine run
that holds the acquired data, with the summary in its ``optimization`` stream.

What a callback sees
--------------------

An optimization run contains the same document types as any other Bluesky run:

``start``
    Run-level context such as the plan name and arguments, the optimizer, actuator and sensor
    names, and the optimization UID.

``descriptor``
    One ``primary`` stream. Its ``object_keys`` name the parameter fields under ``"parameters"``
    and the outcome fields under ``"outcomes"``.

``event``
    One event per evaluated point: its parameter and outcome values, its suggestion ID
    (``adaptive_suggestion_id``), its iteration (``adaptive_iteration``), and the UID returned by
    the acquisition plan (``adaptive_acquisition_uid``), or the queue item UID for a ``QueueserverAgent``.

``stop``
    Completion status and reason, useful for summaries or cleanup.

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
