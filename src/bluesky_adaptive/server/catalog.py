"""
Catalog entries: what an application developer supplies to the adaptive server.

.. warning::

    This module is **experimental**. The API is not yet stable and may change
    in future releases without a deprecation period.

Each entry pairs a pydantic model, which clients fill in through the agent spec, with a ``build``
function that receives the validated :class:`~bluesky_adaptive.server.problem.ProblemDefinition`.
``build`` raising ``ValueError`` rejects the agent with 422; it is the application's validation hook.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from ..protocols import ContextFunction, EvaluationFunction, Optimizer
from .problem import ProblemDefinition

ParamsT = TypeVar("ParamsT", bound=BaseModel)


@runtime_checkable
class RunIngestor(Protocol):
    """
    Turns runs the agent did not submit into points for :meth:`~bluesky_adaptive.protocols.Optimizer.ingest`.

    ``accepts`` is a cheap check on the start document before any data is loaded. It is a method of
    the built ingestor, rather than a function of the start document alone, so it can use the
    entry's params and the problem (for example, to require that the run moved exactly the agent's DOFs).
    """

    def accepts(self, start: Mapping[str, Any]) -> bool:
        """Return whether a run with this start document may hold points for the agent."""
        ...

    def __call__(self, uid: str) -> Sequence[Mapping[str, Any]]:
        """Return points (DOF values and outputs, no ``"_id"``) measured in run ``uid``, or ``[]``."""
        ...


@dataclass(frozen=True)
class OptimizerEntry(Generic[ParamsT]):
    """
    An optimizer clients can choose.

    Parameters
    ----------
    name : str
        Catalog name.
    version : str
        Behavior version, recorded with every agent built from the entry.
    options : type[pydantic.BaseModel]
        Model of the options clients give in ``spec.optimizer.options``.
    build : Callable[[ProblemDefinition, options, Path], Optimizer]
        Builds the optimizer. The path is a file path in the agent's own directory; the optimizer may
        write its checkpoint there (adding a suffix of its choice).
    description : str
        One line shown in the catalog.
    no_data_errors : tuple[type[Exception], ...]
        Exceptions the optimizer raises from ``suggest`` or ``get_best_points`` because it has too
        little data. The server reports them with 409 rather than 500. ``ValueError`` from
        ``get_best_points`` always counts.
    """

    name: str
    version: str
    options: type[ParamsT]
    build: Callable[[ProblemDefinition, ParamsT, Path], Optimizer]
    description: str = ""
    no_data_errors: tuple[type[Exception], ...] = ()


@dataclass(frozen=True)
class EvaluationEntry(Generic[ParamsT]):
    """
    An evaluation function clients can choose.

    Parameters
    ----------
    name : str
        Catalog name.
    version : str
        Behavior version.
    params : type[pydantic.BaseModel]
        Model of ``spec.evaluation.params``.
    build : Callable[[ProblemDefinition, params], EvaluationFunction[str]]
        Builds the evaluation function, called with a Bluesky run UID and the suggestions.
    outputs : Callable[[params], Iterable[str]]
        The outcome names the evaluation produces. Objectives and outcome constraints may use only these.
    description : str
        One line shown in the catalog.
    """

    name: str
    version: str
    params: type[ParamsT]
    build: Callable[[ProblemDefinition, ParamsT], EvaluationFunction[str]]
    outputs: Callable[[ParamsT], Iterable[str]]
    description: str = ""


@dataclass(frozen=True)
class ContextEntry(Generic[ParamsT]):
    """
    A way to derive context features from a Bluesky run, for context-aware optimizers.

    Parameters
    ----------
    name : str
        Catalog name.
    version : str
        Behavior version.
    params : type[pydantic.BaseModel]
        Model of ``spec.context.params``.
    build : Callable[[ProblemDefinition, params], ContextFunction[str]]
        Builds the function from run UID to feature values.
    features : Callable[[params], Iterable[str]]
        The feature names the function returns. Optimizers need them at build time.
    description : str
        One line shown in the catalog.
    """

    name: str
    version: str
    params: type[ParamsT]
    build: Callable[[ProblemDefinition, ParamsT], ContextFunction[str]]
    features: Callable[[ParamsT], Iterable[str]]
    description: str = ""


@dataclass(frozen=True)
class IngestionEntry(Generic[ParamsT]):
    """
    A way to learn from runs the agent did not submit.

    Parameters
    ----------
    name : str
        Catalog name.
    version : str
        Behavior version.
    params : type[pydantic.BaseModel]
        Model of ``spec.ingestion.params``.
    build : Callable[[ProblemDefinition, params], RunIngestor]
        Builds the ingestor.
    outputs : Callable[[params], Iterable[str]] | None
        Outcome names the ingestor produces, if known from the params alone. They must be among the
        evaluation's outputs.
    description : str
        One line shown in the catalog.
    """

    name: str
    version: str
    params: type[ParamsT]
    build: Callable[[ProblemDefinition, ParamsT], RunIngestor]
    outputs: Callable[[ParamsT], Iterable[str]] | None = None
    description: str = ""


_EntryT = TypeVar("_EntryT", OptimizerEntry, EvaluationEntry, ContextEntry, IngestionEntry)


def _index(entries: Iterable[_EntryT], label: str) -> dict[str, _EntryT]:
    index: dict[str, _EntryT] = {}
    for entry in entries:
        if entry.name in index:
            raise ValueError(f"duplicate {label} entry {entry.name!r}")
        index[entry.name] = entry
    return index


class Catalog:
    """
    The entries an application offers, indexed by name.

    Parameters
    ----------
    optimizers : Iterable[OptimizerEntry]
        At least one optimizer.
    evaluations : Iterable[EvaluationEntry]
        At least one evaluation.
    contexts : Iterable[ContextEntry], optional
        Context entries for context-aware optimizers.
    ingestions : Iterable[IngestionEntry], optional
        Ingestion entries for passive learning.

    Raises
    ------
    ValueError
        If two entries of a kind share a name, or there is no optimizer or evaluation.
    """

    def __init__(
        self,
        *,
        optimizers: Iterable[OptimizerEntry[Any]],
        evaluations: Iterable[EvaluationEntry[Any]],
        contexts: Iterable[ContextEntry[Any]] = (),
        ingestions: Iterable[IngestionEntry[Any]] = (),
    ):
        self.optimizers: dict[str, OptimizerEntry[Any]] = _index(optimizers, "optimizer")
        self.evaluations: dict[str, EvaluationEntry[Any]] = _index(evaluations, "evaluation")
        self.contexts: dict[str, ContextEntry[Any]] = _index(contexts, "context")
        self.ingestions: dict[str, IngestionEntry[Any]] = _index(ingestions, "ingestion")
        if not self.optimizers or not self.evaluations:
            raise ValueError("a catalog needs at least one optimizer and one evaluation entry")
