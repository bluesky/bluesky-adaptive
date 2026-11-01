"""
Completed-run feeds and routing for optimization through a Bluesky queueserver.

.. warning::

    This module is **experimental**. The API is not yet stable and may change
    in future releases without a deprecation period.

A :class:`RunFeed` reports every Bluesky run that finished, with its start and stop documents. One
:class:`RunRouter` per document stream hands each run to the runner that submitted it, matched by the
correlation UID in the start document, and offers every run to subscribers such as passive ingestion.

Feeds deliver on their own threads. Callbacks must not block; runners and ingestion only enqueue.
"""

import itertools
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

CORRELATION_UID_KEY: Literal["blop_correlation_uid"] = "blop_correlation_uid"
"""Start-document key holding the UID that ties a run to the iteration that submitted it."""

FeedState = Literal["idle", "subscribed", "reconnecting", "stopped"]
"""Lifecycle of a run feed."""


@dataclass(frozen=True)
class CompletedRun:
    """
    A Bluesky run that finished.

    Parameters
    ----------
    start : Mapping[str, Any]
        The run's start document.
    stop : Mapping[str, Any]
        The run's stop document.
    """

    start: Mapping[str, Any]
    stop: Mapping[str, Any]

    @property
    def uid(self) -> str:
        """The run UID."""
        return self.start["uid"]

    @property
    def correlation_uid(self) -> str | None:
        """The correlation UID bluesky-adaptive put in the start document, if any."""
        return self.start.get(CORRELATION_UID_KEY)

    @property
    def exit_status(self) -> str | None:
        """The stop document's exit status: ``"success"``, ``"fail"`` or ``"abort"``."""
        return self.stop.get("exit_status")


RunCallback = Callable[[CompletedRun], None]
"""Receives a completed run. Must not block."""

SubscriberCallback = Callable[[CompletedRun, str | None], None]
"""Receives every completed run and the owner of its claim, or ``None``. Must not block."""


class RunFeed(Protocol):
    """A source of completed runs."""

    def start(self, callback: RunCallback) -> None:
        """Start delivering completed runs to ``callback`` from a background thread."""
        ...

    def stop(self) -> None:
        """Stop delivering runs and release resources."""
        ...

    @property
    def state(self) -> FeedState:
        """Lifecycle of the feed."""
        ...


class _Dispatcher(Protocol):
    def subscribe(self, func: Callable[[str, Mapping[str, Any]], Any]) -> int: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


def _remember(seen: "OrderedDict[str, Any]", key: str, value: Any, size: int) -> None:
    seen[key] = value
    while len(seen) > size:
        seen.popitem(last=False)


