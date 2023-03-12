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

from abc import ABC
from logging import getLogger

import numpy as np
import sklearn

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
        return dict(independent_variable=[x], observable=[y], cache_len=[len(self.independent_cache)])

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

    def report(self, **kwargs):
        weights = self.model.fit_transform(
            np.array([x for _, x in sorted(zip(self.independent_cache, self.observable_cache))])
        )
        try:
            components = self.model.components_
        except AttributeError:
            components = []

        return dict(
            weights=[weights],
            components=[components],
            cache_len=[len(self.independent_cache)],
            latest_data=[self.tell_cache[-1]],
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

    def report(self, **kwargs):
        arr = np.array([x for _, x in sorted(zip(self.independent_cache, self.observable_cache))])
        self.model.fit(arr)
        clusters = self.model.predict(arr)
        distances = self.model.transform(arr)

        return dict(
            clusters=[clusters],
            distances=[distances],
            cluster_centers=[self.model.cluster_centers_],
            cache_len=[len(self.independent_cache)],
            latest_data=[self.tell_cache[-1]],
        )
