"""
Beam metrics from 2D screen images, shared by evaluation and ingestion.

Per-event metrics (flux, centroid, size) are computed for every configured screen of a run. Evaluation maps
event ``i`` to the suggestion recorded at position ``i`` of the start document's ``blop_suggestions``;
ingestion pairs the same rows with DOF readbacks.

Image rows are vertical and columns horizontal: ``x`` metrics use the column axis and ``y`` metrics the
row axis, in pixels.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, get_args

import numpy as np
from numpy.typing import ArrayLike
from pydantic import BaseModel, ConfigDict, Field, field_validator
from scipy import ndimage, signal

from bluesky_adaptive.protocols import ID_KEY
from bluesky_adaptive.server.catalog import EvaluationEntry
from bluesky_adaptive.server.problem import ProblemDefinition
from prototypes.common.tiled_data import read_images, run_client

logger = logging.getLogger(__name__)

SUGGESTIONS_KEY = "blop_suggestions"
"""Start-document key holding the suggestions in acquisition order."""

MetricName = Literal[
    "total",
    "peak",
    "x_centroid",
    "y_centroid",
    "centroid_radius_px",
    "x_sigma_px",
    "y_sigma_px",
    "x_fwhm_px",
    "y_fwhm_px",
]
METRICS: tuple[MetricName, ...] = get_args(MetricName)
"""Metric names :func:`screen_metrics` returns, in output order."""


# The image math below is vendored from tst-sim-tools/src/tst_sim_tools/analysis/image.py
# (_finite_float_array, fwhm, threshold_image, gaussian_blur, preprocess, locate_centroid, analyze_image),
# with analyze_image's "no beam" error replaced by finite penalties so an optimizer never sees NaN.


def _finite_float_array(values: ArrayLike) -> np.ndarray:
    array = np.array(values, dtype=float, copy=True)
    array[~np.isfinite(array)] = 0.0
    return array


def fwhm(profile: ArrayLike) -> float:
    """
    Compute the full width at half maximum of a 1D marginal profile.

    Parameters
    ----------
    profile : ArrayLike
        One-dimensional intensity profile.

    Returns
    -------
    float
        Width in pixels. Profiles with no positive signal, no peak, or that fill the whole profile return the
        profile length as a finite penalty.
    """
    values = np.asarray(profile, dtype=float)
    peak = float(values.max())
    if peak <= 0.0 or np.all(values >= peak / 2.0):
        return float(values.size)
    peaks, _ = signal.find_peaks(values)
    if peaks.size == 0:
        return float(values.size)
    peak_index = int(peaks[np.argmax(values[peaks])])
    width = float(signal.peak_widths(values, [peak_index], rel_height=0.5)[0][0])
    return width if width > 0.0 else float(values.size)


def threshold_image(image: ArrayLike, threshold: float = 0.0) -> np.ndarray:
    """
    Zero pixels at or below a fraction of the image peak.

    Parameters
    ----------
    image : ArrayLike
        Two-dimensional image.
    threshold : float
        Fraction of the peak, in [0, 1].

    Returns
    -------
    numpy.ndarray
        Thresholded float copy with non-finite pixels set to zero.

    Raises
    ------
    ValueError
        If ``threshold`` is outside [0, 1].
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"expected threshold in [0, 1], got {threshold}")
    thresholded = _finite_float_array(image)
    peak = float(thresholded.max())
    if peak <= 0.0:
        thresholded.fill(0.0)
        return thresholded
    thresholded[thresholded <= peak * threshold] = 0.0
    return thresholded


def gaussian_blur(image: ArrayLike, sigma: float = 0.0, truncate: float = 4.0) -> np.ndarray:
    """
    Apply a Gaussian blur.

    Parameters
    ----------
    image : ArrayLike
        Two-dimensional image.
    sigma : float
        Standard deviation in pixels; zero returns a finite copy.
    truncate : float
        Kernel half-width in units of ``sigma``.

    Returns
    -------
    numpy.ndarray
        Blurred float image.

    Raises
    ------
    ValueError
        If ``sigma`` is negative or ``truncate`` is not positive.
    """
    if sigma < 0.0:
        raise ValueError(f"expected non-negative sigma, got {sigma}")
    if truncate <= 0.0:
        raise ValueError(f"expected positive truncate, got {truncate}")
    blurred = _finite_float_array(image)
    if sigma == 0.0:
        return blurred
    return ndimage.gaussian_filter(blurred, sigma=sigma, mode="nearest", truncate=truncate)


def preprocess(image: ArrayLike, threshold: float = 0.0, blur: float = 0.0) -> np.ndarray:
    """
    Threshold, then blur, an image before metric extraction.

    Parameters
    ----------
    image : ArrayLike
        Two-dimensional image.
    threshold : float
        Fraction of the peak to retain, in [0, 1].
    blur : float
        Gaussian blur sigma in pixels.

    Returns
    -------
    numpy.ndarray
        Preprocessed float image.
    """
    return gaussian_blur(threshold_image(image, threshold=threshold), sigma=blur)


