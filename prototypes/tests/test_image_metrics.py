"""Tests for prototypes.common.image_metrics with fake run objects."""

from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from prototypes.common.image_metrics import (
    IMAGE_METRICS,
    METRICS,
    ImageMetricsEvaluation,
    ImageMetricsParams,
    build_image_metrics,
    event_outcomes_from_run,
    image_metric_outputs,
    screen_metrics,
)
from prototypes.common.tiled_data import read_images

HEIGHT, WIDTH = 64, 80


class FakeArray:
    def __init__(self, data):
        self.data = np.asarray(data)

    def read(self):
        return self.data


class FakeRun(dict):
    def __init__(self, streams, start=None):
        super().__init__({name: {key: FakeArray(value) for key, value in keys.items()} for name, keys in streams.items()})
        self.metadata = {"start": start or {}}


def gaussian(row, col, sigma=3.0, amplitude=100.0):
    yy, xx = np.indices((HEIGHT, WIDTH))
    return amplitude * np.exp(-((yy - row) ** 2 + (xx - col) ** 2) / (2 * sigma**2))


def test_gaussian_spot_metrics():
    image = gaussian(20.0, 50.0, sigma=3.0)
    metrics = screen_metrics(image)
    assert set(metrics) == set(METRICS)
    assert metrics["total"] == pytest.approx(image.sum())
    assert metrics["peak"] == pytest.approx(100.0)
    assert metrics["x_centroid"] == pytest.approx(50.0, abs=0.05)
    assert metrics["y_centroid"] == pytest.approx(20.0, abs=0.05)
    assert metrics["x_sigma_px"] == pytest.approx(3.0, rel=0.02)
    assert metrics["y_sigma_px"] == pytest.approx(3.0, rel=0.02)
    assert metrics["x_fwhm_px"] == pytest.approx(2.3548 * 3.0, rel=0.05)
    expected_radius = np.hypot(50.0 - (WIDTH - 1) / 2, 20.0 - (HEIGHT - 1) / 2)
    assert metrics["centroid_radius_px"] == pytest.approx(expected_radius, abs=0.05)


def test_empty_image_penalties():
    metrics = screen_metrics(np.zeros((HEIGHT, WIDTH)), threshold=0.5, blur=1.0)
    assert all(np.isfinite(value) for value in metrics.values())
    assert metrics == {
        "total": 0.0,
        "peak": 0.0,
        "x_centroid": -1.0,
        "y_centroid": -1.0,
        "centroid_radius_px": pytest.approx(np.hypot(WIDTH, HEIGHT)),
        "x_sigma_px": WIDTH,
        "y_sigma_px": HEIGHT,
        "x_fwhm_px": WIDTH,
        "y_fwhm_px": HEIGHT,
    }


def test_external_stack_normalizes_and_ignores_nan():
    image = gaussian(30.0, 40.0)
    noisy = image.copy()
    noisy[0, 0] = np.nan
    noisy[1, 1] = np.inf
    run = FakeRun({"primary": {"diag2_image": np.stack([noisy, image])[:, np.newaxis]}})
    stack = read_images(run, "primary", "diag2_image")
    assert stack.shape == (2, HEIGHT, WIDTH)
    assert np.isfinite(stack).all()
    rows = event_outcomes_from_run(run, ImageMetricsParams(screens=[{"image_key": "diag2_image", "prefix": "diag2"}]))
    assert len(rows) == 2
    assert rows[0]["diag2_total"] == pytest.approx(rows[1]["diag2_total"])
    assert rows[0]["diag2_y_centroid"] == pytest.approx(30.0, abs=0.05)


def test_missing_key_names_available_keys():
    run = FakeRun({"primary": {"diag2_image": np.zeros((1, HEIGHT, WIDTH))}})
    with pytest.raises(KeyError, match="diag2_image"):
        read_images(run, "primary", "diag1_image")
    with pytest.raises(KeyError, match="primary"):
        read_images(run, "baseline", "diag2_image")


def test_screens_must_have_equal_event_counts():
    run = FakeRun({"primary": {"a_image": np.zeros((2, 4, 4)), "b_image": np.zeros((3, 4, 4))}})
    params = ImageMetricsParams(screens=[{"image_key": "a_image", "prefix": "a"}, {"image_key": "b_image", "prefix": "b"}])
    with pytest.raises(ValueError, match="different numbers of events"):
        event_outcomes_from_run(run, params)


