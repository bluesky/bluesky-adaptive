import pytest
from bluesky.tests.utils import DocCollector
from bluesky.utils import RequestAbort, RequestStop, RunEngineInterrupted

from bluesky_adaptive.per_event import adaptive_plan, recommender_factory
from bluesky_adaptive.recommendations import RequestPause, SequenceRecommender


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


class StopSequenceRecommender(SequenceRecommender):
    """Test sequence recommender that aborts if the reccomendation gets too big"""

    def suggest(self, *args, **kwargs):
        next_point = super().suggest(*args, **kwargs)
        if any([p > 2 for p in next_point]):
            raise RequestStop("Too big!")
        else:
            return next_point


def test_seq_recommender_stop(RE, hw):
    recommender = StopSequenceRecommender(
        [
            [1],
            [2],
            [3],
        ]
    )
    cb, queue = recommender_factory(recommender, ["motor"], ["det"])
    dc = DocCollector()

    RE(
        adaptive_plan([hw.det], {hw.motor: 0}, to_recommender=cb, from_recommender=queue),
        dc.insert,
    )
    assert len(dc.start) == 1
    assert len(dc.event) == 1
    (events,) = dc.event.values()
    assert len(events) == 3
    assert list(dc.stop.values())[0]["exit_status"] == "success"


class AbortSequenceRecommender(SequenceRecommender):
    """Test sequence recommender that aborts if the reccomendation gets too big"""

    def suggest(self, *args, **kwargs):
        next_point = super().suggest(*args, **kwargs)
        if any([p > 2 for p in next_point]):
            raise RequestAbort("Too big!")
        else:
            return next_point


def test_seq_recommender_abort(RE, hw):
    recommender = AbortSequenceRecommender(
        [
            [1],
            [2],
            [3],
        ]
    )
    cb, queue = recommender_factory(recommender, ["motor"], ["det"])
    dc = DocCollector()

    RE(
        adaptive_plan([hw.det], {hw.motor: 0}, to_recommender=cb, from_recommender=queue),
        dc.insert,
    )
    assert len(dc.start) == 1
    assert len(dc.event) == 1
    (events,) = dc.event.values()
    assert len(events) == 3
    assert list(dc.stop.values())[0]["exit_status"] == "abort"


class PauseSequenceRecommender(SequenceRecommender):
    """Test sequence recommender that aborts if the reccomendation gets too big"""

    def suggest(self, *args, **kwargs):
        next_point = super().suggest(*args, **kwargs)
        if any([p > 2 for p in next_point]):
            raise RequestPause("Too big!")
        else:
            return next_point


def test_seq_recommender_pause(RE, hw):
    recommender = PauseSequenceRecommender(
        [
            [1],
            [2],
            [3],
        ]
    )
    cb, queue = recommender_factory(recommender, ["motor"], ["det"])
    dc = DocCollector()

    with pytest.raises(RunEngineInterrupted):
        RE(
            adaptive_plan([hw.det], {hw.motor: 0}, to_recommender=cb, from_recommender=queue),
            dc.insert,
        )

    assert len(dc.start) == 1
    assert len(dc.event) == 1
    (events,) = dc.event.values()
    assert len(events) == 3
    assert RE.state == "paused"
