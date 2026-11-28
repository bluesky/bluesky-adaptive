import itertools
import logging
import time
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bluesky.plans import count
from bluesky.run_engine import RunEngine
from bluesky_tiled_plugins import TiledWriter
from tiled.catalog import in_memory
from tiled.client import Context, from_context, from_uri
from tiled.server import SimpleTiledServer
from tiled.server.app import build_app

import bluesky_adaptive.evaluation.tiled as tiled_evaluation
from bluesky_adaptive.evaluation import per_run, per_suggestion
from bluesky_adaptive.evaluation.documents import RunEventBuffer
from bluesky_adaptive.evaluation.tiled import TiledQueueItemRuns, TiledRunPoller
from bluesky_adaptive.plans import default_acquire, optimize
from bluesky_adaptive.protocols import OptimizationProblem, Optimizer

from ..conftest import MovableSignal, ReadableSignal, re_manager

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


@pytest.fixture()
def tiled_client(tmp_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    warnings.filterwarnings("ignore", category=FutureWarning)
    catalog = in_memory(writable_storage=[tmp_path / "data", f"duckdb:///{tmp_path / 'storage.duckdb'}"])
    with Context.from_app(build_app(catalog)) as context:
        yield from_context(context)


def queue_item_documents(plan, item_uid):
    """Run the plan as the RE Manager runs a queue item's plan and return its documents."""
    documents = []
    RE = RunEngine({})
    RE.subscribe(lambda name, doc: documents.append((name, doc)))
    RE(plan, queue_server={"queue_item_uid": item_uid})
    return documents


def run_uids(documents):
    return [doc["uid"] for name, doc in documents if name == "start"]


def write(writer, documents):
    for name, doc in documents:
        writer(name, doc)


def test_tiled_queue_item_runs_waits_for_the_items_runs_and_selects_the_first_that_passes_run_filter(
    tiled_client, monkeypatch
):
    det = ReadableSignal("det")

    def dark_then_light():
        yield from count([det], md={"dark_frame": True})
        yield from count([det])

    writer = TiledWriter(tiled_client)
    # Another item's run is already in the catalog.
    write(writer, queue_item_documents(count([det]), "item-1"))
    documents = queue_item_documents(dark_then_light(), "item-2")
    dark_uid, light_uid = run_uids(documents)

    def write_item_2(seconds):
        # TiledWriter writes the running item's runs between polls, so the next poll must find them.
        assert documents, "Polled again after item-2's runs were written."
        write(writer, documents)
        documents.clear()

    monkeypatch.setattr(tiled_evaluation, "time", SimpleNamespace(monotonic=time.monotonic, sleep=write_item_2))
    rm = re_manager(running="item-2")
    light_runs = TiledQueueItemRuns(rm, tiled_client, run_filter=lambda start: not start.get("dark_frame"), timeout=0)

    assert light_runs.resolve("item-2") == light_uid
    assert TiledQueueItemRuns(rm, tiled_client).resolve("item-2") == dark_uid


def test_tiled_queue_item_runs_waits_for_a_stop_document_written_after_the_item_finished(tiled_client, monkeypatch):
    writer = TiledWriter(tiled_client)
    documents = queue_item_documents(count([ReadableSignal("det")]), "item-1")
    (uid,) = run_uids(documents)
    unwritten = [documents.pop()]  # the stop document
    write(writer, documents)
    rm = re_manager(history=[("item-1", {"exit_status": "completed", "run_uids": [uid], "msg": ""})])

    def write_stop(seconds):
        # The RE Manager reports the item finished before TiledWriter writes the run's stop document.
        assert rm.history_get.called
        write(writer, unwritten)
        unwritten.clear()

    monkeypatch.setattr(tiled_evaluation, "time", SimpleNamespace(monotonic=time.monotonic, sleep=write_stop))

    assert TiledQueueItemRuns(rm, tiled_client, timeout=5).resolve("item-1") == uid


def test_tiled_queue_item_runs_raises_when_the_re_manager_has_no_record_of_the_item(tiled_client):
    with pytest.raises(KeyError, match="removed from the queue"):
        TiledQueueItemRuns(re_manager(), tiled_client, timeout=60).resolve("item-1")


def test_tiled_queue_item_runs_raises_at_once_when_the_item_ends_without_a_run(tiled_client):
    # A failed item goes back in the queue under a new UID, and its original UID stays in the history.
    failed = {"exit_status": "failed", "run_uids": [], "msg": "plan raised"}
    runs = TiledQueueItemRuns(re_manager(queued=["item-1-retry"], history=[("item-1", failed)]), tiled_client, timeout=60)

    with pytest.raises(RuntimeError, match="ended with status 'failed' without opening a run: plan raised"):
        runs.resolve("item-1")


def test_tiled_queue_item_runs_raises_run_filter_errors_for_the_item(tiled_client):
    writer = TiledWriter(tiled_client)
    write(writer, queue_item_documents(count([ReadableSignal("det")]), "item-1"))
    runs = TiledQueueItemRuns(re_manager(running="item-1"), tiled_client, run_filter=lambda start: start["sample"] == "s1")

    with pytest.raises(RuntimeError, match="run_filter raised") as excinfo:
        runs.resolve("item-1")
    assert isinstance(excinfo.value.__cause__, KeyError)


def test_tiled_queue_item_runs_raises_at_once_when_no_run_of_the_finished_item_passes_run_filter(tiled_client):
    writer = TiledWriter(tiled_client)
    documents = queue_item_documents(count([ReadableSignal("det")], md={"dark_frame": True}), "item-1")
    write(writer, documents)
    finished = {"exit_status": "completed", "run_uids": run_uids(documents), "msg": ""}
    runs = TiledQueueItemRuns(
        re_manager(history=[("item-1", finished)]),
        tiled_client,
        run_filter=lambda start: not start.get("dark_frame"),
        timeout=60,
    )

    with pytest.raises(RuntimeError, match="none of its runs"):
        runs.resolve("item-1")


def test_tiled_queue_item_runs_raises_when_the_run_fails(tiled_client):
    writer = TiledWriter(tiled_client)
    RE = RunEngine({})
    RE.subscribe(writer)

    def fail(detectors, step, pos_cache):
        raise RuntimeError("detector fault")
        yield

    with pytest.raises(RuntimeError, match="detector fault"):
        RE(
            default_acquire([{"x1": 1.0, "_id": "a"}], [MovableSignal("x1")], [ReadableSignal("det")], per_step=fail),
            queue_server={"queue_item_uid": "item-1"},
        )

    with pytest.raises(RuntimeError, match="ended with status 'fail'"):
        TiledQueueItemRuns(re_manager(running="item-1"), tiled_client).resolve("item-1")


def test_tiled_queue_item_runs_times_out_while_the_finished_items_run_has_no_stop_document(tiled_client):
    writer = TiledWriter(tiled_client)
    documents = queue_item_documents(count([ReadableSignal("det")]), "item-1")
    write(writer, documents[:-1])
    finished = {"exit_status": "completed", "run_uids": run_uids(documents), "msg": ""}
    runs = TiledQueueItemRuns(re_manager(history=[("item-1", finished)]), tiled_client, timeout=0.1, poll_interval=0.01)

    with pytest.raises(TimeoutError):
        runs.resolve("item-1")


def test_tiled_queue_item_runs_resolves_an_item_submitted_before_it_was_created(tiled_client):
    writer = TiledWriter(tiled_client)
    documents = queue_item_documents(
        default_acquire([{"x1": 2.0, "_id": "b"}], [MovableSignal("x1")], [ReadableSignal("det")]), "item-1"
    )
    write(writer, documents)
    finished = {"exit_status": "completed", "run_uids": run_uids(documents), "msg": ""}
    rm = re_manager(history=[("item-1", finished)])
    # Created after the item ran, e.g. by an agent that restarted.
    evaluation_function = per_run(
        per_suggestion(lambda data: {"objective": 10 * data["x1"]}, TiledRunPoller(tiled_client)),
        TiledQueueItemRuns(rm, tiled_client).resolve,
    )

    assert evaluation_function("item-1", []) == [{"objective": 20.0, "_id": "b"}]
