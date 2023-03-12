# isort: skip_file

import pytest
from bluesky.tests.conftest import RE  # noqa
from bluesky_kafka.tests.conftest import broker_authorization_config  # noqa
from bluesky_kafka.tests.conftest import kafka_bootstrap_servers  # noqa
from bluesky_kafka.tests.conftest import publisher_factory  # noqa
from bluesky_kafka.tests.conftest import pytest_addoption  # noqa
from bluesky_kafka.tests.conftest import temporary_topics  # noqa
from bluesky_kafka.tests.conftest import consume_documents_from_kafka_until_first_stop_document  # noqa

from ophyd.tests.conftest import hw  # noqa

from tiled.client import from_profile

# @pytest.fixture(autouse=True, scope="session")
# def spin_docker(docker_ip, docker_services):  # noqa
#     return docker_ip


# @pytest.fixture(scope="session")
# def docker_compose_file(pytestconfig):
#     return os.path.join(str(pytestconfig.rootdir), "bluesky_adaptive", "tests", "docker-compose.yml")


@pytest.fixture(scope="function")
def tiled_profile():
    return "testing_sandbox"


@pytest.fixture(scope="module")
def tiled_node():
    return from_profile("testing_sandbox")
