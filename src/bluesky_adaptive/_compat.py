"""RunEngine plan stubs for supported Bluesky releases."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, ParamSpec, TypeVar, cast

from bluesky import plan_stubs as bps
from bluesky.utils import MsgGenerator, plan

P = ParamSpec("P")
T = TypeVar("T")

logger = logging.getLogger(__name__)


def call_in_thread(func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> MsgGenerator[T]:
    """Run a blocking callback without blocking the RunEngine loop."""
    dispatch = getattr(bps, "call_in_thread", _call_in_thread)
    # Older @plan implementations reserve the keyword "f" for the plan itself.
    return dispatch(partial(func, *args, **kwargs))


@plan
def _call_in_thread(func: Callable[[], T], /) -> MsgGenerator[T]:
    # Adapted from bluesky/bluesky#2115 for releases without the plan stub.
    task: asyncio.Task[T] | None = None

    def call() -> T:
        try:
            return func()
        except StopIteration as exc:
            raise RuntimeError(f"{func!r} raised StopIteration") from exc

    def start_or_wait() -> Awaitable[Any]:
        nonlocal task
        if task is None:
            task = asyncio.create_task(asyncio.to_thread(call))
        return asyncio.wait([task])

    def log_failure(done: asyncio.Task[T]) -> None:
        if not done.cancelled() and (exc := done.exception()) is not None:
            logger.error("call_in_thread: %r failed after its plan ended", func, exc_info=exc)

    try:
        yield from bps.wait_for([start_or_wait])
        if task is None:  # Inspected as a list of messages without a RunEngine.
            return cast(T, None)
        while not task.done():  # Wait for the same callback after a pause.
            yield from bps.wait_for([start_or_wait])
        return task.result()
    finally:
        if task is not None and not task.done():
            task.add_done_callback(log_failure)
