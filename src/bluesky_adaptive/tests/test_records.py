from unittest.mock import MagicMock

import numpy as np
import pytest
from event_model import EventModelValidationError

from bluesky_adaptive.protocols import Optimizer
from bluesky_adaptive.records import ACQUISITION_FIELD, ID_FIELD, ITERATION_FIELD, RecordRun


def _collector():
    documents: list[tuple[str, dict]] = []

    def callback(name, doc):
        documents.append((name, dict(doc)))

    return callback, documents


def test_record_run_infers_fields_and_matches_outcomes_by_id():
    callback, documents = _collector()
    record = RecordRun(
        MagicMock(spec=Optimizer),
        callbacks=[callback],
        metadata={"plan_name": "test"},
        optimization_uid="opt-1",
    )

    record.record(
        0,
        "acq",
        [{"_id": 1, "x": 0.5}, {"_id": 2, "x": 0.7}],
        [{"_id": 2, "y": 2.0}, {"_id": 1, "y": 1.0}],
    )
    record.stop()

    assert [name for name, _ in documents] == ["start", "descriptor", "event", "event", "stop"]
    assert documents[0][1]["adaptive"]["optimization_uid"] == "opt-1"
    descriptor = documents[1][1]
    assert descriptor["name"] == "primary"
    assert descriptor["object_keys"] == {"parameters": ["x"], "outcomes": ["y"]}
    assert set(descriptor["data_keys"]) == {ID_FIELD, ITERATION_FIELD, ACQUISITION_FIELD, "x", "y"}
    assert descriptor["data_keys"][ID_FIELD]["dtype"] == "string"
    assert documents[2][1]["data"] == {
        ID_FIELD: "1",
        ITERATION_FIELD: 0,
        ACQUISITION_FIELD: "acq",
        "x": 0.5,
        "y": 1.0,
    }
    assert documents[3][1]["data"] == {
        ID_FIELD: "2",
        ITERATION_FIELD: 0,
        ACQUISITION_FIELD: "acq",
        "x": 0.7,
        "y": 2.0,
    }
    assert documents[4][1]["exit_status"] == "success"
    assert documents[4][1]["num_events"] == {"primary": 2}


class _DescribableConfigurableOptimizer:
    def describe(self):
        return {
            "x": {"source": "test", "dtype": "number", "shape": [], "units": "mm"},
            "y": {"source": "test", "dtype": "number", "shape": []},
        }

    def read_configuration(self):
        return {"seed": {"value": 3, "timestamp": 0.0}}

    def describe_configuration(self):
        return {"seed": {"source": "test", "dtype": "number", "shape": []}}


def test_record_run_uses_declared_fields_and_configuration():
    callback, documents = _collector()
    record = RecordRun(
        _DescribableConfigurableOptimizer(),
        callbacks=[callback],
        metadata={"plan_name": "test"},
        optimization_uid="opt-1",
    )

    record.record(0, "acq", [{"_id": 1, "x": 0.5}], [{"_id": 1, "y": 1.0}])

    descriptor = documents[1][1]
    assert descriptor["data_keys"]["x"]["units"] == "mm"
    assert descriptor["configuration"]["optimizer"]["data"] == {"seed": 3}


@pytest.mark.parametrize(
    ("suggestions", "outcomes"),
    [
        ([{"_id": 1, "adaptive_x": 0.5}], [{"_id": 1, "y": 1.0}]),
        ([{"_id": 1, "x": 0.5}], [{"_id": 1, "adaptive_y": 1.0}]),
        ([{"_id": 1, "x": 0.5}], [{"_id": 1, "x": 1.0}]),
    ],
)
def test_record_run_rejects_reserved_or_shared_field_names(suggestions, outcomes):
    callback, documents = _collector()
    record = RecordRun(MagicMock(spec=Optimizer), callbacks=[callback], metadata={"plan_name": "test"})

    with pytest.raises(ValueError):
        record.record(0, "acq", suggestions, outcomes)

    assert [name for name, _ in documents] == ["start"]


@pytest.mark.parametrize(
    ("second_point", "error"),
    [({"_id": 3}, EventModelValidationError), ({"_id": 3, "x": 0.7, "z": 0.8}, KeyError)],
    ids=["missing-field", "extra-field"],
)
def test_record_run_step_with_mismatched_fields_emits_nothing_and_is_not_counted(second_point, error):
    callback, documents = _collector()
    record = RecordRun(MagicMock(spec=Optimizer), callbacks=[callback], metadata={"plan_name": "test"})
    record.record(0, "acq-0", [{"_id": 1, "x": 0.5}], [{"_id": 1, "y": 1.0}])

    with pytest.raises(error):
        record.record(1, "acq-1", [{"_id": 2, "x": 0.6}, second_point], [{"_id": 2, "y": 2.0}, {"_id": 3, "y": 3.0}])
    record.stop("fail")

    assert [name for name, _ in documents] == ["start", "descriptor", "event", "stop"]
    assert documents[-1][1]["num_events"] == {"primary": 1}


def test_record_run_describes_one_element_arrays_as_arrays():
    callback, documents = _collector()
    record = RecordRun(MagicMock(spec=Optimizer), callbacks=[callback], metadata={"plan_name": "test"})
    record.record(0, "acq", [{"_id": 1, "x": np.float64(0.5)}], [{"_id": 1, "y": np.array([1.0])}])

    descriptor = documents[1][1]
    assert (descriptor["data_keys"]["y"]["dtype"], descriptor["data_keys"]["y"]["shape"]) == ("array", [1])
    assert (descriptor["data_keys"]["x"]["dtype"], descriptor["data_keys"]["x"]["shape"]) == ("number", [])
    assert documents[2][1]["data"]["y"] == [1.0]
