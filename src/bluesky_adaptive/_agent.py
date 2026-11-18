"""Base class shared by the agents of the optimizer backends."""

import uuid

from .callbacks.logger import OptimizationLogger
from .protocols import DocumentCallback


class _BaseAgent:
    """
    Callback registry and optimization identifier shared by the agents of the optimizer backends.

    Agents call ``super().__init__(optimization_uid=optimization_uid)`` from their own ``__init__``. An agent built
    without its ``__init__``, such as one restored from a checkpoint, calls ``_BaseAgent.__init__`` on the instance.

    Parameters
    ----------
    optimization_uid : str | None, optional
        Identifier of the optimization. If None, a new UUID4 is generated.
    """

    def __init__(self, *, optimization_uid: str | None = None) -> None:
        self._callbacks: list[DocumentCallback] = [OptimizationLogger()]
        self._optimization_uid = optimization_uid if optimization_uid is not None else str(uuid.uuid4())

    @property
    def callbacks(self) -> list[DocumentCallback]:
        """The list of active optimization callbacks.

        Callbacks in this list receive the documents of the agent's optimization runs. The default list
        contains an :class:`~bluesky_adaptive.callbacks.logger.OptimizationLogger`.

        The list can be mutated directly, or use :meth:`subscribe` /
        :meth:`unsubscribe` for convenience.
        """
        return self._callbacks

    def subscribe(self, callback: DocumentCallback) -> None:
        """Subscribe a callback to receive optimization run documents.

        Parameters
        ----------
        callback : DocumentCallback
            A Bluesky document callback, e.g. a :class:`bluesky.callbacks.CallbackBase` instance.

        Raises
        ------
        ValueError
            If *callback* is already subscribed.
        """
        if callback in self._callbacks:
            raise ValueError(f"Callback {callback!r} is already subscribed.")
        self._callbacks.append(callback)

    def unsubscribe(self, callback: DocumentCallback) -> None:
        """Unsubscribe a previously subscribed callback.

        Parameters
        ----------
        callback : DocumentCallback
            The callback to remove.

        Raises
        ------
        ValueError
            If *callback* is not subscribed.
        """
        self._callbacks.remove(callback)

    @property
    def optimization_uid(self) -> str:
        """Identifier of this agent's optimization."""
        return self._optimization_uid
