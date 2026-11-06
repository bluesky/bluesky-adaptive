"""Record runs: one Bluesky run per optimization loop call, composed with event-model."""

import copy
import inspect
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, cast

import numpy as np
from bluesky.protocols import Configurable
from bluesky.utils import RequestAbort, RequestStop
from event_model import ComposeEvent, DataKey, compose_run

from .protocols import ID_KEY, Describable, DocumentCallback, Optimizer

ID_FIELD: Literal["adaptive_id"] = "adaptive_id"
"""Record event field holding the suggestion ID, as a string."""
ITERATION_FIELD: Literal["adaptive_iteration"] = "adaptive_iteration"
"""Record event field holding the loop iteration, or ``-1`` for one-shot plans."""
ACQUISITION_FIELD: Literal["adaptive_acquisition"] = "adaptive_acquisition"
"""Record event field holding the acquisition UID; non-scalar UIDs are stored as their ``repr``."""


def _with_optimization_uid(md: Mapping[str, Any] | None, optimization_uid: str) -> dict[str, Any]:
    """Return a copy of ``md`` with ``md["adaptive"]["optimization_uid"]`` set, keeping other ``"adaptive"`` entries."""
    md = dict(md or {})
    md["adaptive"] = {**md.get("adaptive", {}), "optimization_uid": optimization_uid}
    return md


def _infer_data_key(value: Any) -> DataKey:
    """Infer the data key of a record event value."""
    numpy_array = np.array(value)
    # The first recorded value defines the descriptor, so reserve enough string space for UUIDs.
    dtype_numpy = (
        np.promote_types(numpy_array.dtype, np.dtype("<U36")).str if numpy_array.dtype.kind == "U" else numpy_array.dtype.str
    )
    if numpy_array.ndim > 0:
        dtype = "array"
        shape = list(numpy_array.shape)
    else:
        shape = []
        dtype = "number" if isinstance(numpy_array.item(), int | float) else "string"
    return DataKey(source="bluesky_adaptive", dtype=dtype, shape=shape, dtype_numpy=dtype_numpy)


def _event_values(point: Mapping[str, Any]) -> dict[str, Any]:
    """Return the point's fields other than its ID, with NumPy values converted to Python values."""
    return {k: v.tolist() if isinstance(v, np.ndarray | np.generic) else v for k, v in point.items() if k != ID_KEY}


