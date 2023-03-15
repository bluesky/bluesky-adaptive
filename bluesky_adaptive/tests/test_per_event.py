from bluesky.tests.utils import DocCollector

from bluesky_adaptive.per_event import adaptive_plan, recommender_factory
from bluesky_adaptive.recommendations import SequenceRecommender


def test_seq_recommender(RE, hw):
    recommender = SequenceRecommender(
        [
            [
                1,
            ],
            [
                2,
            ],
            [
                3,
            ],
        ]
    )  # noqa

    cb, queue = recommender_factory(recommender, ["motor"], ["det"])
    dc = DocCollector()

    RE(
        adaptive_plan([hw.det], {hw.motor: 0}, to_recommender=cb, from_recommender=queue),
        dc.insert,
    )

    assert len(dc.start) == 1
    assert len(dc.event) == 1
    (events,) = dc.event.values()
    assert len(events) == 4