class DocumentStreamFeed:
    """
    Completed runs from a Bluesky document stream, such as the queueserver's ZMQ publisher.

    Parameters
    ----------
    dispatcher : bluesky.callbacks.zmq.RemoteDispatcher
        Dispatcher for the document stream. The feed runs it on a daemon thread.
    cache_size : int, optional
        Number of start documents kept while waiting for their stop documents.
    """

    def __init__(self, dispatcher: _Dispatcher, *, cache_size: int = 1000):
        self._dispatcher = dispatcher
        self._cache_size = cache_size
        self._starts: OrderedDict[str, Mapping[str, Any]] = OrderedDict()
        self._callback: RunCallback | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    @property
    def state(self) -> FeedState:
        """``"subscribed"`` while the dispatcher thread runs; ZMQ cannot tell whether a publisher is connected."""
        if self._stopped:
            return "stopped"
        if self._thread is None:
            return "idle"
        return "subscribed" if self._thread.is_alive() else "stopped"

    def start(self, callback: RunCallback) -> None:
        """Subscribe to the dispatcher and run it on a daemon thread."""
        if self._thread is not None:
            raise RuntimeError("DocumentStreamFeed is already started")
        self._callback = callback
        self._dispatcher.subscribe(self._on_document)
        self._thread = threading.Thread(target=self._dispatcher.start, name="run-feed-documents", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the dispatcher."""
        self._stopped = True
        if self._thread is not None:
            self._dispatcher.stop()

    def _on_document(self, name: str, doc: Mapping[str, Any]) -> None:
        try:
            if name == "start":
                _remember(self._starts, doc["uid"], doc, self._cache_size)
            elif name == "stop":
                start = self._starts.pop(doc["run_start"], None)
                if start is None:
                    logger.debug("Ignoring stop document of a run whose start was not seen: %s", doc["run_start"])
                elif self._callback is not None:
                    self._callback(CompletedRun(start=start, stop=doc))
        except Exception:
            # Raising here would stop the dispatcher for every agent on this stream.
            logger.exception("Failed to handle %r document", name)


class _Container(Protocol):
    def subscribe(self, executor: ThreadPoolExecutor | None = None) -> Any: ...


class TiledRunFeed:
    """
    Completed runs from a Tiled container that a ``TiledWriter`` writes to.

    Subscribes to the container over Tiled's websocket stream. A run is complete when its metadata
    holds both ``start`` and ``stop``; by then the run's data is readable.

    Parameters
    ----------
    container : tiled.client.container.Container
        The container runs are written to, e.g. ``from_uri(uri)`` or ``from_uri(uri)["raw"]``.
    replay : bool, optional
        Replay the updates the server still holds when the feed first starts, instead of
        starting from new updates.
    reconnect_delay : float, optional
        Seconds to wait before subscribing again after the subscription fails.
    dedupe_size : int, optional
        Number of run UIDs remembered so each run is delivered once.
    """

    def __init__(
        self,
        container: _Container,
        *,
        replay: bool = False,
        reconnect_delay: float = 5.0,
        dedupe_size: int = 10_000,
    ):
        self._container = container
        self._replay = replay
        self._reconnect_delay = reconnect_delay
        self._dedupe_size = dedupe_size
        self._callback: RunCallback | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._subscription: Any = None
        self._state: FeedState = "idle"
        self._last_sequence: int | None = None
        self._delivered: OrderedDict[str, None] = OrderedDict()

    @property
    def state(self) -> FeedState:
        """
        Lifecycle of the feed.

        ``"subscribed"`` means the subscription has started. Its websocket may still be connecting, so
        runs that stop within the first moments can be missed unless ``replay`` is set. Tiled's client
        reconnects dropped connections within a subscription, resuming from the last update received.
        """
        return self._state

    @property
    def last_sequence(self) -> int | None:
        """Sequence number of the last update handled."""
        return self._last_sequence

    def start(self, callback: RunCallback) -> None:
        """Subscribe on a daemon thread that resubscribes after failures."""
        if self._thread is not None:
            raise RuntimeError("TiledRunFeed is already started")
        self._callback = callback
        self._thread = threading.Thread(target=self._run, name="run-feed-tiled", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Disconnect and wait for the subscription thread to exit."""
        self._stopping.set()
        with self._lock:
            subscription = self._subscription
        if subscription is not None:
            subscription.disconnect()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self._state = "stopped"

    def _start_from(self) -> int | None:
        if self._last_sequence is not None:
            return self._last_sequence + 1
        return 0 if self._replay else None

    def _run(self) -> None:
        while not self._stopping.is_set():
            # A subscription shuts its executor down when it disconnects, so each attempt gets a new one.
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="run-feed-tiled-callbacks")
            try:
                subscription = self._container.subscribe(executor=executor)
                subscription.child_created.add_callback(self._on_child_created)
                subscription.child_metadata_updated.add_callback(self._on_metadata_updated)
                with self._lock:
                    if self._stopping.is_set():
                        break
                    self._subscription = subscription
                self._state = "subscribed"
                subscription.start(start=self._start_from())
            except Exception:
                if self._stopping.is_set():
                    break
                logger.exception("Tiled run feed subscription failed; subscribing again in %.1f s", self._reconnect_delay)
            finally:
                with self._lock:
                    self._subscription = None
                executor.shutdown(wait=False)
            if self._stopping.is_set():
                break
            self._state = "reconnecting"
            self._stopping.wait(self._reconnect_delay)
        self._state = "stopped"

    def _seen_sequence(self, update: Any) -> None:
        sequence = getattr(update, "sequence", None)
        if sequence is not None and (self._last_sequence is None or sequence > self._last_sequence):
            self._last_sequence = sequence

    def _on_child_created(self, update: Any) -> None:
        self._seen_sequence(update)

    def _on_metadata_updated(self, update: Any) -> None:
        # Exceptions raised in Tiled callbacks vanish with the executor's future, so log them here.
        try:
            self._seen_sequence(update)
            metadata = update.metadata or {}
            start, stop = metadata.get("start"), metadata.get("stop")
            if not start or not stop:
                return
            uid = start.get("uid") or update.key
            with self._lock:
                if uid in self._delivered:
                    return
                _remember(self._delivered, uid, None, self._dedupe_size)
            if self._callback is not None:
                self._callback(CompletedRun(start=dict(start, uid=uid), stop=stop))
        except Exception:
            logger.exception("Failed to handle Tiled update %s", getattr(update, "sequence", None))


