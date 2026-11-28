"""Look up Bluesky Queue Server items in the RE Manager."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotations only: the Tiled evaluation helpers must import without the queueserver extra.
    import bluesky_queueserver_api.http
    import bluesky_queueserver_api.zmq


def finished_item_run_uids(
    re_manager_api: "bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI",
    item_uid: str,
) -> list[str] | None:
    """Return the UIDs of the runs a finished queue item opened, or None while the item is queued or running.

    Parameters
    ----------
    re_manager_api : bluesky_queueserver_api.zmq.REManagerAPI | bluesky_queueserver_api.http.REManagerAPI
        The RE Manager running the item.
    item_uid : str
        Queue item UID.

    Returns
    -------
    list[str] | None
        The ``run_uids`` of the ``result`` in the item's history entry, in start order, or None while the item is
        queued or running.

    Raises
    ------
    RuntimeError
        If the item ended without opening a run.
    KeyError
        If the RE Manager has no record of the item.
    """
    # Items move from queue to running to history, and each response is consistent, so this order misses none.
    # A failed item is re-queued under a new UID; its original UID stays in the history.
    queue = re_manager_api.queue_get(reload=True)
    if queue["running_item"].get("item_uid") == item_uid or any(item["item_uid"] == item_uid for item in queue["items"]):
        return None
    for item in reversed(re_manager_api.history_get(reload=True)["items"]):
        if item["item_uid"] == item_uid:
            result = item["result"]
            if not result["run_uids"]:
                raise RuntimeError(
                    f"Queue item {item_uid!r} ended with status {result['exit_status']!r} without opening a run: "
                    f"{result.get('msg') or '(no message)'}"
                )
            return list(result["run_uids"])
    raise KeyError(
        f"The RE Manager has no record of queue item {item_uid!r}. Check whether it was removed from the queue "
        "or the history was cleared."
    )
