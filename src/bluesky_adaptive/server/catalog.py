"""
Catalogs of application-declared components that clients choose by name.

A catalog entry is code registered by the application at startup. Clients never supply
code: they pick an entry by name and configure it with JSON that the entry's parameter
model validates. The parameter model is therefore the boundary between what an
application fixes and what clients may configure at runtime.
"""

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, JsonValue

from ..protocols import BaseOptimizationProblem, Optimizer
from .errors import NotFoundError

P = TypeVar("P", bound=BaseModel)

Problem = BaseOptimizationProblem[Any, Any, Any, Any]
"""Any optimization problem: :class:`~bluesky_adaptive.protocols.OptimizationProblem` or a queueserver problem."""


class NoParams(BaseModel):
    """Parameters of an entry that clients cannot configure."""

    model_config = ConfigDict(extra="forbid")


class CatalogEntry(Protocol):
    """What a catalog needs from its entries."""

    @property
    def name(self) -> str:
        """The unique name clients use to choose the entry."""
        ...

    @property
    def description(self) -> str:
        """A human-readable description of the entry."""
        ...

    @property
    def params(self) -> type[BaseModel]:
        """The Pydantic model that validates the client-supplied parameters."""
        ...


E = TypeVar("E", bound=CatalogEntry)


class CatalogEntryDescription(BaseModel):
    """A catalog entry as reported to clients."""

    name: str
    description: str
    params_schema: dict[str, JsonValue]


@dataclass(frozen=True)
class ProblemTemplate(Generic[P]):
    """
    Builds a fresh optimization problem from client-supplied parameters.

    A template decides how much of a problem is configurable: a template with
    :class:`NoParams` serves one fixed problem, while a template whose parameters include
    DOFs, objectives, and the choice of evaluation function lets clients compose problems.
    ``build`` is called with validated parameters every time an optimization starts, so
    every optimization gets its own optimizer. Raise :class:`ValueError` from ``build`` to
    reject parameters that the model cannot check, for example an unknown device name.

    Parameters
    ----------
    name : str
        The unique name agents use to refer to the template.
    params : type[pydantic.BaseModel]
        Validates the parameters an agent supplies.
    build : Callable[[P], BaseOptimizationProblem]
        Builds the problem. Return a :class:`~bluesky_adaptive.protocols.QueueserverOptimizationProblem`
        to allow running the problem through a queueserver.
    description : str, optional
        A human-readable description.
    """

    name: str
    params: type[P]
    build: Callable[[P], Problem]
    description: str = ""

    def create(self, params: Mapping[str, Any]) -> Problem:
        """
        Validate the parameters and build a problem.

        Raises
        ------
        pydantic.ValidationError
            If the parameters do not match the parameter model.
        ValueError
            If ``build`` rejects the parameters.
        """
        return self.build(self.params.model_validate(params))


@dataclass(frozen=True)
class AnalysisMethod(Generic[P]):
    """
    Inspects an optimizer, for example to tabulate trials or plot model predictions.

    Parameters
    ----------
    name : str
        The unique name clients use to request the analysis.
    params : type[pydantic.BaseModel]
        Validates the client-supplied analysis parameters.
    compute : Callable[[Optimizer, P], JsonValue]
        Computes the analysis. Called while holding the optimizer lock.
    supports : Callable[[Optimizer], bool], optional
        Whether the analysis applies to an optimizer. Default: every optimizer.
    description : str, optional
        A human-readable description.
    """

    name: str
    params: type[P]
    compute: Callable[[Optimizer, P], JsonValue]
    supports: Callable[[Optimizer], bool] = field(default=lambda optimizer: True)
    description: str = ""


class Catalog(Generic[E]):
    """
    A registry of named entries.

    Parameters
    ----------
    entries : Iterable[E], optional
        Entries to register.

    Raises
    ------
    ValueError
        If two entries have the same name.
    """

    def __init__(self, entries: Iterable[E] = ()):
        self._entries: dict[str, E] = {}
        for entry in entries:
            self.register(entry)

    def register(self, entry: E) -> E:
        """
        Register an entry.

        Raises
        ------
        ValueError
            If an entry with the same name is already registered.
        """
        if entry.name in self._entries:
            raise ValueError(f"An entry named {entry.name!r} is already registered.")
        self._entries[entry.name] = entry
        return entry

    def get(self, name: str) -> E:
        """
        Return the entry with the given name.

        Raises
        ------
        NotFoundError
            If no entry has the name.
        """
        try:
            return self._entries[name]
        except KeyError:
            raise NotFoundError(f"Unknown entry {name!r}; expected one of {sorted(self._entries)}") from None

    def __contains__(self, name: object) -> bool:
        """Whether an entry has the name."""
        return name in self._entries

    def __iter__(self) -> Iterator[E]:
        """Iterate over the entries in registration order."""
        return iter(self._entries.values())

    def __len__(self) -> int:
        """Return the number of entries."""
        return len(self._entries)

    def describe(self) -> list[CatalogEntryDescription]:
        """Describe every entry, including the JSON schema of its parameters."""
        return [
            CatalogEntryDescription(
                name=entry.name, description=entry.description, params_schema=entry.params.model_json_schema()
            )
            for entry in self._entries.values()
        ]
