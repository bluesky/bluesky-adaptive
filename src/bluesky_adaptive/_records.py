"""Record an optimization as a Bluesky run composed with event-model, outside the RunEngine."""

import copy
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, cast

import numpy as np
from bluesky.utils import RequestAbort, RequestStop
from event_model import ComposeEvent, DataKey, compose_run

from .protocols import ID_KEY, DocumentCallback

SUGGESTION_ID_FIELD: Literal["adaptive_suggestion_id"] = "adaptive_suggestion_id"
ITERATION_FIELD: Literal["adaptive_iteration"] = "adaptive_iteration"
ACQUISITION_UID_FIELD: Literal["adaptive_acquisition_uid"] = "adaptive_acquisition_uid"
_RESERVED_PREFIX = "adaptive_"


def _event_value(value: Any) -> Any:
    """Return ``value`` as a plain Python value, with a one-element sequence replaced by its element."""
    if isinstance(value, np.ndarray | np.generic):
        value = value.tolist()
    if isinstance(value, list | tuple) and len(value) == 1:
        return value[0]
    return value


def _infer_data_key(value: Any) -> DataKey:
    """Infer the data key of an event value."""
    array = np.asarray(value)
    # The first recorded value defines the descriptor, so reserve enough string space for UUIDs.
    dtype_numpy = np.promote_types(array.dtype, np.dtype("<U36")).str if array.dtype.kind == "U" else array.dtype.str
    if array.ndim > 0:
        return DataKey(source="bluesky_adaptive", dtype="array", shape=list(array.shape), dtype_numpy=dtype_numpy)
    dtype = "number" if isinstance(array.item(), int | float) else "string"
    return DataKey(source="bluesky_adaptive", dtype=dtype, shape=[], dtype_numpy=dtype_numpy)


