# isort: skip_file

import pytest
from bluesky.tests.conftest import RE  # noqa
from bluesky_kafka.tests.conftest import kafka_bootstrap_servers  # noqa
from bluesky_kafka.tests.conftest import publisher_factory  # noqa
from bluesky_kafka.tests.conftest import pytest_addoption  # noqa
from bluesky_kafka.tests.conftest import temporary_topics  # noqa
from bluesky_kafka.tests.conftest import consume_documents_from_kafka_until_first_stop_document  # noqa

from ophyd.tests.conftest import hw  # noqa
from databroker import temp
from tiled.client import from_profile


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
