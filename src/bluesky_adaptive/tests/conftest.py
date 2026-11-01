import time
from collections.abc import Callable, Mapping
from typing import Any

from bluesky.protocols import HasHints, HasParent, Hints, NamedMovable, Readable, Status

from bluesky_adaptive.protocols import Checkpointable, Optimizer
from bluesky_adaptive.runs import CompletedRun, RunCallback


def wait_until(predicate: Callable[[], Any], timeout: float = 5.0, interval: float = 0.005) -> None:
    """Poll ``predicate`` until it is truthy; fail the test after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout} s")
        time.sleep(interval)


class FakeFeed:
    """A run feed driven by the test: ``emit`` delivers a completed run on the caller's thread."""

    def __init__(self) -> None:
        self.callback: RunCallback | None = None
        self.state = "idle"

    def start(self, callback: RunCallback) -> None:
        self.callback = callback
        self.state = "subscribed"

    def stop(self) -> None:
        self.state = "stopped"

    def emit(self, start: Mapping[str, Any], stop: Mapping[str, Any]) -> CompletedRun:
        assert self.callback is not None, "feed not started"
        run = CompletedRun(start=start, stop=stop)
        self.callback(run)
        return run


class CheckpointableOptimizer(Optimizer, Checkpointable): ...


class AlwaysSuccessfulStatus(Status):
    def add_callback(self, callback) -> None:
        callback(self)

    def exception(self, timeout=0.0):
        return None

    @property
    def done(self) -> bool:
        return True

    @property
    def success(self) -> bool:
        return True


class ReadableSignal(Readable, HasHints, HasParent):
    def __init__(self, name: str) -> None:
        self._name = name
        self._value = 0.0

    @property
    def name(self) -> str:
        return self._name

    @property
    def hints(self) -> Hints:
        return {
            "fields": [self._name],
            "dimensions": [],
            "gridding": "rectilinear",
        }

    @property
    def parent(self) -> Any | None:
        return None

    def read(self):
        return {self._name: {"value": self._value, "timestamp": time.time()}}

    def describe(self):
        return {self._name: {"source": self._name, "dtype": "number", "shape": []}}


class MovableSignal(ReadableSignal, NamedMovable):
    def __init__(self, name: str, initial_value: float = 0.0) -> None:
        super().__init__(name)
        self._value: float = initial_value

    def set(self, value: float) -> Status:
        self._value = value
        return AlwaysSuccessfulStatus()
