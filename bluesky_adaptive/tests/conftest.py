# isort: skip_file

import pytest
import time

from fastapi.testclient import TestClient
from bluesky.tests.conftest import RE  # noqa
from bluesky_kafka.tests.conftest import kafka_bootstrap_servers  # noqa
from bluesky_kafka.tests.conftest import publisher_factory  # noqa
from bluesky_kafka.tests.conftest import pytest_addoption  # noqa
from bluesky_kafka.tests.conftest import temporary_topics  # noqa
from bluesky_kafka.tests.conftest import consume_documents_from_kafka_until_first_stop_document  # noqa

from ophyd.tests.conftest import hw  # noqa
from databroker import temp
from tiled.client import from_profile
from bluesky_adaptive.server import app


@pytest.fixture(scope="session")
def kafka_producer_config():
    return {
        "acks": 1,
        "enable.idempotence": False,
        "request.timeout.ms": 5000,
    }


@pytest.fixture(scope="function")
def tiled_profile():
    return "testing_sandbox"


@pytest.fixture(scope="module")
def tiled_node():
    return from_profile("testing_sandbox")


@pytest.fixture(scope="function")
def catalog():
    return temp().v2


@pytest.fixture(scope="module")
def fastapi_client():
    """
    Create a FastAPI test client for the bluesky_adaptive server.
    """
    with TestClient(app) as client:
        then = time.monotonic()
        while not client.get("/api/").status_code == 200:
            # Wait for the FastAPI server to start, and Worker to be initialized
            if time.monotonic() - then > 5:
                raise TimeoutError("The FastAPI server did not start in time.")
            time.sleep(0.1)  # Wait for the server to start
        yield client
