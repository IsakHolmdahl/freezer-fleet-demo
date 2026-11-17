"""HTTP API, dashboard and background work of the store's cloud service."""
import logging
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from storecloud.config import Settings
from storecloud.db import Database, Freezer, Rollout, RolloutFreezer
from storecloud.maintenance import Approver
from storecloud.rollout import RolloutEngine
from storecloud.storeapi import Software, StoreApi, StoreApiError, http_client
from storecloud.sync import sync_freezers

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
Status = Literal["online", "offline"]


class FreezerOut(BaseModel):
    id: str
    name: str
    status: Status
    last_seen: datetime
    software_version: str
    temperature_c: float
    setpoint_c: float
    door_open: bool
    defrosting: bool
    alarm: bool


class ReadingOut(BaseModel):
    ts: datetime
    temperature_c: float
    setpoint_c: float
    door_open: bool
    defrosting: bool


class Summary(BaseModel):
    store: str | None                 # name of the store, once the store API has answered
    freezers: int
    online: int
    offline: int
    alarms: int
    software: dict[str, int]          # software version -> number of freezers
    last_sync: datetime | None
    sync_error: str | None            # why the last read of the store API failed, if it did


class DecisionOut(BaseModel):
    request_id: str
    freezer_id: str
    action: str
    params: dict[str, Any]
    technician: str | None
    decision: Literal["approve", "reject"]
    reason: str | None
    decided_at: datetime


class RolloutFreezerOut(BaseModel):
    freezer_id: str
    canary: bool
    state: str
    attempts: int
    error: str | None
    installed_at: datetime | None


class RolloutOut(BaseModel):
    id: int
    version: str
    state: str
    canary_count: int
    max_attempts: int
    max_failed: int
    created_at: datetime
    finished_at: datetime | None
    freezers: list[RolloutFreezerOut]


class StartRolloutBody(BaseModel):
    version: str
    freezer_ids: list[str] | None = None
    canary_count: int = 2
    max_attempts: int = 3
    max_failed: int = 1


