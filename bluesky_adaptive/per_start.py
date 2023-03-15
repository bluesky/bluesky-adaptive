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
import itertools
import uuid
from queue import Empty, Queue

import bluesky.plan_stubs as bps
import bluesky.plans as bp
import bluesky.preprocessors as bpp
from event_model import RunRouter

from .recommendations import NoRecommendation
from .utils import extract_event_page


def recommender_factory(adaptive_obj, independent_keys, dependent_keys, *, max_count=10, queue=None):
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

    poisoned = None

    def callback(name, doc):
        nonlocal poisoned
        # TODO handle multi-stream runs with more than 1 event!
        if name == "start":
            if doc["batch_count"] > max_count:
                queue.put(None)
                poisoned = True
                return
            else:
                poisoned = False

        if name == "event_page":
            if poisoned:
                return
            independent, measurement = extract_event_page(independent_keys, dependent_keys, payload=doc["data"])
            adaptive_obj.tell_many(independent, measurement)
            # pull the next point out of the adaptive API
            try:
                next_point = adaptive_obj.ask(1)
            except NoRecommendation:
                queue.put(None)
            else:
                queue.put({k: v for k, v in zip(independent_keys, next_point)})

    rr = RunRouter([lambda name, doc: ([callback], [])])
    return rr, queue


def adaptive_plan(dets, first_point, *, to_recommender, from_recommender, md=None, take_reading=bp.count):
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
