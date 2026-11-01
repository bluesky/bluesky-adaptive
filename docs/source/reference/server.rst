Adaptive Server
===============

.. warning::

   The adaptive server is **experimental**. The API is not yet stable and may change
   in future releases without a deprecation period. It is not recommended for
   production use.

An application developer builds an adaptive server from **catalog entries**: optimizers, evaluation
functions, and optionally context functions and run ingestors. Clients (GUIs, scripts, AI agents)
compose agents from those entries over HTTP, together with the plans and devices the queueserver
allows; they never send code. Each agent runs the suggest → acquire → ingest loop through a Bluesky
Queueserver, one Bluesky run per iteration, and learns when a run completes, as reported by Tiled
(or by the ZMQ document stream). The ``prototypes/`` directory of the repository holds two complete
applications built this way: a beamline alignment server and a passive learning server.

Install with ``pip install bluesky-adaptive[server]``.

.. automodule:: bluesky_adaptive.server.app

Agent specs
-----------

``POST /agents`` takes a :class:`~bluesky_adaptive.server.ProblemSpec`. Names in DOFs, objectives and
constraints are data keys: a DOF's ``name`` is the key its readback is recorded under, and its
``actuator`` is the device path the queueserver moves. Constraints are linear expressions with spaces
around binary operators, because names may contain ``-``:

.. code-block:: json

    {
      "queueserver": "tst",
      "dofs": [
        {"name": "dcm_c2-pitch", "actuator": "dcm_c2.pitch", "bounds": [-1e-4, 1e-4]},
        {"name": "m1-pitch", "actuator": "m1.pitch", "bounds": [-1e-4, 1e-4]}
      ],
      "objectives": [{"name": "diag2_total"}],
      "outcome_constraints": ["diag2_y_centroid >= 116", "diag2_y_centroid <= 140"],
      "sensors": ["diag2"],
      "acquisition_plan": {"name": "adaptive_acquire"},
      "evaluation": {
        "name": "image-metrics",
        "params": {"screens": [{"image_key": "diag2_image", "prefix": "diag2", "metrics": ["total", "y_centroid"]}]}
      },
      "optimizer": {"name": "ax-bo", "options": {"seed": 0}}
    }

The server validates the spec against the catalog (entry names and params, declared outputs and
context features), the queueserver (allowed plans that fit the :class:`~bluesky_adaptive.protocols.AcquisitionPlan`
contract, movable actuators, readable sensors) and the entries' ``build`` functions, and answers
``422`` with the reason when anything does not fit.

An agent with a ``context`` entry is **contextual**: every ``/suggest`` and the first iteration of
``/run`` take ``context``, the UID of the Bluesky run whose conditions to suggest for. Later
iterations use the context of the agent's own previous run. An agent with an ``ingestion`` entry
can subscribe to every completed run it did not submit and learn from the runs its ingestor accepts.

Endpoints
---------

All endpoints are under ``/api/v1``; request and response bodies are JSON. Points use the same
mappings as the Python API, including the ``"_id"`` key.

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Endpoint
     - Description
   * - ``GET /catalog/{kind}``
     - Entries of one kind (``optimizers``, ``evaluations``, ``contexts``, ``ingestions``) with their
       params JSON schema and declared outputs or features
   * - ``GET /queueservers``
     - Configured queueservers with their manager status and run feed state
   * - ``GET /queueservers/{name}/plans``
     - Allowed plans, flagging those that fit the acquisition contract
   * - ``GET /queueservers/{name}/devices``
     - Allowed devices by dotted path, with their capabilities
   * - ``POST /agents``
     - ``201``; create an agent from a :class:`~bluesky_adaptive.server.ProblemSpec`
   * - ``GET /agents``, ``GET /agents/{id}``
     - Spec, entries, capabilities, status and ingestion summary
   * - ``DELETE /agents/{id}``
     - ``204``; ``409`` while an operation is running
   * - ``POST /agents/{id}/run``
     - ``202``; start the loop: ``iterations``, ``n_points``, ``checkpoint_interval``, ``context``
   * - ``POST /agents/{id}/submit-suggestions``
     - ``202``; acquire the given points in one iteration; points without ``"_id"`` are registered first
   * - ``POST /agents/{id}/stop``
     - Submit nothing more: ``stopped`` at once if the plan was still queued, otherwise ``stopping``
       until the in-flight run is ingested
   * - ``POST /agents/{id}/suggest``
     - Suggest without acquiring: ``n_points``, ``context``
   * - ``POST /agents/{id}/ingest``
     - ``204``; points measured elsewhere, or outcomes of suggestions by ``"_id"``
   * - ``GET /agents/{id}/best-points``
     - The optimizer's best points, ``[{"_id", "parameters", "outcomes"}]``; outcome values are as the
       optimizer reports them, for example ``[mean, variance]`` for Ax
   * - ``GET /agents/{id}/history``
     - Every operation and iteration: suggestions, context, run UID, ingested points, errors
   * - ``GET /agents/{id}/ingestion``
     - Subscription state, backlog, counts by status and the most recent runs
   * - ``POST /agents/{id}/ingestion/subscribe``, ``.../unsubscribe``
     - Start or stop learning from runs the agent did not submit
   * - ``POST /agents/{id}/ingestion/jobs``
     - ``202``; ingest runs by UID, whether or not the ingestor would accept them

``/run``, ``/submit-suggestions``, ``/suggest`` and ``DELETE`` return ``409`` while an operation is
running or stopping. ``/ingest`` waits for the optimizer instead.

Errors return ``{"detail": "<message>"}``:

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Status
     - Cause
   * - ``404``
     - Unknown agent, queueserver or catalog kind, or an agent without ingestion
   * - ``409``
     - The agent is busy, the queueserver environment is closed, or the optimizer has too little data
   * - ``422``
     - Invalid spec, params or points, or a context given to (or missing from) an agent
   * - ``502`` / ``503``
     - The queueserver rejected the request, or did not answer in time

Application
-----------

.. autofunction:: bluesky_adaptive.server.create_app

Settings
--------

.. autoclass:: bluesky_adaptive.server.ServerSettings
   :members: from_file

.. autoclass:: bluesky_adaptive.server.QueueserverSettings

.. autoclass:: bluesky_adaptive.server.TiledFeedSettings

.. autoclass:: bluesky_adaptive.server.ZmqFeedSettings

Catalog entries
---------------

.. autoclass:: bluesky_adaptive.server.OptimizerEntry

.. autoclass:: bluesky_adaptive.server.EvaluationEntry

.. autoclass:: bluesky_adaptive.server.ContextEntry

.. autoclass:: bluesky_adaptive.server.IngestionEntry

.. autoclass:: bluesky_adaptive.server.RunIngestor
   :members:

Problems
--------

.. autoclass:: bluesky_adaptive.server.ProblemSpec

.. autoclass:: bluesky_adaptive.server.RangeDOFSpec

.. autoclass:: bluesky_adaptive.server.ChoiceDOFSpec

.. autoclass:: bluesky_adaptive.server.ProblemDefinition
   :members: dof_names, actuators, objective_weights

.. autoclass:: bluesky_adaptive.server.EntryConfig

.. autoclass:: bluesky_adaptive.server.LinearConstraint
   :members:

.. autoclass:: bluesky_adaptive.server.LinearObjective
   :members:

.. autofunction:: bluesky_adaptive.server.parse_linear_constraint

.. autofunction:: bluesky_adaptive.server.parse_linear_objective
