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
``start``, ``descriptor``, ``event``, and ``stop`` documents. Each call to an
optimization loop (:func:`bluesky_adaptive.plans.optimize`,
:func:`bluesky_adaptive.plans.optimize_in_run`, :func:`bluesky_adaptive.plans.sample_suggestions`,
:func:`bluesky_adaptive.plans.acquire_baseline`, and the queue-server runner's ``run`` and
``submit_suggestions``) composes one *record run* with event-model and passes its documents to
the callbacks given to that call. Agents pass their :attr:`~bluesky_adaptive.ax.Agent.callbacks`.
The record run does not go through the RunEngine, so RunEngine subscribers see only the
acquisition runs.

The record run and the acquisition runs are related but distinct. An acquisition
run records what happened at the beamline for the suggested points. The record
run records the optimization-facing summary of each evaluated point: the
suggestion identifier, the iteration, the acquisition UID, the parameter values,
and the outcomes returned by the evaluation function. Every run of one
optimization carries the same ``start["adaptive"]["optimization_uid"]``, which
links record runs to their acquisition runs.

What a callback sees
--------------------

``start``
    Run-level context such as ``plan_name``, optimizer description, actuator names,
    sensor names, number of iterations, ``n_points``, and
    ``{"adaptive": {"optimization_uid": ...}}``.

``descriptor``
    One ``primary`` stream. Its ``object_keys`` list the parameter fields under
    ``"parameters"`` and the outcome fields under ``"outcomes"``. An optimizer that
    implements :class:`bluesky_adaptive.protocols.Describable` declares those fields;
    otherwise the first evaluated point defines them.

``event``
    One event per evaluated point, emitted after the outcomes are validated and
    before the optimizer ingests them: ``adaptive_id`` (the suggestion ID, as a
    string), ``adaptive_iteration`` (``-1`` for one-shot plans), ``adaptive_acquisition``,
    and the parameter and outcome values. Field names starting with ``adaptive_`` are
    reserved for these bookkeeping fields.

For ``adaptive_acquisition``, string and numeric UIDs are stored directly. Other UIDs are
stored using their ``repr``; the evaluation function still receives the original value.

``stop``
    Completion status and reason: ``success`` when the loop finishes, meets its
    stopping criterion, or is stopped; ``abort`` when it is aborted or halted;
    ``fail`` with the failing iteration and suggestion IDs otherwise.

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
