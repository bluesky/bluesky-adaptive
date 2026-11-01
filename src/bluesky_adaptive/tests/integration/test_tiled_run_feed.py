import threading
import uuid

import bluesky.plans as bp
import pytest
from bluesky.run_engine import RunEngine
from bluesky_tiled_plugins import TiledWriter
from tiled.client import from_uri
from tiled.server import SimpleTiledServer

from bluesky_adaptive.runs import CORRELATION_UID_KEY, TiledRunFeed

from ..conftest import ReadableSignal, wait_until

pytestmark = pytest.mark.integration


def test_tiled_run_feed_reports_completed_run_with_readable_data():
    tiled_server = SimpleTiledServer()
    try:
        client = from_uri(tiled_server.uri)
        RE = RunEngine({})
        RE.subscribe(TiledWriter(client))
        correlation_uid = str(uuid.uuid4())
        delivered = []
        data = {}
        lock = threading.Lock()

        def on_run(run):
            # The run's data must be readable when the feed reports it complete.
            values = from_uri(tiled_server.uri)[run.uid]["primary/det"].read()
            with lock:
                delivered.append(run)
                data[run.uid] = list(values)

        # Replay from the start so the run is seen even if the websocket connects after it is written.
        feed = TiledRunFeed(client, replay=True, reconnect_delay=0.1)
        feed.start(on_run)
        try:
            wait_until(lambda: feed.state == "subscribed", timeout=10)
            (run_uid,) = RE(bp.count([ReadableSignal("det")], num=2, md={CORRELATION_UID_KEY: correlation_uid}))

            wait_until(lambda: delivered, timeout=10)
            # Later metadata updates must not deliver the run again.
            RE(bp.count([ReadableSignal("det")], num=1))
            wait_until(lambda: len(delivered) == 2, timeout=10)
        finally:
            feed.stop()

        run = delivered[0]
        assert (run.uid, run.correlation_uid, run.exit_status) == (run_uid, correlation_uid, "success")
        assert data[run_uid] == [0.0, 0.0]
        assert [r.uid for r in delivered].count(run_uid) == 1
    finally:
        tiled_server.close()
