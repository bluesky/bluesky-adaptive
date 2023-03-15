"""
Per-Start adaptive integration, but triggered on the stop document.

These functions are for integrating adaptive logic that works
between runs.  The decision making process can expect to consume a
full run before having to make a recommendation about what to do next.
This may be desirable if there is a major time miss-match between the
computation and the experiment, of the data collection is not amenable
to streaming analysis, or the natural structure of the experiment
dictates.

This corresponds to a "middle" scale of adaptive integration into
data collection.
"""
from queue import Queue

import event_model
from bluesky_live.bluesky_run import BlueskyRun, DocumentCache
from bluesky_widgets.models.utils import call_or_eval
from ophyd.sim import NumpySeqHandler

from .recommendations import NoRecommendation


def stream_documents_into_runs(add_run):
    """
    Convert a flat stream of documents to "live" BlueskyRuns.

    Parameters
    ----------
    add_run : callable
        This will be called as ``add_run(run: BlueskyRun)`` each time a 'start'
        document is received.

    Returns
    -------
    callback : callable
        This should be subscribed to a callback registry that calls it like
        ``callback(name, doc)``.

    Examples
    --------

    This is used for connecting something that emits a flat stream of documents
    to something that wants to receive BlueskyRuns.

    Append to a plain list.

    >>> from bluesky import RunEngine
    >>> RE = RunEngine()
    >>> runs = []
    >>> RE.subscribe(stream_documents_into_runs(runs.append))

    Or, more usefully to an observable list.

    >>> from bluesky_widgets.models.utils import RunList
    >>> runs = RunList()
    >>> RE.subscribe(stream_documents_into_runs(runs.append))

    Add runs to a model with an ``add_run`` method. For example, it might be a
    model that generates figures.

    >>> from bluesky_widgets.models.plot_builders import AutoLines
    >>> model = AutoLines()

    >>> RE.subscribe(stream_documents_into_runs(model.add_run))
    """

    def factory(name, doc):
        dc = DocumentCache()

        def build_and_add_run(event):
            run = BlueskyRun(dc)
            add_run(run)

        dc.events.started.connect(build_and_add_run)
        return [dc], []

    rr = event_model.RunRouter([factory], handler_registry={"NPY_SEQ": NumpySeqHandler})
    return rr


def recommender_factory(
    *,
    adaptive_obj,
    independent_keys,
    dependent_keys,
    target_keys,
    stream_names=("primary",),
    max_count=10,
    queue=None,
    target_transforms=None
):
    """
    Generate the callback and queue for an Adaptive API backed reccomender.

    This recommends a fixed step size independent of the measurement.

    For each Run (aka Start) that the callback sees it will place
    either a recommendation or `None` into the queue.  Recommendations
    will be of a dict mapping the independent_keys to the recommended
    values and should be interpreted by the plan as a request for more
    data.  A `None` placed in the queue should be interpreted by the
    plan as in instruction to terminate the run.

    The StartDocuments in the stream must contain the key
    ``'batch_count'``.


    Parameters
    ----------
    adaptive_obj : adaptive.BaseLearner
        The recommendation engine.  Must implement

    independent_keys : List[String | Callable]
        Each value must be a stream name, field name, a valid Python
        expression, or a callable. The signature of the callable may include
        any valid Python identifiers provideed by :func:`construct_namespace`
        or the user-provided namespace parmeter below. See examples.

    dependent_keys : List[String | Callable]
        Each value must be a stream name, field name, a valid Python
        expression, or a callable. The signature of the callable may include
        any valid Python identifiers provideed by :func:`construct_namespace`
        or the user-provided namespace parmeter below. See examples.

    target_keys : List[String]
        Keys passed back to the plan, must be the same length as
        the return of `adaptive_obj.ask(1)`

    stream_names : Tuple[String], default ("primary",)
        The streams to be offered to the

    max_count : int, optional
        The maximum number of measurements to take before poisoning the queue.

    queue : Queue, optional
        The communication channel for the callback to feedback to the plan.
        If not given, a new queue will be created.

    target_transforms : Dict[String, Callable], optional
        Transforms to be applied to the values from ask before returning
        to the run engine.  This can be useful handling trivial coordinate
        transformations.

    Returns
    -------
    callback : Callable[str, dict]
        This function must be subscribed to RunEngine to receive the
        document stream.

    queue : Queue
        The communication channel between the callback and the plan.  This
        is always returned (even if the user passed it in).

    """

    if queue is None:
        queue = Queue()

    if target_transforms is None:
        target_transforms = {}

    def tell_recommender(event):
        run = event.run

        independent_map = call_or_eval({j: val for j, val in enumerate(independent_keys)}, run, stream_names)
        dependent_map = call_or_eval({j: val for j, val in enumerate(dependent_keys)}, run, stream_names)

        independent = tuple(independent_map[j] for j in range(len(independent_keys)))
        measurement = tuple(dependent_map[j] for j in range(len(dependent_keys)))
        adaptive_obj.tell_many(independent, measurement)
        # pull the next point out of the adaptive API
        try:
            next_point = adaptive_obj.ask(1)
        except NoRecommendation:
            queue.put(None)
        else:
            if run.metadata["start"].get("batch_count") >= max_count:
                queue.put(None)
            else:
                queue.put({k: target_transforms.get(k, lambda x: x)(v) for k, v in zip(target_keys, next_point)})

    def tell_recommender_on_completion(run):
        run.events.completed.connect(tell_recommender)

    return stream_documents_into_runs(tell_recommender_on_completion), queue
