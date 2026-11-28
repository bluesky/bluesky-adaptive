"""Core Scipy optimizer porting scipy algorithms."""

from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from threading import Condition, Thread
from typing import Any, cast

import numpy as np
from scipy.optimize import OptimizeResult

from bluesky_adaptive.protocols import ID_KEY, Optimizer
from bluesky_adaptive.scipy.configs import SCP, Objective, ScipyCFG
from bluesky_adaptive.scipy.normalizers import InnerOptimizer, ScipyResult


@dataclass
class _Request:
    """A point scipy is waiting on; ``ingest`` fills in ``outcome``."""

    x: np.ndarray
    outcome: Any = None


class _Stop(BaseException):
    """Unwinds the optimizer thread out of scipy when the session is closed or a point times out.

    A ``BaseException`` because scipy wrappers such as shgo's ``FieldWrapper`` swallow ``Exception``. ``error`` is
    ``None`` when the session was closed, and otherwise the failure that ``suggest()`` and ``session()`` re-raise.
    """

    def __init__(self, error: Exception | None = None):
        super().__init__(error)
        self.error = error


class InteractiveOptimizer(Optimizer):
    """An optimizer object to supply an interactive interface for the scipy optimizers, with some caveats.

    Scipy runs on a background thread. Every objective evaluation it requests becomes a pending point that
    ``suggest`` returns and ``ingest`` answers. ``_cond`` guards all state shared with that thread.

    Parameters
    ----------
    optimizer : InnerOptimizer
        The wrapped scipy optimizer.
    config : ScipyCFG | None, optional
        The optimization configuration, by default ``optimizer.config``.
    timeout : float | None, optional
        Seconds to wait for scipy to request a point, and for a suggested point to be ingested. When either wait
        expires, the next ``suggest()`` (or ``session()``, while it waits for scipy's first point) raises
        ``TimeoutError``. ``None``, the default, waits indefinitely, so a long acquisition or a paused RunEngine
        leaves scipy waiting for its point.
    """

    def __init__(self, optimizer: InnerOptimizer, config: ScipyCFG | None = None, timeout: float | None = None):
        self.optimizer = optimizer
        self._thread: Thread | None = None
        self.session(config=config if config else optimizer.config, timeout=timeout)

    def session(self, config: ScipyCFG, timeout: float | None = None):
        """
        Start a fresh optimization, closing the previous optimizer thread first.

        Returns once scipy has requested its first point, so errors raised while scipy starts up propagate here.
        Used both by the constructor and to restart an existing optimizer.
        """
        if self._thread is not None:
            self.close()
        self._params: list[str] = [dof.parameter_name for dof in config.dofs]
        self._objective: Objective = config.objective
        self.force_resiliance = False  # kinda hidden for now
        self._scale = np.ones(len(config.dofs))
        if config.rescale is not None:
            if isinstance(config.rescale, list):
                self._scale = config.rescale
            else:
                self._scale *= config.rescale
        self.SUGGESTION_TIMEOUT = timeout
        self.intermediate: OptimizeResult | ScipyResult | None = None
        self.final: OptimizeResult | ScipyResult | None = None

        self._cond = Condition()
        self._active: dict[int, _Request] = {}
        self._increment: int = 0
        self._closed = False
        self._finished = False
        self._converged = False
        self._error: Exception | None = None
        self._batch_size = config.threads or 1
        self._prefetched: deque[tuple[np.ndarray, Any]] = deque()

        kw: dict[str, Any] = {}
        if config.max_iter is not None:
            if config.optimizer is not SCP.TRUST_CONSTR:
                kw["max_iter"] = config.max_iter
            else:
                kw["maxiter"] = config.max_iter
        if config.eps is not None:
            kw["eps"] = config.eps
        if config.threads:
            kw["workers"] = self._map

        self._thread = Thread(target=self._run, args=(kw,), name="optimizer", daemon=True)
        self._thread.start()
        with self._cond:
            self._wait_for_worker()
        return self

    def __enter__(self):
        """Magic convenience to use "with" to better control thread lifetime."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Lifetime threads when using with."""
        self.close()

    def suggest(self, num_points: int | None = None) -> Sequence[Mapping]:
        """
        Provide a set of points in the input space, to be evaulated next.

        Blocks until scipy has requested points or stopped. Once scipy has stopped, the only suggestion is its final
        point.

        Parameters
        ----------
        num_points : int | None, optional
            The number of points to suggest. If not provided, will default to 1.

        Returns
        -------
        Sequence[Mapping]
            A sequence of mappings, each containing a parameterization of a point to evaluate next.
            Each mapping must contain a unique "_id" key to identify each parameterization.

        Raises
        ------
        RuntimeError
            Scipy converged and ``force_resiliance`` is off.
        TimeoutError
            Scipy requested no point within ``SUGGESTION_TIMEOUT`` seconds, or a suggested point was not ingested
            within it.
        Exception
            Any error raised by the wrapped scipy optimizer.
        """
        with self._cond:
            self._wait_for_worker()
            if self._converged and not self.force_resiliance:
                raise RuntimeError("The optimizer has suspended or reached convergence")
            if self.final is not None:
                return [self._suggestion(self.final.x, self.final.nit)]
            pending = list(self._active.items())[: 1 if num_points is None else num_points]
            return [self._suggestion(request.x, trial_id) for trial_id, request in pending]

    def ingest(self, points: Sequence[Mapping]) -> None:
        """
        Ingest a set of points into the experiment. Either from previously suggested points or from an external source.

        The "_id" key is optional.

        Parameters
        ----------
        points : Sequence[Mapping]
            A sequence of mappings containing the outcomes of each suggested parameterization.
        """
        with self._cond:
            for res in points:
                y = res[self._objective.name]
                request = self._active.pop(res[ID_KEY], None)
                if request is None:
                    if not self.force_resiliance:
                        # A point dropped because the optimizer thread failed, e.g. timed out, reports that failure.
                        raise self._error or ValueError("optimizer did not expect to receive an update")
                    continue
                request.outcome = y
                self._cond.notify_all()

    def get_best_points(self) -> list[tuple[Any, Mapping, Mapping]]:
        """
        Get a list of the optimal point found during optimization.

        Returns
        -------
        list[tuple[int, TParameterization, TOutcome]]
            Each element in the list is a tuple of:
              - trial index (int)
              - parameter values (dict)
              - metric values (dict, where values may be (value, sem) tuples)

        See Also
        --------
        navigate_to_best : Plan stub to move actuators to a best point.
        """
        result = self.intermediate
        if self.final is not None:
            result = self.final
        if (result is None) or (self._objective is None):
            raise ValueError("no optimization epoch has been recorded")

        vector = [x_n * s for s, x_n in zip(self._scale, result.x, strict=True)]
        cart = [
            result.nit - 1,
            cast(Mapping, dict(zip(self._params, vector, strict=True))),
            cast(Mapping, {self._objective.name: result.fun}),
        ]
        return cart

    def close(self):
        """
        Stop the optimizer thread and wait for it to exit.

        Pending points are dropped. ``final`` keeps the best point seen (or the start point) so a new session can
        resume from it. Calling ``close`` again has no further effect.
        """
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join()

    def _wait_for_worker(self) -> None:
        """Block until scipy has pending points or has stopped, re-raising its error; the caller holds ``_cond``."""
        if not self._cond.wait_for(lambda: self._active or self._finished, timeout=self.SUGGESTION_TIMEOUT):
            raise TimeoutError(f"The optimizer requested no point within {self.SUGGESTION_TIMEOUT} s")
        if self._error is not None:
            raise self._error

    def _run(self, kws: dict[str, Any]) -> None:
        """Optimizer-thread body: run scipy, then publish how it ended."""
        final: OptimizeResult | ScipyResult | None = None
        converged = False
        error: Exception | None = None
        try:
            final = self.optimizer.call(self._cost, self._record_intermediate, kws=kws)
            converged = True
        except _Stop as stop:
            # Closed or timed out: keep the best point seen so a new session can restart from it. A timeout is
            # also reported through ``error``, so suggest() raises it instead of returning the final point.
            final = self.intermediate
            if final is None:
                final = ScipyResult(list(self.optimizer.x0), np.nan, nit=self._increment)
            error = stop.error
        except Exception as e:  # handed to the caller by suggest() and session()
            error = e
        with self._cond:
            self._finished = True
            self._converged = converged
            self._error = error
            self.final = final
            self._active.clear()
            self._cond.notify_all()

    def _evaluate(self, xs: Sequence[np.ndarray]) -> list[Any]:
        """Publish ``xs`` as pending points and block until all are ingested; runs on the optimizer thread."""
        with self._cond:
            if self._closed:
                raise _Stop
            batch = {self._increment + i: _Request(np.asarray(x)) for i, x in enumerate(xs)}
            self._active.update(batch)
            self._increment += len(batch)
            self._cond.notify_all()
            self._cond.wait_for(
                lambda: self._closed or batch.keys().isdisjoint(self._active), timeout=self.SUGGESTION_TIMEOUT
            )
            # A fully ingested batch is still returned to scipy if close() raced the last ingest.
            pending = sorted(batch.keys() & self._active.keys())
            if pending:
                if self._closed:
                    raise _Stop
                raise _Stop(TimeoutError(f"Points {pending} were not ingested within {self.SUGGESTION_TIMEOUT} s"))
        outcomes = [request.outcome for request in batch.values()]
        if any(outcome is None for outcome in outcomes):
            raise ValueError("return value is not present")
        return outcomes

    def _cost(self, x: np.ndarray) -> Any:
        """Objective handed to scipy: replay the next measured batch outcome, else measure ``x`` alone."""
        if self._prefetched:
            expected, outcome = self._prefetched.popleft()
            if not np.array_equal(x, expected):
                raise RuntimeError(f"scipy evaluated {x} where the batched point {expected} was expected")
            return outcome
        return self._evaluate([x])[0]

    def _map(self, fun: Callable[[np.ndarray], Any], xs: Iterable[np.ndarray]) -> list[Any]:
        """
        Map-like ``workers`` for scipy that publishes up to ``threads`` points at once.

        Each batch is measured through ``_evaluate`` first; ``fun`` (scipy's wrapper around ``_cost``) then receives
        the measured outcomes in order, so scipy's own bookkeeping still runs.
        """
        xs = list(xs)
        for start in range(0, len(xs), self._batch_size):
            batch = xs[start : start + self._batch_size]
            self._prefetched.extend(zip(batch, self._evaluate(batch), strict=True))
        return [fun(x) for x in xs]

    def _record_intermediate(self, intermediate_result: OptimizeResult | ScipyResult) -> None:
        """Scipy callback keeping the best intermediate result; runs on the optimizer thread."""
        if self.intermediate is not None and self.intermediate.fun < intermediate_result.fun:
            return
        intermediate_result.nit = self._increment
        self.intermediate = intermediate_result

    def _suggestion(self, x: Iterable[float], trial_id: int) -> dict[str, Any]:
        """Map a point in scipy's scaled space to a parameterization tagged with ``trial_id``."""
        suggestion: dict[str, Any] = {name: x_n * s for name, s, x_n in zip(self._params, self._scale, x, strict=True)}
        suggestion[ID_KEY] = trial_id
        return suggestion
