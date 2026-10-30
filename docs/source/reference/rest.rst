REST API
========

.. warning::

   The REST API is **experimental**. The API is not yet stable and may change
   in future releases without a deprecation period. It is not recommended for
   production use.

.. automodule:: bluesky_adaptive.rest

Endpoints
---------

Request and response bodies are JSON. Points use the same mappings as the Python API, including the
``"_id"`` key.

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Endpoint
     - Agent call
     - Notes
   * - ``GET /status``
     - --
     - :class:`~bluesky_adaptive.rest.AgentStatus` of the latest ``/run`` or ``/submit-suggestions``
   * - ``POST /run``
     - :meth:`~bluesky_adaptive.ax.queueserver_agent.QueueserverAgent.run`
     - ``202``; body :class:`~bluesky_adaptive.rest.RunRequest`
   * - ``POST /submit-suggestions``
     - :meth:`~bluesky_adaptive.ax.queueserver_agent.QueueserverAgent.submit_suggestions`
     - ``202``; body :class:`~bluesky_adaptive.rest.SuggestionsRequest`
   * - ``POST /stop``
     - :meth:`~bluesky_adaptive.ax.queueserver_agent.QueueserverAgent.stop`
     - A plan already on the queueserver still runs and is ingested
   * - ``POST /suggest``
     - ``suggest``
     - Ask without acquiring; body :class:`~bluesky_adaptive.rest.SuggestRequest`
   * - ``POST /ingest``
     - ``ingest``
     - ``204``; body :class:`~bluesky_adaptive.rest.IngestRequest`
   * - ``GET /best-points``
     - ``get_best_points``
     - List of :class:`~bluesky_adaptive.rest.BestPoint`
   * - ``GET /trials``
     - ``ax_client.summarize()``
     - One object per Ax trial; missing values are ``null``
   * - ``POST /checkpoint``
     - ``checkpoint``
     - ``204``; requires ``checkpoint_path`` on the agent

While an operation is running, ``/run``, ``/submit-suggestions``, ``/suggest``, ``/ingest``, and
``/checkpoint`` return ``409 Conflict``: the queueserver runner uses the optimizer from its document
listener thread, and Ax is not thread-safe.

Errors return ``{"detail": "<ExceptionType>: <message>"}``:

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Status
     - Cause
   * - ``400``
     - Ax ``UserInputError``
   * - ``409``
     - An operation is running, the queueserver environment is not open, other Ax errors (for example,
       no completed trials yet), or no ``checkpoint_path``
   * - ``422``
     - Invalid request body, or points the optimizer rejects (unknown ``"_id"``, no outcomes)
   * - ``502`` / ``504``
     - The queueserver rejected the plan, or the request to it timed out

Application
-----------

.. autofunction:: bluesky_adaptive.rest.create_app

Models
------

.. autoclass:: bluesky_adaptive.rest.AgentStatus

.. autoclass:: bluesky_adaptive.rest.RunRequest

.. autoclass:: bluesky_adaptive.rest.SuggestRequest

.. autoclass:: bluesky_adaptive.rest.SuggestionsRequest

.. autoclass:: bluesky_adaptive.rest.IngestRequest

.. autoclass:: bluesky_adaptive.rest.BestPoint