class RunRouter:
    """
    Routes completed runs from one feed to the runners that submitted them and to subscribers.

    Parameters
    ----------
    feed : RunFeed
        The feed for one document stream (one queueserver).
    """

    def __init__(self, feed: RunFeed):
        self._feed = feed
        self._lock = threading.Lock()
        self._claims: dict[str, tuple[str, RunCallback]] = {}
        self._subscribers: dict[int, SubscriberCallback] = {}
        self._tokens = itertools.count()
        self._runs_routed = 0

    @property
    def feed(self) -> RunFeed:
        """The feed this router reads."""
        return self._feed

    @property
    def runs_routed(self) -> int:
        """Number of completed runs routed so far."""
        return self._runs_routed

    def start(self) -> None:
        """Start the feed."""
        self._feed.start(self._route)

    def close(self) -> None:
        """Stop the feed and drop all claims and subscribers."""
        self._feed.stop()
        with self._lock:
            self._claims.clear()
            self._subscribers.clear()

    def claim(self, correlation_uid: str, owner: str, callback: RunCallback) -> None:
        """
        Send the run with ``correlation_uid`` to ``callback``.

        Claim before submitting the plan, so a run cannot finish before its claim exists.

        Parameters
        ----------
        correlation_uid : str
            The correlation UID the plan puts in its start document.
        owner : str
            Who claimed it; passed to subscribers so they can skip their own runs.
        callback : RunCallback
            Receives the completed run. Must not block.
        """
        with self._lock:
            if correlation_uid in self._claims:
                raise ValueError(f"correlation uid {correlation_uid!r} is already claimed")
            self._claims[correlation_uid] = (owner, callback)

    def release(self, correlation_uid: str) -> None:
        """Drop a claim; later runs with that correlation UID only reach subscribers."""
        with self._lock:
            self._claims.pop(correlation_uid, None)

    def subscribe(self, callback: SubscriberCallback) -> int:
        """
        Offer every completed run to ``callback``, with the owner of its claim or ``None``.

        Returns
        -------
        int
            A token for :meth:`unsubscribe`.
        """
        with self._lock:
            token = next(self._tokens)
            self._subscribers[token] = callback
        return token

    def unsubscribe(self, token: int) -> None:
        """Stop offering runs to the subscriber with ``token``."""
        with self._lock:
            self._subscribers.pop(token, None)

    def _route(self, run: CompletedRun) -> None:
        correlation_uid = run.correlation_uid
        with self._lock:
            claim = self._claims.get(correlation_uid) if correlation_uid is not None else None
            subscribers = list(self._subscribers.values())
            self._runs_routed += 1
        owner = None
        if claim is not None:
            owner, callback = claim
            try:
                callback(run)
            except Exception:
                logger.exception("Claimant %r failed to accept run %s", owner, run.uid)
        for subscriber in subscribers:
            try:
                subscriber(run, owner)
            except Exception:
                logger.exception("Subscriber failed to accept run %s", run.uid)
