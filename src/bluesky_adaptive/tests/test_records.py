import numpy as np
import pytest
from bluesky.utils import RequestAbort, RequestStop

from bluesky_adaptive._records import ACQUISITION_UID_FIELD, ITERATION_FIELD, SUGGESTION_ID_FIELD, RunRecorder


def _collector():
    documents: list[tuple[str, dict]] = []

    def callback(name, doc):
        documents.append((name, dict(doc)))

    return callback, documents


def _first(documents, name):
    return next(doc for doc_name, doc in documents if doc_name == name)


def test_run_recorder_records_one_event_per_suggestion():
    callback, documents = _collector()
    recorder = RunRecorder(callbacks=[callback], metadata={"plan_name": "test"})

    recorder.record(
        0,
        "acquisition-uid",
        [{"_id": 1, "x": 0.5}, {"_id": 2, "x": 0.7}],
        [{"_id": 2, "y": 2.0}, {"_id": 1, "y": 1.0}],
    )
    recorder.stop()

    assert [name for name, _ in documents] == ["start", "descriptor", "event", "event", "stop"]
    assert _first(documents, "start")["plan_name"] == "test"
    descriptor = _first(documents, "descriptor")
    assert descriptor["name"] == "primary"
    assert descriptor["object_keys"] == {"parameters": ["x"], "outcomes": ["y"]}
    assert descriptor["data_keys"][SUGGESTION_ID_FIELD]["dtype"] == "string"
    assert [doc["data"] for name, doc in documents if name == "event"] == [
        {SUGGESTION_ID_FIELD: "1", ITERATION_FIELD: 0, ACQUISITION_UID_FIELD: "acquisition-uid", "x": 0.5, "y": 1.0},
        {SUGGESTION_ID_FIELD: "2", ITERATION_FIELD: 0, ACQUISITION_UID_FIELD: "acquisition-uid", "x": 0.7, "y": 2.0},
    ]
    stop = _first(documents, "stop")
    assert (stop["exit_status"], stop["num_events"]) == ("success", {"primary": 2})


def test_run_recorder_empty_step_sends_nothing():
    callback, documents = _collector()
    RunRecorder(callbacks=[callback], metadata={}).record(0, "acquisition-uid", [], [])

    assert [name for name, _ in documents] == ["start"]


@pytest.mark.parametrize(
    ("acquisition_uid", "recorded"),
    [("run-uid", "run-uid"), (("near", "far"), "('near', 'far')"), (7, "7"), (None, "None")],
    ids=["string", "tuple", "int", "none"],
)
def test_run_recorder_records_acquisition_uid_as_string(acquisition_uid, recorded):
    callback, documents = _collector()
    RunRecorder(callbacks=[callback], metadata={}).record(0, acquisition_uid, [{"_id": 1, "x": 0.5}], [{"_id": 1, "y": 1.0}])

    assert _first(documents, "event")["data"][ACQUISITION_UID_FIELD] == recorded


@pytest.mark.parametrize(
    ("value", "recorded", "dtype", "shape"),
    [
        (np.array([1.5]), 1.5, "number", []),
        ([2.5], 2.5, "number", []),
        (np.float64(0.5), 0.5, "number", []),
        (np.array([1.0, 2.0]), [1.0, 2.0], "array", [2]),
    ],
    ids=["one-element-array", "one-element-list", "numpy-scalar", "array"],
)
def test_run_recorder_records_plain_values(value, recorded, dtype, shape):
    callback, documents = _collector()
    RunRecorder(callbacks=[callback], metadata={}).record(
        0, "acquisition-uid", [{"_id": 1, "x": 0.5}], [{"_id": 1, "y": value}]
    )

    data_key = _first(documents, "descriptor")["data_keys"]["y"]
    assert _first(documents, "event")["data"]["y"] == recorded
    assert (data_key["dtype"], data_key["shape"]) == (dtype, shape)


@pytest.mark.parametrize(
    ("suggestions", "outcomes"),
    [
        ([{"_id": 1, "adaptive_x": 0.5}], [{"_id": 1, "y": 1.0}]),
        ([{"_id": 1, "x": 0.5}], [{"_id": 1, "adaptive_y": 1.0}]),
        ([{"_id": 1, "x": 0.5}], [{"_id": 1, "x": 1.0}]),
    ],
    ids=["reserved-parameter", "reserved-outcome", "shared-name"],
)
def test_run_recorder_rejects_reserved_or_shared_field_names(suggestions, outcomes):
    callback, documents = _collector()
    recorder = RunRecorder(callbacks=[callback], metadata={})

    with pytest.raises(ValueError):
        recorder.record(0, "acquisition-uid", suggestions, outcomes)

    assert [name for name, _ in documents] == ["start"]


@pytest.mark.parametrize("recorded_before", [False, True], ids=["first-step", "later-step"])
def test_run_recorder_step_with_different_fields_sends_nothing(recorded_before):
    callback, documents = _collector()
    recorder = RunRecorder(callbacks=[callback], metadata={})
    if recorded_before:
        recorder.record(0, "acquisition-0", [{"_id": 1, "x": 0.5}], [{"_id": 1, "y": 1.0}])
    sent = len(documents)

    with pytest.raises(ValueError, match="must have the fields"):
        recorder.record(
            1,
            "acquisition-1",
            [{"_id": 2, "x": 0.6}, {"_id": 3, "x": 0.7, "z": 0.8}],
            [{"_id": 2, "y": 2.0}, {"_id": 3, "y": 3.0}],
        )
    recorder.stop("fail")

    assert len(documents) == sent + 1
    assert _first(documents, "stop")["num_events"] == ({"primary": 1} if recorded_before else {})


def test_run_recorder_failing_start_callback_stops_the_run_for_earlier_callbacks():
    first, first_documents = _collector()
    later, later_documents = _collector()

    def failing(name, doc):
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        RunRecorder(callbacks=[first, failing, later], metadata={})

    assert [name for name, _ in first_documents] == ["start", "stop"]
    assert _first(first_documents, "stop")["exit_status"] == "fail"
    assert later_documents == []


def test_run_recorder_sends_one_stop():
    callback, documents = _collector()
    recorder = RunRecorder(callbacks=[callback], metadata={})

    recorder.stop("abort", "first")
    recorder.stop()

    assert [name for name, _ in documents] == ["start", "stop"]
    assert _first(documents, "stop")["reason"] == "first"


@pytest.mark.parametrize(
    ("error", "exit_status", "reason"),
    [
        (RequestStop(), "success", "Stop requested"),
        (RequestAbort(), "abort", "Aborted: RequestAbort"),
        (GeneratorExit(), "abort", "Aborted: GeneratorExit"),
        (
            RuntimeError("detector offline"),
            "fail",
            "Iteration 3 with suggestion IDs [1, 2] failed: RuntimeError: detector offline",
        ),
    ],
    ids=["stop", "abort", "halt", "failure"],
)
def test_run_recorder_stop_for_maps_the_exception_to_the_exit_status(error, exit_status, reason):
    callback, documents = _collector()

    RunRecorder(callbacks=[callback], metadata={}).stop_for(error, 3, [{"_id": 1}, {"_id": 2}])

    stop = _first(documents, "stop")
    assert (stop["exit_status"], stop["reason"]) == (exit_status, reason)
