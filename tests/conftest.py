"""Fixtures: the service wired to the fake store in-process, on a clock the test moves."""
import time

import pytest
from fastapi.testclient import TestClient

from storecloud.app import create_app
from storecloud.config import Settings
from storecloud.fakestore.app import in_process_api
from storecloud.fakestore.world import FakeStore
from storecloud.storeapi import StoreApi


class Clock:
    """Starts ten minutes behind the real time. Technician passes are checked against the real
    time, so a pass made on this clock is valid as long as a test advances at most ten minutes."""

    def __init__(self):
        self.now = float(int(time.time()) - 600)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock) -> FakeStore:
    return FakeStore(clock=clock)


@pytest.fixture
def api(store) -> StoreApi:
    return in_process_api(store)


@pytest.fixture
def app(api, clock):
    settings = Settings(_env_file=None, database_url="sqlite://")
    return create_app(settings, store_api=api, clock=clock, background=False)


@pytest.fixture
def worker(app):
    return app.state.worker


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