class RunRecorder:
    """
    Records an optimization as one Bluesky run composed with event-model, without a RunEngine.

    The start document is sent to the callbacks on construction. Each evaluated point becomes one
    ``primary`` event holding its suggestion ID, iteration, and acquisition UID, followed by its
    parameters and outcomes. The descriptor's ``object_keys`` name the parameter fields under
    ``"parameters"`` and the outcome fields under ``"outcomes"``.

    If a callback raises on the start document, the callbacks that already received it get a ``"fail"``
    stop document and the exception propagates.

    Parameters
    ----------
    callbacks : Iterable[DocumentCallback]
        The callbacks that receive the documents.
    metadata : Mapping[str, Any]
        Start document metadata.
    """

    def __init__(self, *, callbacks: Iterable[DocumentCallback], metadata: Mapping[str, Any]) -> None:
        bundle = compose_run(metadata=dict(metadata))
        self._compose_descriptor = bundle.compose_descriptor
        self._compose_stop = bundle.compose_stop
        self._compose_event: ComposeEvent | None = None
        self._fields: set[str] = set()
        self._stopped = False
        self._callbacks: list[DocumentCallback] = []
        start = cast(dict[str, Any], bundle.start_doc)
        for callback in callbacks:
            try:
                callback("start", start)
            except BaseException:
                self.stop("fail", f"Callback {callback!r} failed on the start document")
                raise
            self._callbacks.append(callback)

    def _emit(self, name: str, doc: Mapping[str, Any]) -> None:
        document = cast(dict[str, Any], doc)
        for callback in self._callbacks:
            callback(name, document)

    def record(
        self, iteration: int, acquisition_uid: Any, suggestions: Sequence[Mapping], outcomes: Sequence[Mapping]
    ) -> None:
        """
        Record one evaluated step as one event per suggestion, in suggestion order.

        The first recorded step defines the fields and sends the descriptor. A step that does not
        match those fields raises before any of its documents are sent. The outcomes must already be
        validated against the suggestions with ``utils._validate_outcomes``.

        Parameters
        ----------
        iteration : int
            The loop iteration, or ``-1`` outside an iterating loop.
        acquisition_uid : Any
            The value returned by the acquisition plan. Recorded as is if it is a string, otherwise as
            its ``repr``.
        suggestions : Sequence[Mapping]
            The evaluated suggestions.
        outcomes : Sequence[Mapping]
            The outcomes, matched to suggestions by ``"_id"``.

        Raises
        ------
        ValueError
            If a parameter or outcome name starts with ``"adaptive_"``, a parameter and an outcome share
            a name, or a point's fields differ from the fields of the first recorded step.
        """
        outcome_by_id = {outcome[ID_KEY]: outcome for outcome in outcomes}
        acquisition_value = acquisition_uid if isinstance(acquisition_uid, str) else repr(acquisition_uid)
        rows: list[dict[str, Any]] = []
        object_keys: dict[str, list[str]] = {}
        for suggestion in suggestions:
            parameters = {k: _event_value(v) for k, v in suggestion.items() if k != ID_KEY}
            results = {k: _event_value(v) for k, v in outcome_by_id[suggestion[ID_KEY]].items() if k != ID_KEY}
            reserved = sorted(k for k in (*parameters, *results) if k.startswith(_RESERVED_PREFIX))
            if reserved:
                raise ValueError(
                    f"Parameter and outcome names must not start with {_RESERVED_PREFIX!r}, which is reserved for "
                    f"record fields. Got {reserved!r}."
                )
            shared = sorted(parameters.keys() & results.keys())
            if shared:
                raise ValueError(f"A parameter and an outcome must not share a name. Got {shared!r}.")
            if not object_keys:
                object_keys = {"parameters": list(parameters), "outcomes": list(results)}
            rows.append(
                {
                    SUGGESTION_ID_FIELD: str(suggestion[ID_KEY]),
                    ITERATION_FIELD: iteration,
                    ACQUISITION_UID_FIELD: acquisition_value,
                    **parameters,
                    **results,
                }
            )
        if not rows:
            return

        fields = self._fields or set(rows[0])
        for row in rows:
            if set(row) != fields:
                raise ValueError(
                    f"Every recorded point must have the fields {sorted(fields)!r}. "
                    f"Suggestion {row[SUGGESTION_ID_FIELD]!r} has {sorted(row)!r}."
                )

        descriptor = None
        if self._compose_event is None:
            bundle = self._compose_descriptor(
                name="primary",
                data_keys={key: _infer_data_key(value) for key, value in rows[0].items()},
                object_keys=object_keys,
            )
            descriptor = bundle.descriptor_doc
            self._compose_event = bundle.compose_event
            self._fields = fields
        now = time.time()
        events = [self._compose_event(data=row, timestamps=dict.fromkeys(row, now), time=now) for row in rows]
        if descriptor is not None:
            # The event composer validates later events against this descriptor, and consumers such as
            # TiledWriter modify the descriptor they receive.
            self._emit("descriptor", copy.deepcopy(descriptor))
        for event in events:
            self._emit("event", event)

    def stop(self, exit_status: Literal["success", "abort", "fail"] = "success", reason: str = "") -> None:
        """
        Send the stop document. Later calls do nothing.

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
        Send the stop document for the exception that ends the optimization loop.

        :class:`bluesky.utils.RequestStop` stops with ``"success"``. :class:`bluesky.utils.RequestAbort` and
        exceptions that are not an :class:`Exception` (``GeneratorExit``, ``KeyboardInterrupt``) stop with
        ``"abort"``. Any other exception stops with ``"fail"``, with a reason naming the iteration and the
        suggestion IDs.

        Parameters
        ----------
        error : BaseException
            The exception that ends the loop.
        iteration : int
            The iteration that raised, or ``-1`` outside an iterating loop.
        suggestions : Sequence[Mapping]
            The suggestions of that iteration.
        """
        if isinstance(error, RequestStop):
            self.stop("success", "Stop requested")
        elif isinstance(error, RequestAbort) or not isinstance(error, Exception):
            self.stop("abort", f"Aborted: {type(error).__name__}")
        else:
            suggestion_ids = [suggestion.get(ID_KEY) for suggestion in suggestions]
            self.stop(
                "fail",
                f"Iteration {iteration} with suggestion IDs {suggestion_ids!r} failed: {type(error).__name__}: {error}",
            )
