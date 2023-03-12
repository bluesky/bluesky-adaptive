"""
Basic BoTorch functionality, primarily for examples.

These mixins act to fufill the abstract methods of blusky_adaptive.agents.Agent that are relevant to
the decision making, and not the experimental specifics.

Children will need to implement the following:
Experiment specific:
    - measurement_plan_name
    - measurement_plan_args
    - measurement_plan_kwargs
    - unpack_run
"""

import importlib
from abc import ABC
from logging import getLogger
from typing import Callable, Optional

import torch
from botorch import fit_gpytorch_mll
from botorch.acquisition import UpperConfidenceBound
from botorch.models import SingleTaskGP
from botorch.optim import optimize_acqf
from gpytorch.mlls import ExactMarginalLogLikelihood

from bluesky_adaptive.agents.base import Agent

logger = getLogger("bluesky_adaptive.agents")


class SingleTaskGPAgentBase(Agent, ABC):
    def __init__(
        self,
        *,
        bounds: torch.Tensor,
        gp: SingleTaskGP = None,
        device: torch.device = None,
        out_dim=1,
        partial_acq_function: Optional[Callable] = None,
        num_restarts: int = 10,
        **kwargs
    ):
        """Single Task GP based Bayesian Optimization

        Parameters
        ----------
        bounds : torch.Tensor
            A `2 x d` tensor of lower and upper bounds for each column of `X`
        gp : SingleTaskGP, optional
            GP surrogate model to use, by default uses BoTorch default
        device : torch.device, optional
            Device, by default cuda if avail
        out_dim : int, optional
            Dimension of output predictions by surrogate model, by default 1
        partial_acq_function : Optional[Callable], optional
            Partial acquisition function that will take a single argument of a conditioned surrogate model.
            By default UCB with beta at 0.1
        num_restarts : int, optional
            Number of restarts for optimizing the acquisition function, by default 10
        """
        super().__init__(**kwargs)
        self.inputs = None
        self.observables = None

        self.device = (
            torch.device("cuda" if torch.cuda.is_available() else "cpu")
            if device is None
            else torch.device(device)
        )
        self.bounds = torch.tensor(bounds, device=self.device)
        if gp is None:
            dummy_x, dummy_y = torch.randn(2, self.bounds.shape[-1]), torch.randn(2, out_dim)
            gp = SingleTaskGP(dummy_x, dummy_y)

        self.surrogate_model = gp
        self.mll = ExactMarginalLogLikelihood(self.surrogate_model.likelihood, self.surrogate_model)

        self.surrogate_model.to(self.device)
        self.mll.to(self.device)

        if partial_acq_function is None:
            self._partial_acqf = lambda gp: UpperConfidenceBound(gp, beta=0.1)
            self.acqf_name = "UpperConfidenceBound"
        self._partial_acqf = partial_acq_function
        self.acqf_name = "custom"
        self.num_restarts = num_restarts

    def server_registrations(self) -> None:
        super().server_registrations()
        self._register_method("update_acquisition_function")

    def update_acquisition_function(self, acqf_name, **kwargs):
        module = importlib.import_module("botorch.acquisition")
        self.acqf_name = acqf_name
        self._partial_acqf = lambda gp: getattr(module, acqf_name)(gp, **kwargs)
        self.close_and_restart()

    def start(self, *args, **kwargs):
        _md = dict(acqf_name=self.acqf_name)
        self.metadata.update(_md)
        super().start(*args, **kwargs)

    def tell(self, x, y):
        if self.inputs is None:
            self.inputs = torch.atleast_2d(torch.tensor(x))
            self.targets = torch.atleast_1d(torch.tensor(y))
        else:
            self.inputs = torch.cat([self.inputs, torch.atleast_2d(torch.tensor(x))], dim=0)
            self.targets = torch.cat([self.targets, torch.atleast_1d(torch.tensor(y))], dim=0)
        self.surrogate_model.set_train_data(self.inputs, self.targets, strict=False)
        return dict(independent_variable=x, observable=y, cache_len=len(self.targets))

    def report(self):
        """Fit GP, and construct acquisition function.
        Document retains state dictionary.
        """
        fit_gpytorch_mll(self.mll)
        acqf = self._partial_acqf(self.surrogate_model)
        return dict(state_dict=acqf.state_dict())

    def ask(self, batch_size=1):
        """Fit GP, optimize acquisition function, and return next points.
        Document retains candidate, acquisition values, and state dictionary.
        """
        fit_gpytorch_mll(self.mll)
        acqf = self._partial_acqf(self.surrogate_model)
        candidate, acq_value = optimize_acqf(
            acq_function=acqf, bounds=self.bounds, q=batch_size, num_restarts=self.num_restarts
        )
        return (
            dict(candidate=candidate, acquisition_value=acq_value, state_dict=acqf.state_dict()),
            torch.atleast_1d(candidate).detach().numpy(),
        )
