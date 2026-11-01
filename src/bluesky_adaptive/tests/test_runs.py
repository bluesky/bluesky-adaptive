import threading
from types import SimpleNamespace

import pytest

from bluesky_adaptive.runs import CORRELATION_UID_KEY, CompletedRun, DocumentStreamFeed, RunRouter, TiledRunFeed

from .conftest import FakeFeed, wait_until


def _start(uid, correlation_uid=None):
    start = {"uid": uid, "time": 0.0}
    if correlation_uid is not None:
        start[CORRELATION_UID_KEY] = correlation_uid
    return start


def _stop(uid, exit_status="success"):
    return {"uid": f"{uid}-stop", "run_start": uid, "time": 0.0, "exit_status": exit_status}


@pytest.fixture
def feed():
    return FakeFeed()


@pytest.fixture
def router(feed):
    router = RunRouter(feed)
    router.start()
    yield router
    router.close()


# RunRouter


def test_router_sends_claimed_run_only_to_its_claimant(router, feed):
    mine, theirs = [], []
    router.claim("corr-a", "agent-a", mine.append)
    router.claim("corr-b", "agent-b", theirs.append)

    run = feed.emit(_start("run-1", "corr-a"), _stop("run-1"))

    assert mine == [run]
    assert theirs == []


def test_router_offers_every_run_to_subscribers_with_owner(router, feed):
    offered = []
    router.subscribe(lambda run, owner: offered.append((run.uid, owner)))
    router.claim("corr-a", "agent-a", lambda run: None)

    feed.emit(_start("claimed", "corr-a"), _stop("claimed"))
    feed.emit(_start("unclaimed", "corr-x"), _stop("unclaimed"))
    feed.emit(_start("foreign"), _stop("foreign"))

    assert offered == [("claimed", "agent-a"), ("unclaimed", None), ("foreign", None)]
    assert router.runs_routed == 3


def test_router_released_claim_reaches_subscribers_only(router, feed):
    claimed, offered = [], []
    router.claim("corr-a", "agent-a", claimed.append)
    router.subscribe(lambda run, owner: offered.append(owner))

    router.release("corr-a")
    feed.emit(_start("run-1", "corr-a"), _stop("run-1"))

    assert claimed == []
    assert offered == [None]


def test_router_unsubscribed_callback_gets_no_runs(router, feed):
    offered = []
    token = router.subscribe(lambda run, owner: offered.append(run.uid))
    feed.emit(_start("run-1"), _stop("run-1"))

    router.unsubscribe(token)
    feed.emit(_start("run-2"), _stop("run-2"))

    assert offered == ["run-1"]


def test_router_rejects_duplicate_claim(router):
    router.claim("corr-a", "agent-a", lambda run: None)

    with pytest.raises(ValueError, match="already claimed"):
        router.claim("corr-a", "agent-b", lambda run: None)


def test_router_isolates_raising_callbacks(router, feed):
    def explode(*args):
        raise RuntimeError("boom")

    offered = []
    router.claim("corr-a", "agent-a", explode)
    router.subscribe(explode)
    router.subscribe(lambda run, owner: offered.append((run.uid, owner)))

    feed.emit(_start("run-1", "corr-a"), _stop("run-1"))
    feed.emit(_start("run-2"), _stop("run-2"))

    assert offered == [("run-1", "agent-a"), ("run-2", None)]


def test_completed_run_exposes_document_fields():
    run = CompletedRun(start=_start("run-1", "corr-a"), stop=_stop("run-1", "abort"))

    assert (run.uid, run.correlation_uid, run.exit_status) == ("run-1", "corr-a", "abort")
    assert CompletedRun(start=_start("run-2"), stop={}).correlation_uid is None


# DocumentStreamFeed


class FakeDispatcher:
    """Mimics RemoteDispatcher: ``start`` blocks until ``stop``; the test pushes documents."""

    def __init__(self):
        self.subscriber = None
        self.stopped = threading.Event()

    def subscribe(self, func):
        self.subscriber = func
        return 0

    def start(self):
        self.stopped.wait()

    def stop(self):
        self.stopped.set()

    def push(self, name, doc):
        self.subscriber(name, doc)


@pytest.fixture
def dispatcher():
    return FakeDispatcher()


@pytest.fixture
def document_feed(dispatcher):
    feed = DocumentStreamFeed(dispatcher)
    yield feed
    feed.stop()


def test_document_feed_emits_each_started_run_on_its_stop(dispatcher, document_feed):
    runs = []
    document_feed.start(runs.append)
    assert document_feed.state == "subscribed"

    dispatcher.push("start", _start("run-1", "corr-a"))
    dispatcher.push("start", _start("run-2"))
    dispatcher.push("descriptor", {"uid": "d", "run_start": "run-1"})
    dispatcher.push("stop", _stop("run-2", "fail"))
    dispatcher.push("stop", _stop("run-1"))

    assert [(run.uid, run.correlation_uid, run.exit_status) for run in runs] == [
        ("run-2", None, "fail"),
        ("run-1", "corr-a", "success"),
    ]


def test_document_feed_ignores_stop_without_start(dispatcher, document_feed):
    runs = []
    document_feed.start(runs.append)

    dispatcher.push("stop", _stop("never-started"))
    dispatcher.push("start", _start("run-1"))
    dispatcher.push("stop", _stop("run-1"))
    dispatcher.push("stop", _stop("run-1"))

    assert [run.uid for run in runs] == ["run-1"]


