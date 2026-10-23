"""
Wraps Xopt generators into bluesky-adaptive agents.

These mixins act to fufill the abstract methods of blusky_adaptive.agents.Agent that are relevant to
the decision making, and not the experimental specifics.

Children will need to implement the following:
Experiment specific:
    - measurement_plan_name
    - measurement_plan_args
    - measurement_plan_kwargs
    - unpack_run
"""

from abc import ABC
from logging import getLogger

import numpy as np
from xopt import Generator

from bluesky_adaptive.agents.base import Agent

logger = getLogger("bluesky_adaptive.agents")


class XoptGeneratorBase(Agent, ABC):
    def __init__(
        self,
        *,
        generator: Generator,
        **kwargs,
    ):
        """Xopt based optimization agent.

        Assumes that inputs and outputs are ordered according to the Xopt generator's VOCs object.
        For example, independent_variable values should correspond to generator.vocs.variable_names
        and dependent_variable values should correspond to generator.vocs.output_names.

        Parameters
        ----------
        generator : Generator
            An Xopt Generator instance

        """
        super().__init__(**kwargs)
        self.generator = generator

    def ingest(self, independent_variable, dependent_variable=None):
        """Ingest new data into the generator."""
        # transform arrays to dicts
        data = dict(zip(self.generator.vocs.variable_names, independent_variable))

        if dependent_variable is not None:
            data.update(dict(zip(self.generator.vocs.output_names, dependent_variable)))

        self.generator.add_data(data)

        return data

    def suggest(self, batch_size):
        """Suggest next steps from the generator."""
        proposed_steps = self.generator.generate(batch_size)

        # transform dict to arrays
        next_steps = [[step[k] for k in self.generator.vocs.variable_names] for step in proposed_steps]
        return ([self.generator.model_dump()], [np.atleast_1d(step) for step in next_steps])
