"""
Module of mixins for agents that range from the sensible to the useless.
These mixins act to fufill the abstract methods of blusky_adaptive.agents.Agent that are relevant to
the decision making, and not the experimental specifics.
    - ingest
    - suggest
    - report (optional)
    - name (optional)

Children will need to implement the following:
Experiment specific:
    - measurement_plan_name
    - measurement_plan_args
    - measurement_plan_kwargs
    - unpack_run
"""

from abc import ABC
from collections.abc import Generator, Sequence
from logging import getLogger
from typing import Union

import numpy as np
from numpy.typing import ArrayLike

from bluesky_adaptive.agents.base import Agent

logger = getLogger("bluesky_adaptive.agents")


class SequentialAgentBase(Agent, ABC):
    """Agent Mixin to take a pre-defined sequence and walk through it on ``suggest``.

    Parameters
    ----------
    sequence : Sequence[Union[float, ArrayLike]]
        Sequence of points to be queried
    relative_bounds : tuple[Union[float, ArrayLike]], optional
        Relative bounds for the members of the sequence to follow, by default None

    Attributes
    ----------
    independent_cache : list
        List of the independent variables at each observed point
    observable_cache : list
        List of all observables corresponding to the points in the independent_cache
    sequence : Sequence[Union[float, ArrayLike]]
        Sequence of points to be queried
    relative_bounds : tuple[Union[float, ArrayLike]], optional
        Relative bounds for the members of the sequence to follow, by default None
    suggest_count : int
        Number of queries this agent has made
    """

    name = "sequential"

    def __init__(
        self,
        *,
        sequence: Sequence[Union[float, ArrayLike]],
        relative_bounds: tuple[Union[float, ArrayLike]] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.independent_cache = []
        self.observable_cache = []
        self.sequence = sequence
        self.relative_bounds = relative_bounds
        self.suggest_count = 0
        self._position_generator = self._create_position_generator()

    def _create_position_generator(self) -> Generator:
        """Yield points from sequence if within bounds"""
        for point in self.sequence:
            if self.relative_bounds:
                arr = np.array(point)
                condition = arr <= self.relative_bounds[1] or arr >= self.relative_bounds[0]
                try:
                    if condition:
                        yield point
                        continue
                    else:
                        logger.warning(
                            f"Next point will be skipped.  {point} in sequence for {self.instance_name}, "
                            f"is out of bounds {self.relative_bounds}"
                        )
                except ValueError:  # Array not float
                    if condition.all():
                        yield arr
                        continue
                    else:
                        logger.warning(
                            f"Next point will be skipped.  {point} in sequence for {self.instance_name}, "
                            f"is out of bounds {self.relative_bounds}"
                        )
            else:
                yield point

    def ingest(self, x, y) -> dict:
        self.independent_cache.append(x)
        self.observable_cache.append(y)
        return {"independent_variable": x, "observable": y, "cache_len": len(self.independent_cache)}

    def suggest(self, batch_size: int = 1) -> tuple[Sequence[dict[str, ArrayLike]], Sequence[ArrayLike]]:
        docs = []
        proposals = []
        for _ in range(batch_size):
            self.suggest_count += 1
            try:
                proposals.append(next(self._position_generator))
            except StopIteration:
                logger.warning("StopIteration met. Stopping sequential agent thread.")
                self.stop()
            docs.append({"proposal": proposals[-1], "suggestion_count": self.suggest_count})
        return docs, proposals

    def report(self, **kwargs) -> dict:
        return {"percent_completion": self.suggest_count / len(self.sequence)}
