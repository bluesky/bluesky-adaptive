import itertools
import logging
import warnings
from unittest.mock import MagicMock

import pytest
from bluesky.run_engine import RunEngine
from bluesky_tiled_plugins import TiledWriter
from tiled.client import from_uri
from tiled.server import SimpleTiledServer

from bluesky_adaptive.evaluation import per_suggestion
from bluesky_adaptive.evaluation.documents import RunEventBuffer
from bluesky_adaptive.evaluation.tiled import TiledRunPoller
from bluesky_adaptive.plans import default_acquire, optimize
from bluesky_adaptive.protocols import OptimizationProblem, Optimizer

from ..conftest import MovableSignal, ReadableSignal

pytestmark = pytest.mark.integration


def test_tiled_run_poller_scores_each_suggestion_from_its_primary_row() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    warnings.filterwarnings("ignore", category=FutureWarning)

    tiled_server = SimpleTiledServer()
    try:
        tiled_client = from_uri(tiled_server.uri)
        RE = RunEngine({})
        RE.subscribe(TiledWriter(tiled_client))
        optimizer = MagicMock(spec=Optimizer)
        optimizer.suggest.return_value = [{"x1": 5.0, "_id": "far"}, {"x1": 0.5, "_id": "near"}]
        problem = OptimizationProblem(
            optimizer=optimizer,
            actuators=[MovableSignal("x1", initial_value=0.0)],
            sensors=[ReadableSignal("det")],
            evaluation_function=per_suggestion(
                lambda data: {"objective": 10 * data["x1"]}, TiledRunPoller(tiled_client, timeout=10)
            ),
        )

        RE(optimize(problem, iterations=2, n_points=2))

        # Routing acquires "near" first from x1=0.0, then "far" first from x1=5.0.
        assert [call.args[0] for call in optimizer.ingest.call_args_list] == [
            [{"objective": 5.0, "_id": "near"}, {"objective": 50.0, "_id": "far"}],
            [{"objective": 50.0, "_id": "far"}, {"objective": 5.0, "_id": "near"}],
        ]
    finally:
        tiled_server.close()


@pytest.mark.parametrize("writer_kwargs", [{}, {"batch_size": 1}], ids=["batched", "streamed"])
def test_tiled_run_poller_waits_until_tiled_writer_has_written_every_row(writer_kwargs: dict) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    warnings.filterwarnings("ignore", category=FutureWarning)

    RE = RunEngine({})
    documents = []
    RE.subscribe(lambda name, doc: documents.append((name, doc)))
    buffer = RunEventBuffer()
    RE.subscribe(buffer)
    suggestions = [{"x1": 5.0, "_id": "far"}, {"x1": 0.5, "_id": "near"}]
    (uid,) = RE(default_acquire(suggestions, [MovableSignal("x1")], [ReadableSignal("det")]))
    expected = buffer.pop(uid)

    tiled_server = SimpleTiledServer()
    try:
        poller = TiledRunPoller(from_uri(tiled_server.uri), timeout=0)
        outcomes = []

        def poll_after_write(response) -> None:
            if response.request.method != "GET":
                try:
                    outcomes.append("complete" if poller(uid) == expected else "incomplete")
                except TimeoutError:
                    outcomes.append("waiting")

        writer_client = from_uri(tiled_server.uri)
        writer_client.context.http_client.event_hooks["response"].append(poll_after_write)
        writer = TiledWriter(writer_client, **writer_kwargs)
        for name, doc in documents:
            writer(name, doc)

        # "primary" and its rows appear over several writes, before the stop document. Polled after every write, the
        # poller waits until the stop document is written, then returns every row.
        assert [outcome for outcome, _ in itertools.groupby(outcomes)] == ["waiting", "complete"]
    finally:
        tiled_server.close()
