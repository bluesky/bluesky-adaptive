"""
Read Bluesky runs written to Tiled by ``TiledWriter``.

Runs live at the catalog root keyed by run UID. Each run holds one container per stream (``primary``,
``baseline``, ...), and each stream holds one array per data key, whether the data was recorded inline
in the event documents (scalars such as ``dcm_c2-pitch``) or externally in HDF5 files (screen images
such as ``diag2_image``, read back as ``(N, 1, H, W)``).
"""

import functools
import logging
from typing import Any

import numpy as np
from numpy.typing import ArrayLike
from tiled.client import from_uri

from prototypes.common.settings import tiled_api_key, tiled_uri

logger = logging.getLogger(__name__)


@functools.cache
def tiled_client() -> Any:
    """
    Connect to the Tiled catalog named by the environment, once per process.

    Returns
    -------
    tiled.client.container.Container
        The catalog root at ``TILED_URI``, opened with ``TILED_API_KEY`` (see :mod:`prototypes.common.settings`).
    """
    uri = tiled_uri()
    logger.info("Connecting to Tiled at %s", uri)
    return from_uri(uri, api_key=tiled_api_key())


def run_client(uid: str) -> Any:
    """
    Return the Tiled container of one Bluesky run.

    Parameters
    ----------
    uid : str
        The run's start-document UID.

    Returns
    -------
    bluesky_tiled_plugins.clients.bluesky_run.BlueskyRunV3
        The run container; its keys are stream names.

    Raises
    ------
    KeyError
        If no run with ``uid`` exists in the catalog.
    """
    try:
        return tiled_client()[uid]
    except KeyError as err:
        raise KeyError(f"run {uid!r} not found in Tiled") from err


def start_of(uid: str) -> dict[str, Any]:
    """
    Return the start document of one Bluesky run.

    Parameters
    ----------
    uid : str
        The run's start-document UID.

    Returns
    -------
    dict[str, Any]
        A plain copy of the run's ``metadata["start"]``.

    Raises
    ------
    KeyError
        If the run does not exist or has no start document.
    """
    metadata = run_client(uid).metadata
    if "start" not in metadata:
        raise KeyError(f"run {uid!r} has no start document in its metadata")
    return dict(metadata["start"])


def stream_column(run: Any, stream: str, key: str) -> np.ndarray:
    """
    Read one data key of one stream of a run.

    Parameters
    ----------
    run : Any
        A run container, as returned by :func:`run_client`; any mapping of stream name to a mapping of
        data key to an object with ``read()`` works.
    stream : str
        Stream name, e.g. ``"primary"`` or ``"baseline"``.
    key : str
        Data key, e.g. ``"dcm_c2-pitch"`` or ``"diag2_image"``.

    Returns
    -------
    numpy.ndarray
        One row per event; inline and external data read the same way.

    Raises
    ------
    KeyError
        If the stream or the data key is missing; the message lists what is available.
    """
    try:
        node = run[stream]
    except KeyError as err:
        raise KeyError(f"run has no stream {stream!r}; streams: {sorted(run)}") from err
    try:
        column = node[key]
    except KeyError as err:
        raise KeyError(f"stream {stream!r} has no data key {key!r}; keys: {sorted(node)}") from err
    return np.asarray(column.read())


def image_series(images: ArrayLike) -> np.ndarray:
    """
    Normalize detector data to an ``(N, height, width)`` float stack with non-finite pixels set to zero.

    Adapted from ``image_series`` in ``tst-sim-tools/src/tst_sim_tools/analysis/image.py``.

    Parameters
    ----------
    images : ArrayLike
        One ``(H, W)`` image, an ``(N, H, W)`` stack, or an ``(N, M, H, W)`` external-image stack; ``M`` frames
        per event are squeezed when ``M == 1`` and summed otherwise.

    Returns
    -------
    numpy.ndarray
        Owned float array of shape ``(N, H, W)``.

    Raises
    ------
    ValueError
        If the data has another number of dimensions.
    """
    stack = np.array(images, dtype=float, copy=True)
    stack[~np.isfinite(stack)] = 0.0
    if stack.ndim == 2:
        return stack[np.newaxis, :, :]
    if stack.ndim == 4:
        return stack[:, 0, :, :] if stack.shape[1] == 1 else stack.sum(axis=1)
    if stack.ndim != 3:
        raise ValueError(f"expected a 2D image, 3D stack or 4D external stack, got shape {stack.shape}")
    return stack


def read_images(run: Any, stream: str, key: str) -> np.ndarray:
    """
    Read one image data key of a run as an ``(N, H, W)`` float stack.

    Parameters
    ----------
    run : Any
        A run container, as for :func:`stream_column`.
    stream : str
        Stream name.
    key : str
        Image data key, e.g. ``"diag2_image"``.

    Returns
    -------
    numpy.ndarray
        Float stack of shape ``(N, H, W)`` with NaN and infinite pixels set to zero.

    Raises
    ------
    KeyError
        If the stream or the data key is missing.
    ValueError
        If the data is not 2D, 3D or 4D.
    """
    return image_series(stream_column(run, stream, key))
