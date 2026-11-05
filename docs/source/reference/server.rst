Server
======

.. warning::

   The server is an **experimental prototype**. The API may change in future releases
   without a deprecation period. Install it with ``pip install bluesky-adaptive[server]``.

``bluesky_adaptive.server`` provides an optimization manager and mountable FastAPI routers for
building an optimization service. The application supplies the scientific choices; the server
supplies agents, optimization sessions, execution through a queueserver, storage, and events.

Concepts
--------

*Problem template*
    Code registered by the application that builds an
    :class:`~bluesky_adaptive.protocols.OptimizationProblem` or
    :class:`~bluesky_adaptive.protocols.QueueserverOptimizationProblem` from parameters. Its
    Pydantic parameter model is the boundary between what the application fixes and what
    clients configure at runtime.
*Agent*
    Saved configuration: a template name and parameters, validated by building the problem once.
*Optimization*
    One session started from an agent: a snapshot of the agent's configuration, a freshly built
    optimizer, its trials, and its queueserver executions. Only one optimization is open at a time.
    Starting a new optimization starts fresh; optimizations left open when the server stops are
    marked ``interrupted`` and never resumed.

What is declared in code and what is configurable
-------------------------------------------------

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Declared by the application at startup
     - Configured by clients at runtime
   * - Problem templates and the code they run: optimizer construction, evaluation functions,
       acquisition plan names, device names
     - Agents: a template and the parameters its model allows, from nothing (a fixed problem)
       to a full Ax optimizer (:class:`~bluesky_adaptive.ax.server.AxOptimizerSpec`)
   * - Analyses (:class:`~bluesky_adaptive.server.AnalysisMethod`)
     - Which analysis to compute, with its parameters
   * - The queueserver, document stream, repository, health checks, beamline routers
     - Optimizations: start, suggest, evaluate, ingest, run, acquire, stop

Clients never supply code. Clients discover what they may configure from ``GET /problems``, which
returns each template's parameter JSON schema. Choices fixed by the application, such as acquisition
functions or, with :meth:`AxOptimizerSpec.restricted <bluesky_adaptive.ax.server.AxOptimizerSpec.restricted>`,
the DOF and objective names, appear there as ``enum`` values.

Routers
-------

Mount the routers you need, or use :func:`~bluesky_adaptive.server.create_app` for all of them.
Paths below are relative to the application's prefix (``/api/v1`` for ``create_app``).

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Router
     - Routes
   * - :func:`~bluesky_adaptive.server.create_agents_router`
     - ``GET /problems``; ``GET, POST /agents``; ``POST /agents/validate``;
       ``GET, PUT, DELETE /agents/{name}``; ``POST /agents/{name}/optimizations``
   * - :func:`~bluesky_adaptive.server.create_optimizations_router`
     - ``GET /optimizations``; ``GET /optimizations/{id}``, ``.../status``, ``.../trials``,
       ``.../runs``; ``POST /optimizations/{id}/stop``
   * - :func:`~bluesky_adaptive.server.create_optimizer_router`
     - ``POST /optimizations/{id}/suggest``, ``.../evaluate``, ``.../ingest``;
       ``GET /optimizations/{id}/best-points``
   * - :func:`~bluesky_adaptive.server.create_queueserver_router`
     - ``GET /queueserver/status``; ``POST /optimizations/{id}/run``, ``.../acquire``
   * - :func:`~bluesky_adaptive.server.create_analysis_router`
     - ``GET /analyses``; ``GET /optimizations/{id}/analyses``;
       ``POST /optimizations/{id}/analyses/{name}``
   * - :func:`~bluesky_adaptive.server.create_events_router`
     - ``GET /events`` (Server-Sent Events; ``?optimization_id=``, ``?snapshot=``,
       ``Last-Event-ID``)
   * - :func:`~bluesky_adaptive.server.create_system_router`
     - ``GET /health``; ``GET /capabilities``

