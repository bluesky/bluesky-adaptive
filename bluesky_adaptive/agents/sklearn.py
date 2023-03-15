"""
Module of mixins for agents that range from the sensible to the useless.
These mixins act to fufill the abstract methods of blusky_adaptive.agents.Agent that are relevant to
the decision making, and not the experimental specifics.
Some of these are passive, and will not implement an ask.
    - tell
    - ask
    - report (optional)
    - name (optional)

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
from typing import Tuple

import numpy as np
import sklearn
from databroker.client import BlueskyRun

from bluesky_adaptive.agents.base import Agent

logger = getLogger("bluesky_adaptive.agents")


class SklearnEstimatorAgentBase(Agent, ABC):
    def __init__(self, *, estimator: sklearn.base.BaseEstimator, **kwargs):
        """Basic functionality for sklearn estimators. Maintains independent and dependent caches.
        Strictly passive agent with do ask mechanism: will raise NotImplementedError

        Parameters
        ----------
        estimator : sklearn.base.TransformerMixin
            Estimator instance that inherits from TransformerMixin and BaseEstimator
            This model will be used to call fit transform.
            Common examples include PCA and NMF.
        """
        super().__init__(**kwargs)
        self.independent_cache = []
        self.observable_cache = []
        self.model = estimator

    def tell(self, x, y):
        self.independent_cache.append(x)
        self.observable_cache.append(y)
        return dict(independent_variable=x, observable=y, cache_len=len(self.independent_cache))

    def ask(self, batch_size):
        raise NotImplementedError

    def update_model_params(self, params: dict):
        self.model.set_params(**params)
        self.close_and_restart()

    def server_registrations(self) -> None:
        super().server_registrations()
        self._register_method("update_model_params")


class DecompositionAgentBase(SklearnEstimatorAgentBase, ABC):
    def __init__(self, *, estimator: sklearn.base.TransformerMixin, **kwargs):
        """Passive, report only agent that provide dataset analysis for decomposition.

        Parameters
        ----------
        estimator : sklearn.base.TransformerMixin
            Estimator instance that inherits from TransformerMixin and BaseEstimator
            This model will be used to call fit transform.
            Common examples include PCA and NMF.
        """
        super().__init__(estimator=estimator, **kwargs)

    def start(self, *args, **kwargs):
        _md = dict(model_type=str(self.model).split("(")[0], model_params=self.model.get_params())
        self.metadata.update(_md)
        super().start(*args, **kwargs)

    def report(self, **kwargs):
        self.model.fit(np.array([x for _, x in sorted(zip(self.independent_cache, self.observable_cache))]))
        try:
            components = self.model.components_
        except AttributeError:
            components = []
        return dict(
            components=components,
            cache_len=len(self.independent_cache),
            latest_data=self.tell_cache[-1],
        )

    @staticmethod
    def remodel_from_report(run: BlueskyRun, idx: int = None) -> Tuple[sklearn.base.TransformerMixin, dict]:
        """Grabs specified (or most recent) report document and rebuilds modelling of dataset at that point.

        This enables fixed dimension reports that can be stacked and compared, while also allowing for
        deep inspection at the time of a report.

        Parameters
        ----------
        run : BlueskyRun
            Agent Run
        idx : int, optional
            Report index, by default most recent

        Returns
        -------
        model : sklearn.base.TransformerMixin
        data : dict
            Dictionary of model components, weights, independent_vars, and observables
        """
        module_ = importlib.import_module("sklearn.decomposition")
        model = getattr(module_, run.start["model_type"])().set_params(**run.start["model_params"])
        idx = -1 if idx is None else idx
        model.components_ = run.report["data"]["components"][idx]
        latest_uid = run.report["data"]["latest_data"][idx]
        independents, observables = [], []
        for ind, obs, uid in zip(
            run.tell["data"]["independent_variable"], run.tell["data"]["observable"], run.tell["data"]["exp_uid"]
        ):
            independents.append(ind)
            observables.append(obs)
            if uid == latest_uid:
                break
        independents, observables = zip(*sorted(zip(independents, observables)))
        arr = np.array(observables)
        try:
            weights = model.transform(arr)
        except AttributeError:
            model.fit(arr)
            model.components_ = run.report["data"]["components"][idx]
            weights = model.transform(arr)
        return model, dict(
            components=model.components_, weights=weights, independent_vars=independents, observables=observables
        )


class ClusterAgentBase(SklearnEstimatorAgentBase, ABC):
    def __init__(self, *, estimator: sklearn.base.ClusterMixin, **kwargs):
        """Passive, report only agent that provide dataset analysis for clustering.

        Parameters
        ----------
        estimator : sklearn.base.ClusterMixin
            Estimator instance that inherits from ClusterMixin, TransformerMixin and BaseEstimator
            This model will be used to call fit transform.
            Common examples include kmeans.
        """
        super().__init__(estimator=estimator, **kwargs)

    def start(self, *args, **kwargs):
        _md = dict(model_type=str(self.model).split("(")[0], model_params=self.model.get_params())
        self.metadata.update(_md)
        super().start(*args, **kwargs)

    def report(self, **kwargs):
        arr = np.array([x for _, x in sorted(zip(self.independent_cache, self.observable_cache))])
        self.model.fit(arr)

        return dict(
            cluster_centers=self.model.cluster_centers_,
            cache_len=len(self.independent_cache),
            latest_data=self.tell_cache[-1],
        )

    @staticmethod
    def remodel_from_report(run: BlueskyRun, idx: int = None) -> Tuple[sklearn.base.TransformerMixin, dict]:
        """Grabs specified (or most recent) report document and rebuilds modelling of dataset at that point.

        This enables fixed dimension reports that can be stacked and compared, while also allowing for
        deep inspection at the time of a report.

        Parameters
        ----------
        run : BlueskyRun
            Agent Run
        idx : int, optional
            Report index, by default most recent

        Returns
        -------
        model : SklearnEstimatorAgentBase
        data : dict
            Dictionary of model components, weights, independent_vars, and observables
        """
        module_ = importlib.import_module("sklearn.cluster")
        model = getattr(module_, run.start["model_type"])().set_params(**run.start["model_params"])
        idx = -1 if idx is None else idx
        model.cluster_centers_ = run.report["data"]["cluster_centers"][idx]
        latest_uid = run.report["data"]["latest_data"][idx]
        independents, observables = [], []
        for ind, obs, uid in zip(
            run.tell["data"]["independent_variable"], run.tell["data"]["observable"], run.tell["data"]["exp_uid"]
        ):
            independents.append(ind)
            observables.append(obs)
            if uid == latest_uid:
                break
        independents, observables = zip(*sorted(zip(independents, observables)))
        arr = np.array(observables)
        try:
            clusters = model.predict(arr)
            distances = model.transform(arr)
        except AttributeError:
            model.fit(arr)
            model.cluster_centers_ = run.report["data"]["cluster_centers"][idx]
            clusters = model.predict(arr)
            distances = model.transform(arr)
        return model, dict(
            clusters=clusters,
            distances=distances,
            cluster_centers=model.cluster_centers_,
            independent_vars=independents,
            observables=observables,
        )
