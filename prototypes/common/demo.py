"""Helpers shared by the demo scripts: HTTP calls to an adaptive server, progress output and queueserver access."""

import time
from collections.abc import Callable
from typing import Any

import httpx
from bluesky_queueserver_api.zmq import REManagerAPI

from prototypes.common.settings import QUEUESERVER, prototype_settings

FINAL_STATES = ("completed", "stopped", "failed")
"""Runner states that end an operation."""


class DemoError(RuntimeError):
    """A request failed, or the server did not reach the expected state."""


def call(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    """
    Send a request to the server's API.

    Parameters
    ----------
    client : httpx.Client
        Client whose base URL ends in ``/api/v1``.
    method, path : str
        HTTP method and path below the base URL.
    **kwargs : Any
        Passed to :meth:`httpx.Client.request`, e.g. ``json=``.

    Returns
    -------
    Any
        The decoded JSON body, or ``None`` for an empty one.

    Raises
    ------
    DemoError
        If the server answers with an error status.
    """
    response = client.request(method, path, **kwargs)
    if response.is_error:
        raise DemoError(f"{method} {path} -> {response.status_code}: {response.text}")
    return response.json() if response.content else None


def follow(
    client: httpx.Client,
    agent_id: str,
    on_iteration: Callable[[dict[str, Any]], None],
    *,
    shown: int = 0,
    poll: float = 1.0,
    timeout: float = 1800.0,
) -> tuple[dict[str, Any], int]:
    """
    Poll an agent until its operation ends, handing each new iteration record to ``on_iteration``.

    Parameters
    ----------
    client : httpx.Client
        API client.
    agent_id : str
        The agent.
    on_iteration : Callable[[dict[str, Any]], None]
        Called once per iteration record, oldest first.
    shown : int
        Number of history records already handled.
    poll : float
        Seconds between polls.
    timeout : float
        Seconds to wait for the operation to end.

    Returns
    -------
    tuple[dict[str, Any], int]
        The agent's final status and the number of history records handled.

    Raises
    ------
    DemoError
        If the operation does not end in time.
    """
    deadline = time.monotonic() + timeout
    while True:
        status = call(client, "GET", f"/agents/{agent_id}")["status"]
        # History is read after the status, so it holds every iteration of an operation that has ended.
        iterations = call(client, "GET", f"/agents/{agent_id}/history")["iterations"]
        for record in iterations[shown:]:
            on_iteration(record)
        shown = len(iterations)
        if status["state"] in FINAL_STATES:
            return status, shown
        if time.monotonic() > deadline:
            raise DemoError(f"agent {agent_id} is still {status['state']} after {timeout:.0f} s")
        time.sleep(poll)


def re_manager(storage_subdir: str) -> REManagerAPI:
    """
    Connect to the queueserver a prototype server uses.

    Parameters
    ----------
    storage_subdir : str
        The server's name in :func:`~prototypes.common.settings.prototype_settings`.

    Returns
    -------
    REManagerAPI
        A client with the default user, not the server's.
    """
    settings = prototype_settings(storage_subdir).queueservers[QUEUESERVER]
    return REManagerAPI(zmq_control_addr=settings.zmq_control_addr, zmq_info_addr=settings.zmq_info_addr)


def server_user(storage_subdir: str) -> str:
    """Return the queueserver user a prototype server submits plans as."""
    return prototype_settings(storage_subdir).queueservers[QUEUESERVER].user
