import pytest
from oxbrook.testing import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    # One real server for the whole run: starting one per test is slow, so
    # tests create what they need rather than assume an empty store.
    with TestClient(app) as client:
        yield client
