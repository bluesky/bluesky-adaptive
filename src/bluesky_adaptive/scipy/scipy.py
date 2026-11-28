"""Scipy optimization power class for fast start QOL and Ax like agent behavior."""

from collections.abc import Mapping, Sequence
from typing import Any, Generic, cast

from bluesky_adaptive._agent import _BaseAgent
from bluesky_adaptive.plans import optimize
from bluesky_adaptive.protocols import (
    AcquisitionPlan,
    Actuator,
    EvaluationFunction,
    OptimizationProblem,
    Sensor,
    TUid,
)
from bluesky_adaptive.scipy.configs import SCP, Objective, RangeDOF, ScipyCFG
from bluesky_adaptive.scipy.inverter import InteractiveOptimizer
from bluesky_adaptive.scipy.normalizers import SHGO, DualAnnealing, Minimize


class Scipy(_BaseAgent, Generic[TUid]):
    """
    A convenience interface associated with running optimizations with Scipy, providing similar syntax to the Ax Agent
    (allowing drop in swapping as much as possible).

    Useful as a cover in for all the QOL provided by the Agent object.

    Parameters
    ----------
    sensors : Sequence[Sensor]
        The sensors to use for acquisition.
    config : ScipyCFG
        The optimization problem and SciPy options.
    evaluation_function : EvaluationFunction
        The function to evaluate acquired data and produce outcomes.
    acquisition_plan : AcquisitionPlan | None, optional
        The acquisition plan to use. If not provided, :func:`bluesky_adaptive.plans.default_acquire` is used.
    optimization_uid : str | None, optional
        The UID recorded in the documents of every optimization run. A random UUID is used if not provided.
    metadata : Mapping[str, Any] | None, optional
        Metadata added to the start document of every optimization run.
    timeout : int | None, optional
        Seconds SciPy waits for each requested point to be ingested. Default is 200.
    resiliance : bool, optional
        Keep suggesting SciPy's final point after it has finished and skip ingested points it did not request,
        instead of raising. Default is True.
    sessioning : bool, optional
        Close the SciPy optimizer thread when :meth:`optimize` finishes. Default is True.
    """  # noqa: D205

    def __init__(
        self,
        sensors: Sequence[Sensor],
        config: ScipyCFG,
        evaluation_function: EvaluationFunction[TUid],
        acquisition_plan: AcquisitionPlan[TUid] | None = None,
        optimization_uid: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        *,
        timeout: int | None = 200,
        resiliance: bool = True,
        sessioning: bool = True,
    ):
        if config.optimizer not in list(SCP):
            raise ValueError(f"optimizer {config.optimizer} not in supported optimizers:{list(SCP)}")

        match config.optimizer:
            case SCP.DUAL_ANNEALING:
                self.inner = DualAnnealing(config)
            case SCP.SHGO:
                self.inner = SHGO(config)
            case _:
                self.inner = Minimize(config)

        self.config = config
        self._sensors = sensors
        self._actuators = [cast(Actuator, dof.actuator) for dof in config.dofs if dof.actuator is not None]
        self._evaluation_function = evaluation_function
        self._acquisition_plan = acquisition_plan
        self.timeout = timeout
        self.optimizer = InteractiveOptimizer(self.inner, timeout=self.timeout)
        self.optimizer.force_resiliance = self.resiliance = resiliance
        super().__init__(optimization_uid=optimization_uid, metadata=metadata)
        self.sessioning = sessioning

    @classmethod
    def Agent(
        cls,
        sensors: Sequence[Sensor],
        dofs: Sequence[RangeDOF],
        objectives: Sequence[Objective],
        evaluation_function: EvaluationFunction[TUid],
        acquisition_plan: AcquisitionPlan[TUid] | None = None,
        optimizer: SCP = SCP.Default,
        # dof_constraints: Sequence[DOFConstraint] | None = None,  #implemented in future iterations? make to match ax?
        # outcome_constraints: Sequence[OutcomeConstraint] | None = None,
        *,
        max_iter: int | None = None,
        eps: float | None = None,
        scale: Sequence[float] | float | None = None,
        threads: int | None = None,
        **kwargs: Any,
    ):
        """
        An emcompassing interface to provide strong interoperability with Ax agent formalism.

        Parameters
        ----------
        sensors : Sequence[Sensor]
            The sensors to use for acquisition. These should be the minimal set
            of sensors that are needed to compute the objectives.
        dofs : Sequence[DOF]
            The degrees of freedom that the agent can control, which determine the search space.
        objectives : Sequence[Objective]
            The objectives which the agent will try to optimize.
        evaluation_function : EvaluationFunction
            The function to evaluate acquired data and produce outcomes.
        acquisition_plan : AcquisitionPlan | None, optional
            The acquisition plan to use for acquiring data from the beamline. If not provided,
            :func:`bluesky_adaptive.plans.default_acquire` will be used.
        optimizer : SCP, optional
            The SciPy optimizer to run. Default is :attr:`SCP.Default`.
        max_iter : int | None, optional
            Passed to :class:`ScipyCFG` as ``max_iter``.
        eps : float | None, optional
            Passed to :class:`ScipyCFG` as ``eps``.
        scale : Sequence[float] | float | None, optional
            Passed to :class:`ScipyCFG` as ``rescale``.
        threads : int | None, optional
            Passed to :class:`ScipyCFG` as ``threads``.
        **kwargs : Any
            Passed to :class:`Scipy`: ``optimization_uid``, ``metadata``, ``timeout``, ``resiliance`` and
            ``sessioning``. Any other keyword raises :class:`TypeError`.

        See Also
        --------
        bluesky_adaptive.ax.Agent

        Notes
        -----
        This is a nearly drop in replacement for Ax agent sans dof + outcome constraints and checkpointing


        """  # noqa: D401
        if len(objectives) > 1:
            raise ValueError("Multiple Objectives are not supported for gradient optimizers")
        config = ScipyCFG(
            dofs=dofs,
            objective=objectives[0],
            optimizer=optimizer,
            max_iter=max_iter,
            eps=eps,
            rescale=scale,
            threads=threads,
        )
        return cls(sensors, config, evaluation_function, acquisition_plan, **kwargs)

    @property
    def sensors(self) -> Sequence[Sensor]:
        """The sensors used for data acquisition."""
        return self._sensors

    @property
    def actuators(self) -> Sequence[Actuator]:
        """The actuators that control the degrees of freedom."""
        return self._actuators

    @property
    def evaluation_function(self) -> EvaluationFunction[TUid]:
        """The function used to evaluate acquired data and produce outcomes."""
        return self._evaluation_function

    @property
    def acquisition_plan(self) -> AcquisitionPlan[TUid] | None:
        """The acquisition plan for acquiring data, or ``None`` if using the default."""
        return self._acquisition_plan

    def to_optimization_problem(self) -> OptimizationProblem[TUid]:
        """
        Construct an optimization problem from the Scipy Base class.

        Creates an immutable :class:`bluesky_adaptive.protocols.OptimizationProblem` that
        encapsulates all components needed for optimization. This is typically
        used internally by optimization plans.

        Returns
        -------
        OptimizationProblem
            An immutable optimization problem that can be deployed via Bluesky.

        See Also
        --------
        bluesky_adaptive.protocols.OptimizationProblem : The optimization problem dataclass.
        bluesky_adaptive.plans.optimize : Uses the optimization problem to run optimization.
        """
        return OptimizationProblem(
            optimizer=self.optimizer,
            actuators=self._actuators,
            sensors=self._sensors,
            evaluation_function=self._evaluation_function,
            acquisition_plan=self._acquisition_plan,
            optimization_uid=self._optimization_uid,
        )

    def suggest(self, num_points: int = 1) -> Sequence[Mapping]:
        """
        Get the next point(s) to evaluate in the search space.

        Returns the points SciPy is currently waiting to have evaluated. Each suggestion
        includes an "_id" key for tracking. Once SciPy has finished, the only suggestion
        is its final point.

        Parameters
        ----------
        num_points : int, optional
            The maximum number of points to suggest. Default is 1. SciPy requests one
            point at a time unless ``threads`` is set in the configuration, so fewer
            points than requested may be returned.

        Returns
        -------
        Sequence[Mapping]
            At most ``num_points`` mappings, each containing a parameterization of a point
            to evaluate next. Each mapping includes an "_id" key for identification.
        """
        return self.optimizer.suggest(num_points)

    def ingest(self, points: Sequence[Mapping]) -> None:
        """
        Ingest evaluation results into the optimizer.

        Passes each outcome to the SciPy evaluation waiting on that point. Only points
        returned by :meth:`suggest` can be ingested; external data is not supported.

        Parameters
        ----------
        points : Sequence[Mapping]
            A sequence of mappings containing outcomes for a trial. Each mapping must
            contain the "_id" key of a suggested point and the objective value.

        Raises
        ------
        KeyError
            A point has no "_id" key or no objective value.
        ValueError
            A point's "_id" is not one SciPy is waiting on and ``resiliance`` is False.
            With ``resiliance`` True such points are skipped.

        Notes
        -----
        This method is typically called automatically by :meth:`optimize`. Manual usage
        is only needed for custom workflows.
        """
        self.optimizer.ingest(points)

    def optimize(self, iterations=10, n_points=1, *, acquisition_plan_kwargs: Mapping[str, Any] | None = None):
        """
        Optimization plan wrapper used by the agent interface.

        Each iteration evaluates at most ``n_points`` points: the points SciPy has pending, which is one unless
        ``threads`` is set in the configuration. ``acquisition_plan_kwargs`` are passed to the acquisition plan on
        every acquisition of this call.
        """
        if self.optimizer.final is not None:
            self.config.initial = self.optimizer.final.x
            self.optimizer = InteractiveOptimizer(self.inner, timeout=self.timeout)
            self.optimizer.force_resiliance = self.resiliance
        optimize_plan = optimize(
            self.to_optimization_problem(),
            iterations=iterations,
            n_points=n_points,
            callbacks=self._callbacks,
            metadata=self._metadata,
            acquisition_plan_kwargs=acquisition_plan_kwargs,
        )
        if self.sessioning:
            with self.optimizer:
                yield from optimize_plan
        else:
            yield from optimize_plan

    def get_best_points(self) -> list[tuple[Any, Mapping, Mapping]]:
        """
        Get the best point found during optimization.

        SciPy optimizes a single objective, so the list holds one point: SciPy's final
        result if it has finished, otherwise its best intermediate result.

        Returns
        -------
        list[tuple[int, TParameterization, TOutcome]]
            A one-element list holding a tuple of:
              - trial index (int)
              - parameter values (dict)
              - objective value (dict)

        Raises
        ------
        ValueError
            SciPy has not reported a result yet.

        See Also
        --------
        navigate_to_best : Plan stub to move actuators to a best point.
        """
        return self.optimizer.get_best_points()