def test_evaluation_maps_events_to_recorded_suggestion_ids():
    images = np.stack([gaussian(10.0, 10.0), gaussian(40.0, 60.0)])[:, np.newaxis]
    start = {"blop_suggestions": [{"_id": "b", "x": 2.0}, {"_id": "a", "x": 1.0}]}
    run = FakeRun({"primary": {"diag2_image": images}}, start=start)
    params = ImageMetricsParams(screens=[{"image_key": "diag2_image", "prefix": "diag2", "metrics": ["y_centroid"]}])
    evaluation = ImageMetricsEvaluation(params, run_loader={"run-1": run}.__getitem__)
    outcomes = evaluation("run-1", [{"_id": "a", "x": 1.0}, {"_id": "b", "x": 2.0}])
    assert [outcome["_id"] for outcome in outcomes] == ["b", "a"]
    assert set(outcomes[0]) == {"_id", "diag2_y_centroid"}
    assert outcomes[0]["diag2_y_centroid"] == pytest.approx(10.0, abs=0.05)
    assert outcomes[1]["diag2_y_centroid"] == pytest.approx(40.0, abs=0.05)


def test_evaluation_rejects_count_mismatch_and_missing_suggestions():
    params = ImageMetricsParams(screens=[{"image_key": "diag2_image", "prefix": "diag2"}])
    images = np.zeros((1, 4, 4))
    mismatch = FakeRun({"primary": {"diag2_image": images}}, start={"blop_suggestions": [{"_id": 0}, {"_id": 1}]})
    with pytest.raises(ValueError, match="1 events"):
        ImageMetricsEvaluation(params, run_loader=lambda uid: mismatch)("r", [{"_id": 0}, {"_id": 1}])
    missing = FakeRun({"primary": {"diag2_image": images}}, start={})
    with pytest.raises(ValueError, match="blop_suggestions"):
        ImageMetricsEvaluation(params, run_loader=lambda uid: missing)("r", [{"_id": 0}])


def test_outputs_and_params_validation():
    params = ImageMetricsParams(
        screens=[
            {"image_key": "diag1_image", "prefix": "diag1", "metrics": ["y_centroid"]},
            {"image_key": "diag2_image", "prefix": "diag2", "metrics": ["total", "x_centroid"]},
        ]
    )
    assert image_metric_outputs(params) == ["diag1_y_centroid", "diag2_total", "diag2_x_centroid"]
    assert list(IMAGE_METRICS.outputs(params)) == image_metric_outputs(params)
    default = ImageMetricsParams(screens=[{"image_key": "diag2_image", "prefix": "diag2"}])
    assert image_metric_outputs(default) == [f"diag2_{metric}" for metric in METRICS]
    with pytest.raises(ValidationError):
        ImageMetricsParams(screens=[{"image_key": "a", "prefix": "d"}, {"image_key": "b", "prefix": "d"}])
    with pytest.raises(ValidationError):
        ImageMetricsParams(screens=[{"image_key": "a", "prefix": "diag-2"}])
    with pytest.raises(ValidationError):
        ImageMetricsParams(screens=[{"image_key": "a", "prefix": "d", "threshold": 1.5}])
    with pytest.raises(ValidationError):
        ImageMetricsParams(screens=[{"image_key": "a", "prefix": "d"}], extra=1)
    with pytest.raises(ValidationError):
        ImageMetricsParams(screens=[])


def test_build_checks_image_keys_against_sensors():
    params = ImageMetricsParams(screens=[{"image_key": "diag2_image", "prefix": "diag2"}])
    assert isinstance(build_image_metrics(SimpleNamespace(sensors=("diag2",)), params), ImageMetricsEvaluation)
    with pytest.raises(ValueError, match="diag1"):
        build_image_metrics(SimpleNamespace(sensors=("diag1", "dcm_c2.pitch")), params)
    dotted = ImageMetricsParams(screens=[{"image_key": "bpm-cam_image", "prefix": "bpm"}])
    assert isinstance(build_image_metrics(SimpleNamespace(sensors=("bpm.cam",)), dotted), ImageMetricsEvaluation)
