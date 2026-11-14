"""Helpers for building evaluation functions."""

import functools
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from ..protocols import ID_KEY, EvaluationFunction, TUid

_TData = TypeVar("_TData")


def per_suggestion(
    fetch: Callable[[TUid], Mapping[Any, _TData]],
) -> Callable[[Callable[[_TData], Mapping[str, Any]]], EvaluationFunction[TUid]]:
    """Build a decorator that turns a function of one suggestion's data into an evaluation function.

    The evaluation function applies the decorated function to each value of ``fetch(uid)`` and sets the outcome's
    ``"_id"`` to its key.

    Parameters
    ----------
    fetch : Callable[[TUid], Mapping[Any, _TData]]
        Returns each suggestion's data keyed by suggestion ID, e.g.
        :meth:`RunEventBuffer.pop <bluesky_adaptive.evaluation.documents.RunEventBuffer.pop>`.

    Returns
    -------
    Callable[[Callable[[_TData], Mapping[str, Any]]], EvaluationFunction[TUid]]
        Decorator that builds the evaluation function.

    Examples
    --------
    >>> from bluesky_adaptive.evaluation import per_suggestion
    >>> @per_suggestion(lambda uid: {"a": {"x": 1.0}, "b": {"x": 2.0}})
    ... def score(data):
    ...     return {"objective": 10 * data["x"]}
    >>> score("run-uid", [])
    [{'objective': 10.0, '_id': 'a'}, {'objective': 20.0, '_id': 'b'}]
    """

    def decorator(evaluate: Callable[[_TData], Mapping[str, Any]]) -> EvaluationFunction[TUid]:
        @functools.wraps(evaluate)
        def evaluation_function(uid: TUid, suggestions: Sequence[Mapping]) -> list[dict[str, Any]]:
            return [{**evaluate(data), ID_KEY: suggestion_id} for suggestion_id, data in fetch(uid).items()]

        return evaluation_function

    return decorator