def test_document_feed_keeps_dispatching_after_callback_error(dispatcher, document_feed):
    runs = []

    def callback(run):
        runs.append(run.uid)
        if run.uid == "run-1":
            raise RuntimeError("boom")

    document_feed.start(callback)
    for uid in ("run-1", "run-2"):
        dispatcher.push("start", _start(uid))
        dispatcher.push("stop", _stop(uid))

    assert runs == ["run-1", "run-2"]


def test_document_feed_stop_stops_dispatcher(dispatcher, document_feed):
    document_feed.start(lambda run: None)

    document_feed.stop()

    assert dispatcher.stopped.is_set()
    assert document_feed.state == "stopped"


# TiledRunFeed


class FakeRegistry:
    def __init__(self):
        self.callbacks = []

    def add_callback(self, callback):
        self.callbacks.append(callback)

    def process(self, update):
        for callback in self.callbacks:
            callback(update)


class FakeSubscription:
    """Mimics tiled's ContainerSubscription: ``start`` blocks until disconnected or crashed."""

    def __init__(self, executor):
        self.executor = executor
        self.child_created = FakeRegistry()
        self.child_metadata_updated = FakeRegistry()
        self.started = threading.Event()
        self.start_arg = "unset"
        self._ended = threading.Event()
        self._error = None
        self.disconnected = False

    def start(self, start=None):
        self.start_arg = start
        self.started.set()
        self._ended.wait()
        if self._error is not None:
            raise self._error

    def disconnect(self, wait=True):
        self.disconnected = True
        self._ended.set()

    def crash(self, error):
        self._error = error
        self._ended.set()

    def created(self, sequence, key, start):
        self.child_created.process(SimpleNamespace(sequence=sequence, key=key, metadata={"start": start}))

    def updated(self, sequence, key, metadata):
        self.child_metadata_updated.process(SimpleNamespace(sequence=sequence, key=key, metadata=metadata))


class FakeContainer:
    def __init__(self):
        self.subscriptions = []

    def subscribe(self, executor=None):
        subscription = FakeSubscription(executor)
        self.subscriptions.append(subscription)
        return subscription

    def subscription(self, index):
        wait_until(lambda: len(self.subscriptions) > index)
        subscription = self.subscriptions[index]
        assert subscription.started.wait(5)
        return subscription


@pytest.fixture
def container():
    return FakeContainer()


@pytest.fixture
def tiled_runs(container):
    runs = []
    feed = TiledRunFeed(container, reconnect_delay=0.01)
    feed.start(runs.append)
    yield feed, runs
    feed.stop()


def test_tiled_feed_emits_once_per_run_when_stop_arrives(container, tiled_runs):
    feed, runs = tiled_runs
    sub = container.subscription(0)
    assert sub.start_arg is None
    start, stop = _start("run-1", "corr-a"), _stop("run-1")

    sub.created(1, "run-1", start)
    sub.updated(2, "run-1", {"start": start})
    assert runs == []

    sub.updated(3, "run-1", {"start": start, "stop": stop})
    sub.updated(4, "run-1", {"start": start, "stop": stop})

    assert runs == [CompletedRun(start=start, stop=stop)]
    assert feed.state == "subscribed"
    assert feed.last_sequence == 4


def test_tiled_feed_replays_from_the_beginning_when_asked(container):
    feed = TiledRunFeed(container, replay=True)
    feed.start(lambda run: None)
    try:
        assert container.subscription(0).start_arg == 0
    finally:
        feed.stop()


def test_tiled_feed_survives_callback_error(container):
    delivered = []

    def callback(run):
        delivered.append(run.uid)
        if run.uid == "run-1":
            raise RuntimeError("boom")

    feed = TiledRunFeed(container)
    feed.start(callback)
    try:
        sub = container.subscription(0)
        for sequence, uid in enumerate(("run-1", "run-2"), start=1):
            sub.updated(sequence, uid, {"start": _start(uid), "stop": _stop(uid)})
    finally:
        feed.stop()

    assert delivered == ["run-1", "run-2"]
    assert len(container.subscriptions) == 1


def test_tiled_feed_resubscribes_after_failure_from_next_sequence(container, tiled_runs):
    feed, runs = tiled_runs
    first = container.subscription(0)
    start, stop = _start("run-1"), _stop("run-1")
    first.created(6, "run-1", start)
    first.updated(7, "run-1", {"start": start, "stop": stop})

    first.crash(ConnectionError("websocket closed"))
    second = container.subscription(1)

    assert second.start_arg == 8
    wait_until(lambda: feed.state == "subscribed")
    # A run delivered before the failure is not delivered again if the server replays it.
    second.updated(7, "run-1", {"start": start, "stop": stop})
    second.updated(8, "run-2", {"start": _start("run-2"), "stop": _stop("run-2")})
    assert [run.uid for run in runs] == ["run-1", "run-2"]


def test_tiled_feed_stop_disconnects_and_does_not_resubscribe(container, tiled_runs):
    feed, _ = tiled_runs
    sub = container.subscription(0)

    feed.stop()

    assert sub.disconnected
    assert feed.state == "stopped"
    assert len(container.subscriptions) == 1