def screen_metrics(image: ArrayLike, threshold: float = 0.0, blur: float = 0.0) -> dict[str, float]:
    """
    Compute beam intensity, centroid and size metrics for one image.

    Parameters
    ----------
    image : ArrayLike
        Two-dimensional image; rows are ``y``, columns are ``x``.
    threshold : float
        Fraction of the peak to retain before blurring, in [0, 1].
    blur : float
        Gaussian blur sigma in pixels.

    Returns
    -------
    dict[str, float]
        One finite value per name in :data:`METRICS`. ``centroid_radius_px`` is the centroid's distance from
        the image center. An image with no positive signal gets penalties: ``total`` and ``peak`` 0, centroids
        -1, radius the image diagonal, ``x`` widths the image width and ``y`` widths the image height.

    Raises
    ------
    ValueError
        If ``image`` is not two-dimensional, or ``threshold``/``blur`` are out of range.
    """
    processed = preprocess(image, threshold=threshold, blur=blur)
    if processed.ndim != 2:
        raise ValueError(f"expected a 2D image, got shape {processed.shape}")
    height, width = processed.shape
    total = float(processed.sum())
    if total <= 0.0:
        return {
            "total": 0.0,
            "peak": 0.0,
            "x_centroid": -1.0,
            "y_centroid": -1.0,
            "centroid_radius_px": float(np.hypot(width, height)),
            "x_sigma_px": float(width),
            "y_sigma_px": float(height),
            "x_fwhm_px": float(width),
            "y_fwhm_px": float(height),
        }
    y_centroid, x_centroid = (float(value) for value in np.asarray(ndimage.center_of_mass(processed), dtype=float))
    yy, xx = np.indices(processed.shape)
    return {
        "total": total,
        "peak": float(processed.max()),
        "x_centroid": x_centroid,
        "y_centroid": y_centroid,
        "centroid_radius_px": float(np.hypot(x_centroid - (width - 1) / 2.0, y_centroid - (height - 1) / 2.0)),
        "x_sigma_px": float(np.sqrt((processed * (xx - x_centroid) ** 2).sum() / total)),
        "y_sigma_px": float(np.sqrt((processed * (yy - y_centroid) ** 2).sum() / total)),
        "x_fwhm_px": fwhm(processed.sum(axis=0)),
        "y_fwhm_px": fwhm(processed.sum(axis=1)),
    }


class ScreenSpec(BaseModel):
    """
    One screen to analyze.

    Parameters
    ----------
    image_key : str
        Image data key in the stream, e.g. ``"diag2_image"``.
    prefix : str
        Output name prefix, e.g. ``"diag2"`` gives ``diag2_total``.
    metrics : list[MetricName]
        Metrics to report, from :data:`METRICS`; all by default.
    threshold : float
        Fraction of the peak to retain before blurring, in [0, 1].
    blur : float
        Gaussian blur sigma in pixels.
    """

    model_config = ConfigDict(extra="forbid")

    image_key: str = Field(min_length=1)
    prefix: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    metrics: list[MetricName] = Field(default_factory=lambda: list(METRICS), min_length=1)
    threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    blur: float = Field(default=0.0, ge=0.0)

    @field_validator("metrics")
    @classmethod
    def _unique_metrics(cls, metrics: list[MetricName]) -> list[MetricName]:
        duplicates = sorted({metric for metric in metrics if metrics.count(metric) > 1})
        if duplicates:
            raise ValueError(f"metrics must be unique, got duplicates {duplicates}")
        return metrics


class ImageMetricsParams(BaseModel):
    """
    Parameters of the ``image-metrics`` evaluation.

    Parameters
    ----------
    screens : list[ScreenSpec]
        Screens to analyze; prefixes must be unique.
    stream : str
        Stream holding the images, one event per acquired point.
    """

    model_config = ConfigDict(extra="forbid")

    screens: list[ScreenSpec] = Field(min_length=1)
    stream: str = "primary"

    @field_validator("screens")
    @classmethod
    def _unique_prefixes(cls, screens: list[ScreenSpec]) -> list[ScreenSpec]:
        prefixes = [screen.prefix for screen in screens]
        duplicates = sorted({prefix for prefix in prefixes if prefixes.count(prefix) > 1})
        if duplicates:
            raise ValueError(f"screen prefixes must be unique, got duplicates {duplicates}")
        return screens


def image_metric_outputs(params: ImageMetricsParams) -> list[str]:
    """
    Name the outputs the evaluation produces.

    Parameters
    ----------
    params : ImageMetricsParams
        Evaluation parameters.

    Returns
    -------
    list[str]
        ``f"{prefix}_{metric}"`` for each screen and metric, in order.
    """
    return [f"{screen.prefix}_{metric}" for screen in params.screens for metric in screen.metrics]


