from bluesky.tests.utils import DocCollector

from bluesky_adaptive.per_start import (
    recommender_factory,
    adaptive_plan,
)
from bluesky_adaptive.recommendations import SequenceRecommender


def test_seq_recommender(RE, hw):

    recommender = SequenceRecommender([[1,], [2,], [3,]])  # noqa

    cb, queue = recommender_factory(recommender, ["motor"], ["det"])
    dc = DocCollector()

    RE(
        adaptive_plan([hw.det], {hw.motor: 0}, to_brains=cb, from_brains=queue),
        dc.insert,
    )

    assert len(dc.start) == 4
    assert len(dc.event) == 4
    for ev in dc.event.values():
        assert len(ev) == 1