Events are named after the resource that changed (``optimization``, ``execution``, ``trial``) and
carry its JSON representation. A new connection first receives a ``snapshot`` event with the open
optimizations, unless it resumes with ``Last-Event-ID`` from events still in the server's history.

Example
-------

``examples/server_app.py`` in the repository builds a server for the Himmelblau function with a
fixed template and a client-composed Ax template. Run it with
``pixi run -e dev-cpu uvicorn --app-dir examples server_app:app``.

.. code-block:: python

    from pydantic import BaseModel

    from bluesky_adaptive.ax.server import AxOptimizerSpec, ax_analysis_methods
    from bluesky_adaptive.protocols import QueueserverOptimizationProblem
    from bluesky_adaptive.server import (
        Catalog, InMemoryOptimizationRepository, OptimizationManager, ProblemTemplate, create_app,
    )

    # DOF names are the beamline's actuators; objective names are the outcomes MyEvaluation returns.
    class AlignmentOptimizerSpec(AxOptimizerSpec.restricted(dofs=["kbv_pitch", "kbh_pitch"], objectives=["fwhm"])):
        """An Ax optimizer over the mirror pitches."""

    class Params(BaseModel):
        optimizer: AlignmentOptimizerSpec

    def build(params: Params) -> QueueserverOptimizationProblem:
        return QueueserverOptimizationProblem(
            optimizer=params.optimizer.build(),
            actuators=[dof.name for dof in params.optimizer.dofs],
            sensors=["det"],
            evaluation_function=MyEvaluation(),  # loads the data of a run UID
            acquisition_plan="default_acquire",
        )

    manager = OptimizationManager(
        problems=Catalog([ProblemTemplate(name="alignment", params=Params, build=build)]),
        repository=InMemoryOptimizationRepository(),
        queueserver=queueserver_client,  # optional
        analyses=Catalog(ax_analysis_methods()),
    )
    app = create_app(manager, health_checks={"queueserver": queueserver_client.check_environment})

API
---

.. autoclass:: bluesky_adaptive.server.OptimizationManager
   :members:

.. autoclass:: bluesky_adaptive.server.ProblemTemplate
   :members:

.. autoclass:: bluesky_adaptive.server.AnalysisMethod

.. autoclass:: bluesky_adaptive.server.Catalog
   :members:

.. autoclass:: bluesky_adaptive.server.NoParams

.. autoclass:: bluesky_adaptive.server.OptimizationRepository
   :members:

.. autoclass:: bluesky_adaptive.server.InMemoryOptimizationRepository

.. autoclass:: bluesky_adaptive.server.EventBroker
   :members:

.. autofunction:: bluesky_adaptive.server.create_app

.. autofunction:: bluesky_adaptive.server.create_agents_router

.. autofunction:: bluesky_adaptive.server.create_optimizations_router

.. autofunction:: bluesky_adaptive.server.create_optimizer_router

.. autofunction:: bluesky_adaptive.server.create_queueserver_router

.. autofunction:: bluesky_adaptive.server.create_analysis_router

.. autofunction:: bluesky_adaptive.server.create_events_router

.. autofunction:: bluesky_adaptive.server.create_system_router

Records
^^^^^^^

.. autoclass:: bluesky_adaptive.server.AgentConfig

.. autoclass:: bluesky_adaptive.server.OptimizationRecord

.. autoclass:: bluesky_adaptive.server.OptimizationStatus

.. autoclass:: bluesky_adaptive.server.ExecutionRecord

.. autoclass:: bluesky_adaptive.server.TrialRecord

.. autoclass:: bluesky_adaptive.server.TrialStatus

Ax backend
^^^^^^^^^^

.. autoclass:: bluesky_adaptive.ax.server.AxOptimizerSpec
   :members: build, restricted

.. autofunction:: bluesky_adaptive.ax.server.ax_analysis_methods
