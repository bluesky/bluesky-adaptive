"""
Settings shared by the prototype servers, read from environment variables.

=================  ==============================================  ==========================================
Variable           Default                                         Meaning
=================  ==============================================  ==========================================
``BA_QS_CONTROL``  ``tcp://localhost:60615``                       RE Manager control address
``BA_QS_INFO``     ``tcp://localhost:60625``                       RE Manager info address
``TILED_URI``      ``http://localhost:8000``                       Tiled server that ``TiledWriter`` writes to
``TILED_API_KEY``  ``secret``                                      Tiled API key
``BA_STORAGE_DIR`` ``$PROTOTYPE_ROOT/servers``                     Parent of each server's storage directory
``PROTOTYPE_ROOT`` ``/tmp/bluesky-adaptive-prototypes``            Deployment state root (see ``deploy/``)
``BA_LOG_LEVEL``   ``INFO``                                        Level of the servers' log output
=================  ==============================================  ==========================================
"""

import logging
import os
from pathlib import Path

from bluesky_adaptive.server import QueueserverSettings, ServerSettings, TiledFeedSettings

QUEUESERVER = "tst"
"""Name agent specs use for the simulated beamline's queueserver."""


def configure_logging() -> None:
    """Send log records at ``BA_LOG_LEVEL`` and above to stderr; Uvicorn configures only its own loggers."""
    logging.basicConfig(
        level=os.environ.get("BA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def tiled_uri() -> str:
    """Return the Tiled server URI."""
    return os.environ.get("TILED_URI", "http://localhost:8000")


def tiled_api_key() -> str | None:
    """Return the Tiled API key."""
    return os.environ.get("TILED_API_KEY", "secret")


def prototype_settings(storage_subdir: str) -> ServerSettings:
    """
    Build the settings of one prototype server.

    Parameters
    ----------
    storage_subdir : str
        The server's own directory under ``BA_STORAGE_DIR``; also names its queueserver user.

    Returns
    -------
    ServerSettings
        One queueserver, :data:`QUEUESERVER`, whose completed runs come from Tiled.
    """
    root = Path(os.environ.get("PROTOTYPE_ROOT", "/tmp/bluesky-adaptive-prototypes"))
    storage_dir = Path(os.environ.get("BA_STORAGE_DIR", str(root / "servers"))) / storage_subdir
    return ServerSettings(
        storage_dir=storage_dir,
        queueservers={
            QUEUESERVER: QueueserverSettings(
                zmq_control_addr=os.environ.get("BA_QS_CONTROL", "tcp://localhost:60615"),
                zmq_info_addr=os.environ.get("BA_QS_INFO", "tcp://localhost:60625"),
                user=f"bluesky-adaptive-{storage_subdir}",
                run_feed=TiledFeedSettings(uri=tiled_uri(), api_key=tiled_api_key()),
            )
        },
    )
