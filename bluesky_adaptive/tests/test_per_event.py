from bluesky.tests.utils import DocCollector

from bluesky_adaptive.per_event import (
    per_event_recommender_factory,
    per_event_adaptive_plan,
)
from bluesky_adaptive.recommendations import SequenceRecommender


def test_seq_recommender(RE, hw):

    recommender = SequenceRecommender([[1,], [2,], [3,]])

    cb, queue = per_event_recommender_factory(recommender, ["motor"], ["det"])
    dc = DocCollector()

    RE(
        per_event_adaptive_plan(
            [hw.det], {hw.motor: 0}, to_brains=cb, from_brains=queue
        ),
        dc.insert,
    )

    assert len(dc.start) == 1
    assert len(dc.event) == 1
    (events,) = dc.event.values()
    assert len(events) == 4
