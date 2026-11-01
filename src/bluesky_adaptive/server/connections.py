"""Connections to queueservers and their completed-run feeds, opened for the server's lifetime."""

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from bluesky_queueserver_api.zmq import REManagerAPI

from ..runs import DocumentStreamFeed, RunFeed, RunRouter, TiledRunFeed
from .errors import NotFoundError
from .settings import QueueserverSettings, ServerSettings, TiledFeedSettings

logger = logging.getLogger(__name__)

ConnectFunction = Callable[[str, QueueserverSettings], tuple[Any, RunFeed]]
"""Creates the RE Manager API client and the run feed for one queueserver."""

_DEVICE_FLAGS = ("is_movable", "is_readable", "is_flyable")
_POSITIONAL = ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")
_KEYWORD = ("POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY")
_VARIADIC = ("VAR_POSITIONAL", "VAR_KEYWORD")


def connect(name: str, settings: QueueserverSettings) -> tuple[REManagerAPI, RunFeed]:
    """
    Create the RE Manager client and run feed for a queueserver, as the settings describe.

    Parameters
    ----------
    name : str
        Queueserver name, for logging.
    settings : QueueserverSettings
        The queueserver's settings.

    Returns
    -------
    tuple[REManagerAPI, RunFeed]
        The client and the (not yet started) feed.
    """
    api = REManagerAPI(
        zmq_control_addr=settings.zmq_control_addr,
        zmq_info_addr=settings.zmq_info_addr,
        timeout_recv=settings.timeout_recv,
    )
    api.user = settings.user
    api.user_group = settings.user_group
    feed_settings = settings.run_feed
    if isinstance(feed_settings, TiledFeedSettings):
        from tiled.client import from_uri

        container = from_uri(feed_settings.uri, api_key=feed_settings.api_key)
        for segment in filter(None, feed_settings.path.split("/")):
            container = container[segment]
        feed: RunFeed = TiledRunFeed(container, replay=feed_settings.replay, reconnect_delay=feed_settings.reconnect_delay)
    else:
        from bluesky.callbacks.zmq import RemoteDispatcher

        feed = DocumentStreamFeed(RemoteDispatcher(feed_settings.address))
    logger.info("Queueserver %r: %s, run feed %s", name, settings.zmq_control_addr, feed_settings.type)
    return api, feed


@dataclass(frozen=True)
class QueueserverConnection:
    """
    A queueserver's RE Manager client and the router for its completed runs.

    Parameters
    ----------
    name : str
        Name agent specs use.
    settings : QueueserverSettings
        The queueserver's settings.
    api : REManagerAPI
        RE Manager client, shared by every agent on this queueserver.
    router : RunRouter
        Routes the queueserver's completed runs to agents.
    """

    name: str
    settings: QueueserverSettings
    api: Any
    router: RunRouter


class Connections:
    """
    The server's queueserver connections.

    Parameters
    ----------
    settings : ServerSettings
        Deployment settings.
    connect_function : ConnectFunction | None, optional
        Creates the client and feed for each queueserver; defaults to :func:`connect`. Tests pass fakes.
    """

    def __init__(self, settings: ServerSettings, connect_function: ConnectFunction | None = None):
        self._settings = settings
        self._connect = connect_function or connect
        self._connections: dict[str, QueueserverConnection] = {}

    def open(self) -> None:
        """Connect to every queueserver, start its run router and apply its autostart setting."""
        for name, settings in self._settings.queueservers.items():
            api, feed = self._connect(name, settings)
            router = RunRouter(feed)
            router.start()
            self._connections[name] = QueueserverConnection(name=name, settings=settings, api=api, router=router)
            if settings.autostart is not None:
                try:
                    api.queue_autostart(settings.autostart)
                except Exception:
                    # An unreachable queueserver must not keep the server from starting; agents report it later.
                    logger.warning("Could not set autostart=%s on queueserver %r", settings.autostart, name, exc_info=True)

    def close(self) -> None:
        """Stop every run router and close every client."""
        for connection in self._connections.values():
            try:
                connection.router.close()
            except Exception:
                logger.exception("Failed to stop the run feed of queueserver %r", connection.name)
            try:
                connection.api.close()
            except Exception:
                logger.exception("Failed to close the client of queueserver %r", connection.name)
        self._connections.clear()

    def __getitem__(self, name: str) -> QueueserverConnection:
        """Return the connection named ``name``; raise :class:`NotFoundError` if there is none."""
        try:
            return self._connections[name]
        except KeyError:
            raise NotFoundError(f"unknown queueserver {name!r}; configured: {sorted(self._connections)}") from None

    def __iter__(self) -> Iterator[QueueserverConnection]:
        """Iterate over the connections."""
        return iter(list(self._connections.values()))


def flatten_devices(devices: Mapping[str, Any], prefix: str = "") -> dict[str, dict[str, bool]]:
    """
    Flatten the queueserver's nested ``devices_allowed`` description into dotted device paths.

    Parameters
    ----------
    devices : Mapping[str, Any]
        ``devices_allowed`` from the RE Manager: names mapped to descriptions with ``is_movable``,
        ``is_readable``, ``is_flyable`` and nested ``components``.
    prefix : str, optional
        Path of the parent device, with a trailing ``"."``.

    Returns
    -------
    dict[str, dict[str, bool]]
        ``{"dcm_c2.pitch": {"is_movable": True, "is_readable": True, "is_flyable": False}, ...}``.
    """
    flat: dict[str, dict[str, bool]] = {}
    for name, description in devices.items():
        path = f"{prefix}{name}"
        flat[path] = {flag: bool(description.get(flag, False)) for flag in _DEVICE_FLAGS}
        flat.update(flatten_devices(description.get("components") or {}, prefix=f"{path}."))
    return flat


def acquisition_contract_problems(plan: Mapping[str, Any], kwargs: Iterable[str] = ()) -> list[str]:
    """
    Explain why a plan cannot be an agent's acquisition plan.

    The runner calls ``plan(suggestions, actuators, sensors, md=..., **kwargs)``, so the plan must
    take three positional arguments, ``md`` and the extra kwargs by keyword, and require nothing else.

    Parameters
    ----------
    plan : Mapping[str, Any]
        The plan's description from the queueserver's ``plans_allowed``.
    kwargs : Iterable[str], optional
        Names of the extra keyword arguments in the agent spec.

    Returns
    -------
    list[str]
        Problems found; empty when the plan fits.
    """
    parameters = list(plan.get("parameters") or [])
    kinds = {parameter["name"]: (parameter.get("kind") or {}).get("name") for parameter in parameters}
    positional = [name for name, kind in kinds.items() if kind in _POSITIONAL][:3]
    takes_var_positional = "VAR_POSITIONAL" in kinds.values()
    takes_var_keyword = "VAR_KEYWORD" in kinds.values()
    problems: list[str] = []
    if len(positional) < 3 and not takes_var_positional:
        problems.append(f"takes {len(positional)} positional parameters {positional}; needs suggestions, actuators, sensors")
    keywords = ["md", *kwargs]
    for name in keywords:
        if name in positional:
            problems.append(f"{name!r} is passed by keyword but is the plan's positional parameter")
        elif kinds.get(name) not in _KEYWORD and not takes_var_keyword:
            problems.append(f"does not take {name!r} by keyword")
    required = [
        parameter["name"]
        for parameter in parameters
        if "default" not in parameter and (parameter.get("kind") or {}).get("name") not in _VARIADIC
    ]
    unfilled = [name for name in required if name not in positional and name not in keywords]
    if unfilled:
        problems.append(f"requires parameters {unfilled} that the agent does not pass")
    return problems
