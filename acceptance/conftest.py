"""Acceptance tests: the service runs for real (background work included) against the fake
store, in one process, with no network and no Docker. Everything is observed through the
HTTP API of the service and through the fake store.

Timings are short so that an install takes about half a second.
"""
import time
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from storecloud.app import create_app
from storecloud.config import Settings
from storecloud.fakestore.app import in_process_api
from storecloud.fakestore.world import FakeStore, Timings

FAST = dict(download=0.3, install=0.2, offline_return=1.0, defrost_on_first_install=0.5,
            door_period=1.0, door_open=0.3, reading_interval=0.1, request_ttl=10.0)
ALL = [f"frys-{n:02d}" for n in range(1, 11)]


def wait_until(predicate: Callable[[], object], timeout: float = 30.0, message: str = "condition"):
    """Polls until `predicate()` is truthy and returns that value; fails after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout}s waiting for {message}")
        time.sleep(0.05)


class Service:
    """The running service. `client` is its HTTP API; `restart()` stops it and starts a new
    process-equivalent on the same database."""

    def __init__(self, store: FakeStore, database_url: str):
        self.store, self.database_url = store, database_url
        self.client: TestClient | None = None
        self.start()

    def start(self) -> None:
        settings = Settings(_env_file=None, database_url=self.database_url, poll_interval_seconds=0.05,
                            offline_after_seconds=5)
        self.client = TestClient(create_app(settings, store_api=in_process_api(self.store)))
        self.client.__enter__()   # runs the startup of the service

    def stop(self) -> None:
        if self.client is not None:
            self.client.__exit__(None, None, None)
            self.client = None

    def restart(self) -> None:
        self.stop()
        self.start()

    # shortcuts
    def get(self, path: str, **kwargs):
        return self.client.get(path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.client.post(path, **kwargs)

    def rollout(self, rollout_id: int) -> dict:
        response = self.get(f"/rollouts/{rollout_id}")
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore(timings=Timings(**FAST))


@pytest.fixture
def service(store, tmp_path):
    running = Service(store, f"sqlite:///{tmp_path / 'service.db'}")
    wait_until(lambda: len(running.get("/freezers").json()) == 10, message="the first read of the store")
    yield running
    running.stop()
