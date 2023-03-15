from queue import Empty

import pytest
from bluesky.tests.utils import DocCollector

from bluesky_adaptive.per_start import adaptive_plan, recommender_factory
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

    # pre-poison the queue to simulate a messy reccomender
    queue.put(None)
    queue.put({})

    RE(
        adaptive_plan([hw.det], {hw.motor: 0}, to_recommender=cb, from_recommender=queue),
        dc.insert,
    )

    assert len(dc.start) == 4
    assert len(dc.event) == 4
    for ev in dc.event.values():
        assert len(ev) == 1

    # check that our reccomender does not leave anything behind
    with pytest.raises(Empty):
        queue.get(block=False)