class RecordRun:
    """
    The record run of one optimization loop call.

    Composes a Bluesky run with event-model, without a RunEngine, and passes every document to the
    callbacks synchronously, in order. The run has one ``"primary"`` stream with one event per
    evaluated point. Each event holds the bookkeeping fields ``adaptive_id``, ``adaptive_iteration``,
    and ``adaptive_acquisition``, plus the point's parameters and outcomes. The descriptor's
    ``object_keys`` list the parameter fields under ``"parameters"`` and the outcome fields under
    ``"outcomes"``.

    The start document is emitted on construction. It carries ``metadata`` plus
    ``{"adaptive": {"optimization_uid": ...}}``.

    Parameters
    ----------
    optimizer : Optimizer
        The optimizer whose points are recorded. A :class:`~bluesky_adaptive.protocols.Describable`
        optimizer declares the parameter and outcome fields; otherwise the first recorded point
        defines them. The configuration of a :class:`bluesky.protocols.Configurable` optimizer is
        recorded in the descriptor's ``configuration`` under ``"optimizer"``.
    callbacks : Iterable[DocumentCallback]
        The callbacks that receive the documents. The iterable is read once, here.
    metadata : Mapping[str, Any]
        Start document metadata.
    optimization_uid : str | None, optional
        Identifier shared by every run of one optimization. If None, a new UUID4 is generated.

    Raises
    ------
    TypeError
        If the optimizer's ``read_configuration()`` or ``describe_configuration()`` returns an awaitable.
    """

    def __init__(
        self,
        optimizer: Optimizer,
        *,
        callbacks: Iterable[DocumentCallback],
        metadata: Mapping[str, Any],
        optimization_uid: str | None = None,
    ) -> None:
        self._callbacks = tuple(callbacks)
        self._optimization_uid = optimization_uid if optimization_uid is not None else str(uuid.uuid4())
        self._declared = dict(optimizer.describe()) if isinstance(optimizer, Describable) else None
        if isinstance(optimizer, Configurable):
            readings = optimizer.read_configuration()
            described = optimizer.describe_configuration()
            if inspect.isawaitable(readings) or inspect.isawaitable(described):
                raise TypeError(
                    "Optimizer configuration is read synchronously; "
                    "read_configuration() and describe_configuration() must not return awaitables."
                )
            self._configuration: dict[str, Any] = {
                "optimizer": {
                    "data": {k: r["value"] for k, r in readings.items()},
                    "timestamps": {k: r["timestamp"] for k, r in readings.items()},
                    "data_keys": dict(described),
                }
            }
        else:
            self._configuration = {}
        # Shared with the composers; restored when a step fails to compose, so the stop
        # document counts only emitted events.
        self._event_counters: dict[str, int] = {}
        bundle = compose_run(
            metadata=_with_optimization_uid(metadata, self._optimization_uid), event_counters=self._event_counters
        )
        self._compose_descriptor = bundle.compose_descriptor
        self._compose_stop = bundle.compose_stop
        self._compose_event: ComposeEvent | None = None
        self._stopped = False
        self._emit("start", bundle.start_doc)

    @property
    def optimization_uid(self) -> str:
        """Identifier shared by every run of this optimization."""
        return self._optimization_uid

    @property
    def stopped(self) -> bool:
        """Whether the stop document has been emitted."""
        return self._stopped

    def _emit(self, name: str, doc: Mapping[str, Any]) -> None:
        # event-model documents are TypedDicts, which are plain dicts at runtime.
        document = cast(dict[str, Any], doc)
        for callback in self._callbacks:
            callback(name, document)

    def record(self, iteration: int, acquisition: Any, suggestions: Sequence[Mapping], outcomes: Sequence[Mapping]) -> None:
        """
        Record one evaluated step: one event per suggestion, in suggestion order.

        The first recorded step also emits the ``"primary"`` descriptor. Every event of the step is
        composed before any is emitted, so a step that fails validation emits nothing.

        The caller must have validated the step with ``utils._validate_outcomes(outcomes, suggestions)``.

        Parameters
        ----------
        iteration : int
            The loop iteration, or ``-1`` for one-shot plans.
        acquisition : Any
            The acquisition UID returned by the acquisition plan. Values that are not ``str``,
            ``int``, or ``float`` are recorded as their ``repr``.
        suggestions : Sequence[Mapping]
            The evaluated suggestions.
        outcomes : Sequence[Mapping]
            The outcomes, matched to suggestions by ``"_id"``.

        Raises
        ------
        ValueError
            If a parameter or outcome name starts with ``"adaptive_"``, or a parameter and an
            outcome share a name.
        event_model.EventModelValidationError, KeyError
            Raised by event-model if a point lacks a descriptor field or has a field the descriptor lacks.
        """
        outcome_by_id = {o[ID_KEY]: o for o in outcomes}
        acquisition_value = acquisition if isinstance(acquisition, str | int | float) else repr(acquisition)
        rows: list[dict[str, Any]] = []
        object_keys: dict[str, list[str]] = {}
        for suggestion in suggestions:
            parameters = _event_values(suggestion)
            results = _event_values(outcome_by_id[suggestion[ID_KEY]])
            reserved = [k for k in (*parameters, *results) if k.startswith("adaptive_")]
            if reserved:
                raise ValueError(
                    "Parameter and outcome names must not start with 'adaptive_', which is reserved for record "
                    f"bookkeeping fields. Got {sorted(reserved)!r}."
                )
            shared = parameters.keys() & results.keys()
            if shared:
                raise ValueError(f"A parameter and an outcome must not share a name. Got {sorted(shared)!r}.")
            if not rows:
                object_keys = {"parameters": list(parameters), "outcomes": list(results)}
            rows.append(
                {
                    ID_FIELD: str(suggestion[ID_KEY]),
                    ITERATION_FIELD: iteration,
                    ACQUISITION_FIELD: acquisition_value,
                    **parameters,
                    **results,
                }
            )
        if not rows:
            return

        counters_before = dict(self._event_counters)
        compose_event = self._compose_event
        descriptor_doc = None
        try:
            if compose_event is None:
                first = rows[0]
                if self._declared is not None:
                    fields = self._declared
                else:
                    fields = {k: _infer_data_key(first[k]) for k in (*object_keys["parameters"], *object_keys["outcomes"])}
                data_keys = {k: _infer_data_key(first[k]) for k in (ID_FIELD, ITERATION_FIELD, ACQUISITION_FIELD)}
                descriptor_bundle = self._compose_descriptor(
                    name="primary",
                    data_keys={**data_keys, **fields},
                    object_keys=object_keys,
                    configuration=self._configuration,
                )
                descriptor_doc = descriptor_bundle.descriptor_doc
                compose_event = descriptor_bundle.compose_event
            now = time.time()
            events = [compose_event(data=row, timestamps=dict.fromkeys(row, now), time=now) for row in rows]
        except BaseException:
            self._event_counters.clear()
            self._event_counters.update(counters_before)
            raise
        self._compose_event = compose_event
        if descriptor_doc is not None:
            # ComposeEvent validates later events against descriptor_doc, and consumers such as
            # TiledWriter modify the descriptor they receive.
            self._emit("descriptor", copy.deepcopy(descriptor_doc))
        for event in events:
            self._emit("event", event)

    def stop(self, exit_status: Literal["success", "abort", "fail"] = "success", reason: str = "") -> None:
        """
        Emit the stop document. Only the first stop takes effect; later calls do nothing.

        Parameters
        ----------
        exit_status : {"success", "abort", "fail"}, optional
            The run's exit status.
        reason : str, optional
            Why the run stopped.
        """
        if self._stopped:
            return
        self._stopped = True
        self._emit("stop", self._compose_stop(exit_status=exit_status, reason=reason))

    def stop_for(self, error: BaseException, iteration: int, suggestions: Sequence[Mapping]) -> None:
        """
        Emit the stop document for the exception that ends the optimization loop.

        :class:`bluesky.utils.RequestStop` stops with ``"success"``. :class:`bluesky.utils.RequestAbort`
        and exceptions that are not :class:`Exception` (``GeneratorExit``, ``KeyboardInterrupt``)
        stop with ``"abort"``. Any other exception stops with ``"fail"`` and a reason naming the
        iteration and suggestion IDs.

        Parameters
        ----------
        error : BaseException
            The exception that ends the loop.
        iteration : int
            The iteration that raised, or ``-1`` for one-shot plans.
        suggestions : Sequence[Mapping]
            The suggestions of that iteration.
        """
        if isinstance(error, RequestStop):
            self.stop("success", "Stop requested")
        elif isinstance(error, RequestAbort) or not isinstance(error, Exception):
            self.stop("abort", f"Aborted: {type(error).__name__}")
        else:
            suggestion_ids = [s.get(ID_KEY) for s in suggestions]
            self.stop(
                "fail",
                f"Iteration {iteration} with suggestion IDs {suggestion_ids!r} failed: {type(error).__name__}: {error}",
            )