def _utc(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _rf_out(rf: RolloutFreezer) -> RolloutFreezerOut:
    return RolloutFreezerOut(
        freezer_id=rf.freezer_id,
        canary=rf.canary,
        state=rf.state,
        attempts=rf.attempts,
        error=rf.error,
        installed_at=_utc(rf.installed_at) if rf.installed_at else None,
    )


def _rollout_out(rollout: Rollout) -> RolloutOut:
    return RolloutOut(
        id=rollout.id,
        version=rollout.version,
        state=rollout.state,
        canary_count=rollout.canary_count,
        max_attempts=rollout.max_attempts,
        max_failed=rollout.max_failed,
        created_at=_utc(rollout.created_at),
        finished_at=_utc(rollout.finished_at) if rollout.finished_at else None,
        freezers=[_rf_out(rf) for rf in rollout.freezers],
    )


class Worker:
    """Background work, one pass every poll interval:
    read the freezers, decide maintenance requests, process rollouts."""

    def __init__(self, api: StoreApi, db: Database, clock: Callable[[], float], interval: float):
        self.api, self.db, self.clock, self.interval = api, db, clock, interval
        self.approver = Approver(api, db, clock)
        self.rollout_engine = RolloutEngine(api, db, clock)
        self.store_name: str | None = None
        self.last_sync: float | None = None
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> None:
        try:
            if self.store_name is None:
                self.store_name = self.api.store().name
            sync_freezers(self.api, self.db)
            self.approver.run_once()
            # Process rollouts - re-fetch each rollout to get fresh state
            for rollout in self.rollout_engine.list_rollouts(10):
                if rollout.state == "running":
                    # Re-fetch to ensure we have the latest state (pause/cancel could have happened)
                    fresh_rollout = self.rollout_engine.get_rollout(rollout.id)
                    if fresh_rollout and fresh_rollout.state == "running":
                        self.rollout_engine.process_rollout(fresh_rollout.id)
                        self.rollout_engine.sync_freezer_states(fresh_rollout.id)
                elif rollout.state == "paused":
                    # Still sync freezer states for paused rollouts to capture in-progress installs
                    fresh_rollout = self.rollout_engine.get_rollout(rollout.id)
                    if fresh_rollout and fresh_rollout.state == "paused":
                        self.rollout_engine.sync_freezer_states(fresh_rollout.id)
            self.last_sync, self.error = self.clock(), None
        except Exception as error:  # noqa: BLE001 - the loop must survive any failed pass
            self.error = str(error)
            log.warning("pass failed: %s", error)

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                self.run_once()
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=loop, name="worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)


def create_app(settings: Settings | None = None, store_api: StoreApi | None = None,
               clock: Callable[[], float] = time.time, background: bool = True) -> FastAPI:
    """`store_api` replaces the client built from the settings (tests). With `background=False`
    nothing runs by itself; call `app.state.worker.run_once()`."""
    settings = settings or Settings()
    api = store_api or StoreApi(http_client(settings.store_api_url, settings.store_api_token))
    db = Database(settings.database_url)
    worker = Worker(api, db, clock, settings.poll_interval_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if background:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="Store cloud", version="0.1.0", lifespan=lifespan)
    app.state.db = db
    app.state.worker = worker

    def freezer_out(freezer: Freezer) -> FreezerOut:
        seen_recently = clock() - freezer.last_seen <= settings.offline_after_seconds
        return FreezerOut(
            id=freezer.id, name=freezer.name, status="online" if freezer.online and seen_recently else "offline",
            last_seen=_utc(freezer.last_seen), software_version=freezer.software_version,
            temperature_c=freezer.temperature_c, setpoint_c=freezer.setpoint_c, door_open=freezer.door_open,
            defrosting=freezer.defrosting,
            alarm=freezer.temperature_c > (
                settings.defrost_alarm_above_c if freezer.defrosting else settings.alarm_above_c),
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/freezers")
    def list_freezers(status: Status | None = None) -> list[FreezerOut]:
        freezers = [freezer_out(f) for f in db.freezers()]
        return [f for f in freezers if status is None or f.status == status]

    @app.get("/freezers/{freezer_id}")
    def get_freezer(freezer_id: str) -> FreezerOut:
        freezer = db.freezer(freezer_id)
        if freezer is None:
            raise HTTPException(404, "unknown freezer")
        return freezer_out(freezer)

    @app.get("/freezers/{freezer_id}/readings")
    def get_readings(freezer_id: str, limit: int = Query(50, ge=1, le=1000)) -> list[ReadingOut]:
        """Newest first."""
        if db.freezer(freezer_id) is None:
            raise HTTPException(404, "unknown freezer")
        return [ReadingOut(ts=_utc(r.ts), temperature_c=r.temperature_c, setpoint_c=r.setpoint_c,
                           door_open=r.door_open, defrosting=r.defrosting)
                for r in db.readings(freezer_id, limit)]

    @app.get("/summary")
    def summary() -> Summary:
        freezers = [freezer_out(f) for f in db.freezers()]
        online = sum(f.status == "online" for f in freezers)
        software: dict[str, int] = {}
        for freezer in freezers:
            software[freezer.software_version] = software.get(freezer.software_version, 0) + 1
        return Summary(
            store=worker.store_name, freezers=len(freezers), online=online, offline=len(freezers) - online,
            alarms=sum(f.alarm for f in freezers), software=software,
            last_sync=_utc(worker.last_sync) if worker.last_sync else None, sync_error=worker.error,
        )

    @app.get("/software")
    def software() -> list[Software]:
        """The software that can be installed, as listed by the store API."""
        try:
            return api.software()
        except StoreApiError as error:
            raise HTTPException(502, str(error)) from error

    @app.get("/maintenance-requests")
    def maintenance_requests(limit: int = Query(50, ge=1, le=500)) -> list[DecisionOut]:
        """The maintenance requests this service decided, newest first."""
        return [DecisionOut(request_id=d.request_id, freezer_id=d.freezer_id, action=d.action, params=d.params,
                            technician=d.technician, decision=d.decision, reason=d.reason,
                            decided_at=_utc(d.decided_at))
                for d in db.decisions(limit)]

    @app.post("/rollouts", status_code=201)
    def create_rollout(body: StartRolloutBody) -> RolloutOut:
        # Check for existing running or paused rollout
        existing = worker.rollout_engine.list_rollouts(1)
        if existing and existing[0].state in ("running", "paused"):
            raise HTTPException(409, "a rollout is already running or paused")
        try:
            rollout = worker.rollout_engine.start_rollout(
                version=body.version,
                freezer_ids=body.freezer_ids,
                canary_count=body.canary_count,
                max_attempts=body.max_attempts,
                max_failed=body.max_failed,
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except StoreApiError as error:
            if error.code == "unknown_version":
                raise HTTPException(404, str(error)) from error
            if error.code == "unknown_freezer":
                raise HTTPException(422, str(error)) from error
            raise HTTPException(502, str(error)) from error
        return _rollout_out(rollout)

    @app.get("/rollouts")
    def list_rollouts(limit: int = Query(10, ge=1, le=100)) -> list[RolloutOut]:
        rollouts = worker.rollout_engine.list_rollouts(limit)
        return [_rollout_out(r) for r in rollouts]

    @app.get("/rollouts/{rollout_id}")
    def get_rollout(rollout_id: int) -> RolloutOut:
        rollout = worker.rollout_engine.get_rollout(rollout_id)
        if rollout is None:
            raise HTTPException(404, "unknown rollout")
        return _rollout_out(rollout)

    @app.post("/rollouts/{rollout_id}/pause")
    def pause_rollout(rollout_id: int) -> RolloutOut:
        rollout = worker.rollout_engine.get_rollout(rollout_id)
        if rollout is None:
            raise HTTPException(404, "unknown rollout")
        if rollout.state != "running":
            raise HTTPException(409, "rollout not running")
        rollout = worker.rollout_engine.pause_rollout(rollout_id)
        return _rollout_out(rollout)

    @app.post("/rollouts/{rollout_id}/resume")
    def resume_rollout(rollout_id: int) -> RolloutOut:
        rollout = worker.rollout_engine.get_rollout(rollout_id)
        if rollout is None:
            raise HTTPException(404, "unknown rollout")
        if rollout.state != "paused":
            raise HTTPException(409, "rollout not paused")
        rollout = worker.rollout_engine.resume_rollout(rollout_id)
        return _rollout_out(rollout)

    @app.post("/rollouts/{rollout_id}/cancel")
    def cancel_rollout(rollout_id: int) -> RolloutOut:
        rollout = worker.rollout_engine.get_rollout(rollout_id)
        if rollout is None:
            raise HTTPException(404, "unknown rollout")
        if rollout.state not in ("running", "paused"):
            raise HTTPException(409, "rollout not running or paused")
        rollout = worker.rollout_engine.cancel_rollout(rollout_id)
        return _rollout_out(rollout)

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
