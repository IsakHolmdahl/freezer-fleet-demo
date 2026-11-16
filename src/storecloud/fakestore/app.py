"""The store API (docs/store-api.md) served from a `FakeStore`."""
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from storecloud.fakestore.world import SOFTWARE, FakeStore, Refused
from storecloud.storeapi import (
    EnergyHour,
    FreezerState,
    Install,
    PassKey,
    Software,
    StoreApi,
    StoreInfo,
    StoreReading,
)


class InstallBody(BaseModel):
    version: str


class DecisionBody(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = None


def create_fake_app(store: FakeStore) -> FastAPI:
    app = FastAPI(title="Fake store API")
    app.state.store = store

    @app.exception_handler(Refused)
    async def refused(request: Request, error: Refused) -> JSONResponse:
        return JSONResponse({"detail": {"code": error.code, "message": error.message}}, status_code=error.status)

    def authorised(store_id: str, request: Request) -> None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or token != store.token:
            raise Refused(401, "unauthorised", "missing or wrong token")
        if store_id != store.id:
            raise Refused(403, "forbidden", "the token belongs to another store")

    api = APIRouter(prefix="/api/v1/stores/{store_id}", dependencies=[Depends(authorised)])

    @api.get("")
    @api.get("/", include_in_schema=False)
    def get_store() -> StoreInfo:
        return StoreInfo(id=store.id, name="Ica Teststad", city="Teststad", team="Local development",
                         freezer_count=len(store.freezers), current_software="1.4.2")

    @api.get("/freezers")
    def freezers() -> list[FreezerState]:
        return store.freezer_states()

    @api.get("/freezers/{freezer_id}")
    def freezer(freezer_id: str) -> FreezerState:
        return store.freezer_state(freezer_id)

    @api.get("/freezers/{freezer_id}/readings")
    def readings(freezer_id: str, since: datetime | None = None,
                 limit: int = Query(200, ge=1, le=1000)) -> list[StoreReading]:
        return store.readings(freezer_id, since, limit)

    @api.get("/software")
    def software() -> list[Software]:
        return SOFTWARE

    @api.post("/freezers/{freezer_id}/install", status_code=202)
    def install(freezer_id: str, body: InstallBody) -> Install:
        return store.start_install(freezer_id, body.version)

    @api.get("/installs")
    def installs() -> list[Install]:
        return store.all_installs()

    @api.get("/maintenance-requests")
    def maintenance_requests(
        status: Literal["pending", "approved", "rejected", "expired"] | None = None,
    ) -> list[dict[str, Any]]:
        return [r.model_dump(by_alias=True, mode="json") for r in store.maintenance_requests(status)]

    @api.post("/maintenance-requests/{request_id}/decision")
    def decide(request_id: str, body: DecisionBody) -> dict[str, Any]:
        return store.decide(request_id, body.decision, body.reason).model_dump(by_alias=True, mode="json")

    @api.get("/technician-pass-key")
    def technician_pass_key() -> PassKey:
        return store.pass_key()

    @api.get("/energy")
    def energy(hours: int = Query(24, ge=1, le=168)) -> list[EnergyHour]:
        return store.energy(hours)

    app.include_router(api)
    return app


def in_process_api(store: FakeStore, token: str | None = None, store_id: str | None = None) -> StoreApi:
    """A store API client that talks to the fake store without a network (tests)."""
    client = TestClient(create_fake_app(store), base_url=f"http://store/api/v1/stores/{store_id or store.id}",
                        headers={"Authorization": f"Bearer {token or store.token}"})
    return StoreApi(client)
