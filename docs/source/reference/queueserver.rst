Queueserver
===========

.. warning::

   The queueserver integration is **experimental**. The API is not yet stable
   and may change in future releases without a deprecation period. It is not
   recommended for production use.

These classes implement the distributed optimization backend that connects
Bluesky Adaptive to a remote `Bluesky Queueserver <https://blueskyproject.io/bluesky-queueserver/>`_.
See the :doc:`/tutorials/queueserver` tutorial for a full worked example.

OptimizationResult
------------------

.. autoclass:: bluesky_adaptive.queueserver.OptimizationResult
   :members:
   :undoc-members:

IterationRecord
---------------

.. autoclass:: bluesky_adaptive.queueserver.IterationRecord
   :members:
   :undoc-members:

QueueserverClient
-----------------

.. autoclass:: bluesky_adaptive.queueserver.QueueserverClient
   :members:
   :undoc-members:
   :show-inheritance:

QueueserverOptimizationRunner
------------------------------

.. autoclass:: bluesky_adaptive.queueserver.QueueserverOptimizationRunner
   :members:
   :undoc-members:
   :show-inheritance:

Completed runs
--------------

.. automodule:: bluesky_adaptive.runs

.. autodata:: bluesky_adaptive.runs.CORRELATION_UID_KEY

.. autoclass:: bluesky_adaptive.runs.CompletedRun
   :members:

.. autoclass:: bluesky_adaptive.runs.RunFeed
   :members:

.. autoclass:: bluesky_adaptive.runs.DocumentStreamFeed
   :members:

.. autoclass:: bluesky_adaptive.runs.TiledRunFeed
   :members:

.. autoclass:: bluesky_adaptive.runs.RunRouter
   :members:
