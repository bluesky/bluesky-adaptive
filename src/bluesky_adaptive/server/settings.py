"""Deployment settings for the adaptive server."""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TiledFeedSettings(_Settings):
    """
    Learn about completed runs from a Tiled container that a ``TiledWriter`` writes to.

    Parameters
    ----------
    uri : str
        Tiled server URI, e.g. ``"http://localhost:8000"``.
    api_key : str | None
        API key; keep secrets in the environment and fill this in when loading settings.
    path : str
        Path of the container runs are written to, ``""`` for the root.
    replay : bool
        Replay updates the server still holds when the server starts.
    reconnect_delay : float
        Seconds between subscription attempts after a failure.
    """

    type: Literal["tiled"] = "tiled"
    uri: str
    api_key: str | None = None
    path: str = ""
    replay: bool = False
    reconnect_delay: float = Field(default=5.0, gt=0)


class ZmqFeedSettings(_Settings):
    """
    Learn about completed runs from a Bluesky document stream published over ZMQ.

    Parameters
    ----------
    address : str
        ``"host:port"`` of the ZMQ proxy's outbound side.
    """

    type: Literal["zmq"] = "zmq"
    address: str


RunFeedSettings = Annotated[TiledFeedSettings | ZmqFeedSettings, Field(discriminator="type")]
"""Where completed runs come from, discriminated by ``type``."""


class QueueserverSettings(_Settings):
    """
    One queueserver agents may submit to.

    Parameters
    ----------
    zmq_control_addr, zmq_info_addr : str
        RE Manager ZMQ addresses.
    timeout_recv : float
        Seconds to wait for an RE Manager reply.
    user, user_group : str
        Identity plans are submitted under; ``user_group`` also selects allowed plans and devices.
    autostart : bool | None
        Queue autostart to apply once at startup; ``None`` leaves it as it is.
    run_feed : TiledFeedSettings | ZmqFeedSettings
        Where completed runs come from.
    """

    zmq_control_addr: str = "tcp://localhost:60615"
    zmq_info_addr: str = "tcp://localhost:60625"
    timeout_recv: float = Field(default=10.0, gt=0)
    user: str = "bluesky-adaptive"
    user_group: str = "primary"
    autostart: bool | None = None
    run_feed: RunFeedSettings


class ServerSettings(_Settings):
    """
    Deployment settings.

    Parameters
    ----------
    storage_dir : Path
        Directory for agent files such as optimizer checkpoints. One server process per directory.
    queueservers : dict[str, QueueserverSettings]
        Queueservers by the name agent specs use.
    """

    storage_dir: Path
    queueservers: dict[str, QueueserverSettings] = Field(min_length=1)

    @classmethod
    def from_file(cls, path: str | Path) -> "ServerSettings":
        """
        Load settings from a JSON or YAML file.

        Parameters
        ----------
        path : str | Path
            ``.json``, ``.yml`` or ``.yaml`` file.

        Returns
        -------
        ServerSettings
            The validated settings.
        """
        path = Path(path)
        text = path.read_text()
        if path.suffix in (".yml", ".yaml"):
            import yaml

            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        return cls.model_validate(data)
