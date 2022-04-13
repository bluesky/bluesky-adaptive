from bluesky.tests.utils import DocCollector

from bluesky_adaptive.per_event import (
    recommender_factory,
    adaptive_plan,
)
from bluesky_adaptive.recommendations import SequenceRecommender, SequentialSummaryAgent


def test_seq_recommender(RE, hw):

    recommender = SequenceRecommender([[1], [2], [3]])  # noqa

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


def test_seq_summary_agent(RE, hw):
    agent = SequentialSummaryAgent([[1], [2], [3]], verbose=False)

    # No reporting by default
    cb, queue = recommender_factory(agent, ["motor"], ["det"])
    RE(
        adaptive_plan([hw.det], {hw.motor: 0}, to_recommender=cb, from_recommender=queue),
    )
    assert agent.n_reports == 0

    # Reporting each event
    agent = SequentialSummaryAgent([[1], [2], [3]], verbose=False)
    cb, queue = recommender_factory(agent, ["motor"], ["det"], report_period=1)
    RE(
        adaptive_plan([hw.det], {hw.motor: 0}, to_recommender=cb, from_recommender=queue),
    )
    assert agent.n_reports == 4
    assert not any(agent.last_summary.isna())

    # Reporting every 2 runs
    agent = SequentialSummaryAgent([[1], [2], [3]], verbose=False)
    cb, queue = recommender_factory(agent, ["motor"], ["det"], report_period=2)
    RE(
        adaptive_plan([hw.det], {hw.motor: 0}, to_recommender=cb, from_recommender=queue),
    )
    assert agent.n_reports == 2
    assert not any(agent.last_summary.isna())
