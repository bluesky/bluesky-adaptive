"""
Per-Start adaptive integration.

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
import uuid
import itertools
from queue import Queue, Empty

import bluesky.preprocessors as bpp
import bluesky.plan_stubs as bps
import bluesky.plans as bp

from .recommendations import NoRecommendation

from bluesky_live.bluesky_run import BlueskyRun, DocumentCache
import event_model


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

    rr = event_model.RunRouter([factory])
    return rr


def recommender_factory(
    adaptive_obj,
    independent_keys,
    dependent_keys,
    *,
    stream_name="primary",
    max_count=10,
    queue=None
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
    adaptive_object : adaptive.BaseLearner
        The recommendation engine

    stream_name: str
        Name of event stream to grab keys from

    independent_keys : List[str]
        The names of the independent keys in the events

    dependent_keys : List[str]
        The names of the dependent keys in the events

    max_count : int, optional
        The maximum number of measurements to take before poisoning the queue.

    queue : Queue, optional
        The communication channel for the callback to feedback to the plan.
        If not given, a new queue will be created.

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

    def tell_recommender(event):

        run = event.run
        print(run)

        ds = run[stream_name].read()
        (independent_key,) = independent_keys
        (dependent_key,) = dependent_keys
        independent = ds[independent_key]
        measurement = ds[dependent_key].median()
        adaptive_obj.tell_many(independent, (measurement, ))
        # pull the next point out of the adaptive API
        try:
            next_point = adaptive_obj.ask(1)
        except NoRecommendation:
            queue.put(None)
        else:
            queue.put({k: v for k, v in zip(independent_keys, next_point)})

    def tell_recommender_on_completion(run):
        run.events.completed.connect(tell_recommender)

    return stream_documents_into_runs(tell_recommender_on_completion), queue


def adaptive_plan(
    dets,
    first_point,
    *,
    to_recommender,
    from_recommender,
    md=None,
    take_reading=bp.count
):
    """
    Execute an adaptive scan using an inter-run recommendation engine.

    Parameters
    ----------
    dets : List[OphydObj]
       The detector to read at each point.  The dependent keys that the
       recommendation engine is looking for must be provided by these
       devices.

    first_point : Dict[Settable, Any]
       The first point of the scan.  The motors that will be scanned
       are extracted from the keys.  The independent keys that the
       recommendation engine is looking for / returning must be provided
       by these devices.

    to_recommender : Callable[document_name: str, document: dict]
       This is the callback that will be registered to the RunEngine.

       The expected contract is for each event it will place either a
       dict mapping independent variable to recommended value or None.

       This plan will either move to the new position and take data
       if the value is a dict or end the run if `None`

    from_recommender : Queue
       The consumer side of the Queue that the recommendation engine is
       putting the recommendations onto.

    md : dict[str, Any], optional
       Any extra meta-data to put in the Start document

    take_reading : plan, optional
        function to do the actual acquisition ::

           def take_reading(dets, md={}):
                yield from ...

        Callable[List[OphydObj], Optional[Dict[str, Any]]] -> Generator[Msg], optional

        This plan must generate exactly 1 Run

        Defaults to `bluesky.plans.count`

    """
    # extract the motors
    motors = list(first_point.keys())
    # convert the first_point variable to from we will be getting
    # from queue
    first_point = {m.name: v for m, v in first_point.items()}

    _md = {"batch_id": str(uuid.uuid4())}

    _md.update(md or {})

    @bpp.subs_decorator(to_recommender)
    def gp_inner_plan():
        # drain the queue in case there is anything left over from a previous
        # run
        while True:
            try:
                from_recommender.get(block=False)
            except Empty:
                break
        uids = []
        next_point = first_point
        for j in itertools.count():
            # this assumes that m.name == the key in Event
            target = {m: next_point[m.name] for m in motors}
            motor_position_pairs = itertools.chain(*target.items())

            yield from bps.mov(*motor_position_pairs)
            uid = yield from take_reading(dets + motors, md={**_md, "batch_count": j})
            uids.append(uid)

            next_point = from_recommender.get(timeout=1)
            if next_point is None:
                return

        return uids

    return (yield from gp_inner_plan())
