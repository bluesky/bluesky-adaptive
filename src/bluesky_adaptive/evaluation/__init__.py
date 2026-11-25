"""Helpers for building evaluation functions."""

import functools
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from ..protocols import ID_KEY, EvaluationFunction, TUid

_TData = TypeVar("_TData")
_TRunUid = TypeVar("_TRunUid")


def per_suggestion(
    evaluate: Callable[[_TData], Mapping[str, Any]],
    fetch: Callable[[TUid], Mapping[Any, _TData]],
) -> EvaluationFunction[TUid]:
    """Build an evaluation function from a function of one suggestion's data.

    The evaluation function applies ``evaluate`` to each value of ``fetch(uid)`` and sets the outcome's ``"_id"`` to
    its key.

    Parameters
    ----------
    evaluate : Callable[[_TData], Mapping[str, Any]]
        Returns the outcome of one suggestion's data.
    fetch : Callable[[TUid], Mapping[Any, _TData]]
        Returns each suggestion's data keyed by suggestion ID, e.g.
        :meth:`RunEventBuffer.pop <bluesky_adaptive.evaluation.documents.RunEventBuffer.pop>`.

    Returns
    -------
    EvaluationFunction[TUid]
        The evaluation function.

    Examples
    --------
    >>> from bluesky_adaptive.evaluation import per_suggestion
    >>> def score(data):
    ...     return {"objective": 10 * data["x"]}
    >>> evaluation_function = per_suggestion(score, lambda uid: {"a": {"x": 1.0}, "b": {"x": 2.0}})
    >>> evaluation_function("run-uid", [])
    [{'objective': 10.0, '_id': 'a'}, {'objective': 20.0, '_id': 'b'}]
    """

    @functools.wraps(evaluate)
    def evaluation_function(uid: TUid, suggestions: Sequence[Mapping]) -> list[dict[str, Any]]:
        return [{**evaluate(data), ID_KEY: suggestion_id} for suggestion_id, data in fetch(uid).items()]

    return evaluation_function


def per_run(evaluate: EvaluationFunction[_TRunUid], run_uid: Callable[[TUid], _TRunUid]) -> EvaluationFunction[TUid]:
    """Build an evaluation function from one that evaluates acquisitions by another UID, such as a Bluesky run UID.

    The evaluation function calls ``evaluate`` with ``run_uid(uid)`` in place of ``uid``.

    Parameters
    ----------
    evaluate : EvaluationFunction[_TRunUid]
        Evaluates the acquisition identified by the UID that ``run_uid`` returns.
    run_uid : Callable[[TUid], _TRunUid]
        Returns the UID ``evaluate`` takes for the evaluation function's UID, e.g.
        :meth:`QueueItemRuns.run_uid <bluesky_adaptive.evaluation.queueserver.QueueItemRuns.run_uid>`.

    Returns
    -------
    EvaluationFunction[TUid]
        The evaluation function.

    Examples
    --------
    >>> from bluesky_adaptive.evaluation import per_run
    >>> def evaluate_run(uid, suggestions):
    ...     return [{"_id": "a", "run": uid}]
    >>> evaluation_function = per_run(evaluate_run, {"item-1": "run-1"}.__getitem__)
    >>> evaluation_function("item-1", [])
    [{'_id': 'a', 'run': 'run-1'}]
    """

    @functools.wraps(evaluate)
    def evaluation_function(uid: TUid, suggestions: Sequence[Mapping]) -> Sequence[Mapping]:
        return evaluate(run_uid(uid), suggestions)

    return evaluation_function