def event_outcomes_from_run(run: Any, params: ImageMetricsParams) -> list[dict[str, float]]:
    """
    Compute per-event metrics for every screen of a run container.

    Parameters
    ----------
    run : Any
        A run container, as returned by :func:`prototypes.common.tiled_data.run_client`.
    params : ImageMetricsParams
        Screens, metrics and stream.

    Returns
    -------
    list[dict[str, float]]
        One dict per event, keyed by :func:`image_metric_outputs`.

    Raises
    ------
    KeyError
        If the stream or an image key is missing.
    ValueError
        If the screens have different numbers of events.
    """
    rows: list[dict[str, float]] | None = None
    counts: dict[str, int] = {}
    for screen in params.screens:
        images = read_images(run, params.stream, screen.image_key)
        counts[screen.image_key] = len(images)
        if rows is None:
            rows = [{} for _ in images]
        elif len(images) != len(rows):
            raise ValueError(f"screens have different numbers of events in stream {params.stream!r}: {counts}")
        for row, image in zip(rows, images, strict=True):
            values = screen_metrics(image, threshold=screen.threshold, blur=screen.blur)
            row.update({f"{screen.prefix}_{metric}": values[metric] for metric in screen.metrics})
    return rows or []


def event_outcomes(uid: str, params: ImageMetricsParams) -> list[dict[str, float]]:
    """
    Compute per-event metrics for every screen of a run in Tiled.

    Parameters
    ----------
    uid : str
        Run UID.
    params : ImageMetricsParams
        Screens, metrics and stream.

    Returns
    -------
    list[dict[str, float]]
        One dict per event, keyed by :func:`image_metric_outputs`.
    """
    return event_outcomes_from_run(run_client(uid), params)


class ImageMetricsEvaluation:
    """
    Evaluation function mapping each event of a run to the suggestion acquired there.

    Parameters
    ----------
    params : ImageMetricsParams
        Screens, metrics and stream.
    run_loader : Callable[[str], Any]
        Returns the run container for a UID; defaults to Tiled.
    """

    def __init__(self, params: ImageMetricsParams, run_loader: Callable[[str], Any] = run_client):
        self.params = params
        self._run_loader = run_loader

    def __call__(self, uid: str, suggestions: Sequence[Mapping]) -> list[dict[str, Any]]:
        """
        Compute outcomes of one run.

        Parameters
        ----------
        uid : str
            Run UID.
        suggestions : Sequence[Mapping]
            The suggestions submitted; their order is not used, the start document records the acquisition order.

        Returns
        -------
        list[dict[str, Any]]
            ``{"_id": ..., **metrics}`` per event, in acquisition order.

        Raises
        ------
        ValueError
            If the start document lacks ``blop_suggestions`` or a suggestion id, or the number of events differs
            from the number of recorded suggestions.
        """
        run = self._run_loader(uid)
        start = run.metadata.get("start") or {}
        recorded = start.get(SUGGESTIONS_KEY)
        if not isinstance(recorded, Sequence) or isinstance(recorded, str):
            raise ValueError(f"run {uid!r} start document has no {SUGGESTIONS_KEY!r} list")
        ids = []
        for index, suggestion in enumerate(recorded):
            if not isinstance(suggestion, Mapping) or ID_KEY not in suggestion:
                raise ValueError(f"run {uid!r} {SUGGESTIONS_KEY}[{index}] has no {ID_KEY!r}: {suggestion!r}")
            ids.append(suggestion[ID_KEY])
        rows = event_outcomes_from_run(run, self.params)
        if len(rows) != len(ids):
            message = f"run {uid!r} has {len(rows)} events in stream {self.params.stream!r}"
            raise ValueError(f"{message} but recorded {len(ids)} suggestions")
        if len(ids) != len(suggestions):
            logger.warning("run %s recorded %d suggestions, %d were submitted", uid, len(ids), len(suggestions))
        return [{ID_KEY: suggestion_id, **row} for suggestion_id, row in zip(ids, rows, strict=True)]


def _sensor_matches(image_key: str, sensor_name: str) -> bool:
    return image_key == sensor_name or image_key.startswith((f"{sensor_name}_", f"{sensor_name}-"))


def build_image_metrics(problem: ProblemDefinition, params: ImageMetricsParams) -> ImageMetricsEvaluation:
    """
    Build the ``image-metrics`` evaluation for a problem.

    Parameters
    ----------
    problem : ProblemDefinition
        The validated problem; its sensors must produce the configured images.
    params : ImageMetricsParams
        Screens, metrics and stream.

    Returns
    -------
    ImageMetricsEvaluation
        The evaluation function.

    Raises
    ------
    ValueError
        If a screen's image key belongs to none of the problem's sensors.
    """
    sensor_names = [sensor.replace(".", "-") for sensor in problem.sensors]
    for screen in params.screens:
        if not any(_sensor_matches(screen.image_key, name) for name in sensor_names):
            raise ValueError(
                f"image key {screen.image_key!r} does not belong to any sensor; "
                f"sensors (ophyd names): {sensor_names or '(none)'}"
            )
    return ImageMetricsEvaluation(params)


IMAGE_METRICS: EvaluationEntry[ImageMetricsParams] = EvaluationEntry(
    "image-metrics",
    version="1",
    params=ImageMetricsParams,
    build=build_image_metrics,
    outputs=image_metric_outputs,
    description="Per-event beam metrics (flux, centroid, size) from 2D screen images",
)
