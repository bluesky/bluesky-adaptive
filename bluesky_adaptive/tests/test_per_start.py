from queue import Empty

import pytest

from bluesky.tests.utils import DocCollector

from bluesky_adaptive.per_start import (
    recommender_factory,
    adaptive_plan,
)
from bluesky_adaptive.recommendations import SequenceRecommender, SequentialSummaryAgent


def test_seq_recommender(RE, hw):

    recommender = SequenceRecommender([[1], [2], [3]])  # noqa

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


def test_seq_summary_agent(RE, hw):
    agent = SequentialSummaryAgent([[1], [2], [3]], verbose=False)

    # No reporting by default
    cb, queue = recommender_factory(agent, ["motor"], ["det"])
    RE(
        adaptive_plan([hw.det], {hw.motor: 0}, to_recommender=cb, from_recommender=queue),
    )
    assert agent.n_reports == 0

    # Reporting each run i.e. at the start of the 2nd, 3rd, and 4th.
    agent = SequentialSummaryAgent([[1], [2], [3]], verbose=False)
    cb, queue = recommender_factory(agent, ["motor"], ["det"], report_period=1)
    RE(
        adaptive_plan([hw.det], {hw.motor: 0}, to_recommender=cb, from_recommender=queue),
    )
    assert agent.n_reports == 3
    assert not any(agent.last_summary.isna())

    # Reporting every 2 runs, i.e. at the start of the 3rd and 5th
    agent = SequentialSummaryAgent([[1], [2], [3], [4]], verbose=False)
    cb, queue = recommender_factory(agent, ["motor"], ["det"], report_period=2)
    RE(
        adaptive_plan([hw.det], {hw.motor: 0}, to_recommender=cb, from_recommender=queue),
    )
    assert agent.n_reports == 2
    assert not any(agent.last_summary.isna())
