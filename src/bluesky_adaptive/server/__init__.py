"""
Multi-agent adaptive server.

.. warning::

    This package is **experimental**. The API is not yet stable and may change
    in future releases without a deprecation period.

An application supplies catalog entries (optimizers, evaluations, and optionally contexts and
ingestions) to :func:`create_app`; clients compose agents from them over HTTP.
"""

from .app import create_app
from .catalog import Catalog, ContextEntry, EvaluationEntry, IngestionEntry, OptimizerEntry, RunIngestor
from .connections import ConnectFunction
from .problem import (
    ChoiceDOFSpec,
    EntryConfig,
    LinearConstraint,
    LinearObjective,
    ProblemDefinition,
    ProblemSpec,
    RangeDOFSpec,
    parse_linear_constraint,
    parse_linear_objective,
)
from .settings import QueueserverSettings, ServerSettings, TiledFeedSettings, ZmqFeedSettings

__all__ = [
    "Catalog",
    "ChoiceDOFSpec",
    "ConnectFunction",
    "ContextEntry",
    "EntryConfig",
    "EvaluationEntry",
    "IngestionEntry",
    "LinearConstraint",
    "LinearObjective",
    "OptimizerEntry",
    "ProblemDefinition",
    "ProblemSpec",
    "QueueserverSettings",
    "RangeDOFSpec",
    "RunIngestor",
    "ServerSettings",
    "TiledFeedSettings",
    "ZmqFeedSettings",
    "create_app",
    "parse_linear_constraint",
    "parse_linear_objective",
]
