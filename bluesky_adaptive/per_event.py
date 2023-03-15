"""
Per-Event adaptive integration.

These tools are for integrating the adaptive logic inside of a run.
They are expected to get single events and provide feedback to drive
the plan based in that information.  This is useful when the computation
time to make the decision is short compared to acquisition / movement time
and the computation is amenable to streaming analysis.

This is a "fine" grained integration of the adaptive logic into data acquisition.

"""

import itertools
from queue import Queue

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from event_model import RunRouter

from .recommendations import NoRecommendation
from .utils import extract_event_page


def recommender_factory(recommender, independent_keys, dependent_keys, *, max_count=10, queue=None):
    """
    Generate the callback and queue for gpCAM integration.

    For each Event that the callback sees it will place either a
    recommendation or `None` into the queue.  Recommendations will be
    of a dict mapping the independent_keys to the recommended values and
    should be interpreted by the plan as a request for more data.  A `None`
    placed in the queue should be interpreted by the plan as in instruction to
    terminate the run.


    Parameters
    ----------
    pgcam_object : gpCAM
        The gpcam recommendation engine

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

    # TODO handle multi-stream runs!
    def callback(name, doc):
        if name == "event_page":
            if doc["seq_num"][-1] > max_count:
                # if at max number of points poison the queue and return early
                queue.put(None)
                return

            independent, measurement = extract_event_page(independent_keys, dependent_keys, payload=doc["data"])
            recommender.tell_many(independent, measurement)
            try:
                next_point = recommender.ask(1)
            except NoRecommendation:
                # no recommendation
                queue.put(None)
            else:
                queue.put({k: v for k, v in zip(independent_keys, next_point)})

    rr = RunRouter([lambda name, doc: ([callback], [])])
    return rr, queue


def adaptive_plan(
    dets, first_point, *, to_recommender, from_recommender, md=None, take_reading=bps.trigger_and_read
):
    """
    Execute an adaptive scan using an per event-run recommendation engine.

    The communication pattern here is that there is 1 recommendation for
    each Event that is generate

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

           def take_reading(dets, name='primary'):
                yield from ...

        Callable[List[OphydObj], Optional[str]] -> Generator[Msg], optional

        Defaults to `bluesky.plan_stubs.trigger_and_read`
    """
    # TODO inject args / kwargs here.
    _md = {"hints": {}}
    _md.update(md or {})
    try:
        _md["hints"].setdefault("dimensions", [(m.hints["fields"], "primary") for m in first_point.keys()])
    except (AttributeError, KeyError):
        ...

    # extract the motors
    motors = list(first_point.keys())
    # convert the first_point variable to from we will be getting
    # from queue
    first_point = {m.name: v for m, v in first_point.items()}

    @bpp.subs_decorator(to_recommender)
    @bpp.run_decorator(md=_md)
    def gp_inner_plan():
        next_point = first_point
        while True:
            # this assumes that m.name == the key in Event
            target = {m: next_point[m.name] for m in motors}
            motor_position_pairs = itertools.chain(*target.items())
            yield from bps.mov(*motor_position_pairs)
            yield from take_reading(dets + motors, name="primary")
            next_point = from_recommender.get(timeout=1)
            if next_point is None:
                return

    return (yield from gp_inner_plan())
