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
from typing import Callable, Optional, Tuple

import torch
from botorch import fit_gpytorch_mll
from botorch.acquisition import AcquisitionFunction, UpperConfidenceBound
from botorch.models import SingleTaskGP
from botorch.optim import optimize_acqf
from databroker.client import BlueskyRun
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
        raw_samples: int = 20,
        **kwargs,
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
        raw_samples : int, optional
            Number of samples used to instantiate the initial conditions of the acquisition function optimizer.
            For a discussion of num_restarts vs raw_samples, see:
            https://github.com/pytorch/botorch/issues/366
            Defaults to 20.
        """
        super().__init__(**kwargs)
        self.inputs = None
        self.targets = None

        self.device = (
            torch.device("cuda" if torch.cuda.is_available() else "cpu")
            if device is None
            else torch.device(device)
        )
        self.bounds = torch.tensor(bounds, device=self.device).view(2, -1)
        if gp is None:
            dummy_x, dummy_y = torch.randn(2, self.bounds.shape[-1], device=self.device), torch.randn(
                2, out_dim, device=self.device
            )
            gp = SingleTaskGP(dummy_x, dummy_y)

        self.surrogate_model = gp
        self.mll = ExactMarginalLogLikelihood(self.surrogate_model.likelihood, self.surrogate_model)

        self.surrogate_model.to(self.device)
        self.mll.to(self.device)

        if partial_acq_function is None:
            self._partial_acqf = lambda gp: UpperConfidenceBound(gp, beta=0.1)
            self.acqf_name = "UpperConfidenceBound"
        else:
            self._partial_acqf = partial_acq_function
            self.acqf_name = "custom"
        self.num_restarts = num_restarts
        self.raw_samples = raw_samples

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
            self.inputs = torch.atleast_2d(torch.tensor(x, device=self.device))
            self.targets = torch.atleast_1d(torch.tensor(y, device=self.device))
        else:
            self.inputs = torch.cat([self.inputs, torch.atleast_2d(torch.tensor(x, device=self.device))], dim=0)
            self.targets = torch.cat([self.targets, torch.atleast_1d(torch.tensor(y, device=self.device))], dim=0)
            self.inputs.to(self.device)
            self.targets.to(self.device)
        self.surrogate_model.set_train_data(self.inputs, self.targets, strict=False)
        return dict(independent_variable=x, observable=y, cache_len=len(self.targets))

    def report(self):
        """Fit GP, and construct acquisition function.
        Document retains state dictionary.
        """
        fit_gpytorch_mll(self.mll)
        acqf = self._partial_acqf(self.surrogate_model)
        return dict(
            latest_data=self.tell_cache[-1],
            cache_len=self.inputs.shape[0],
            **{
                "STATEDICT-" + ":".join(key.split(".")): val.detach().cpu().numpy()
                for key, val in acqf.state_dict().items()
            },
        )

    def ask(self, batch_size=1):
        """Fit GP, optimize acquisition function, and return next points.
        Document retains candidate, acquisition values, and state dictionary.
        """
        if batch_size > 1:
            logger.warning(f"Batch size greater than 1 is not implemented. Reducing {batch_size} to 1.")
            batch_size = 1
        fit_gpytorch_mll(self.mll)
        acqf = self._partial_acqf(self.surrogate_model)
        acqf.to(self.device)
        candidate, acq_value = optimize_acqf(
            acq_function=acqf,
            bounds=self.bounds,
            q=batch_size,
            num_restarts=self.num_restarts,
            raw_samples=self.raw_samples,
        )
        return (
            [
                dict(
                    candidate=candidate.detach().cpu().numpy(),
                    acquisition_value=acq_value.detach().cpu().numpy(),
                    latest_data=self.tell_cache[-1],
                    cache_len=self.inputs.shape[0],
                    **{
                        "STATEDICT-" + ":".join(key.split(".")): val.detach().cpu().numpy()
                        for key, val in acqf.state_dict().items()
                    },
                )
            ],
            torch.atleast_1d(candidate).detach().cpu().numpy(),
        )

    def remodel_from_report(self, run: BlueskyRun, idx: int = None) -> Tuple[AcquisitionFunction, SingleTaskGP]:
        idx = -1 if idx is None else idx
        keys = [key for key in run.report["data"].keys() if key.split("-")[0] == "STATEDICT"]
        state_dict = {".".join(key[10:].split(":")): torch.tensor(run.report["data"][key][idx]) for key in keys}
        acqf = self._partial_acqf(self.surrogate_model)
        acqf.load_state_dict(state_dict)
        acqf.to(self.device)
        return acqf, acqf.model
