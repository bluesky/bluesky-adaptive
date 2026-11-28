import time
from typing import Any
from unittest.mock import MagicMock

from bluesky.protocols import HasHints, HasParent, Hints, NamedMovable, Readable, Status
from bluesky_queueserver_api.zmq import REManagerAPI

from bluesky_adaptive.protocols import Checkpointable, Optimizer


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


def re_manager(*, running=None, queued=(), history=()):
    """Mimic an RE Manager's running item, queue, and history of (item UID, result) pairs."""
    re_manager_api = MagicMock(spec=REManagerAPI)
    re_manager_api.queue_get.return_value = {
        "items": [{"item_uid": item_uid} for item_uid in queued],
        "running_item": {"item_uid": running} if running else {},
    }
    re_manager_api.history_get.return_value = {
        "items": [{"item_uid": item_uid, "result": result} for item_uid, result in history]
    }
    return re_manager_api
