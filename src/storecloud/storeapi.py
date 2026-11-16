"""Client and data shapes of the store API (docs/store-api.md)."""
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class StoreInfo(BaseModel):
    id: str
    name: str
    city: str
    team: str
    freezer_count: int
    current_software: str


class Install(BaseModel):
    id: str
    freezer_id: str
    version: str
    state: Literal["downloading", "installing", "installed", "failed"]
    progress_pct: int
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class FreezerState(BaseModel):
    id: str
    name: str
    online: bool
    last_seen: datetime
    software_version: str
    temperature_c: float
    setpoint_c: float
    door_open: bool
    defrosting: bool
    install: Install | None = None


class StoreReading(BaseModel):
    ts: datetime
    temperature_c: float
    setpoint_c: float
    door_open: bool
    defrosting: bool


class Software(BaseModel):
    version: str
    released: str
    notes: str


class MaintenanceRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    freezer_id: str
    action: str
    params: dict[str, Any]
    technician_pass: str = Field(alias="pass")
    status: Literal["pending", "approved", "rejected", "expired"]
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    reason: str | None = None


class PassKey(BaseModel):
    algorithm: str
    issuer: str
    audience: str
    public_key_pem: str


class EnergyHour(BaseModel):
    freezer_id: str
    hour: datetime
    energy_kwh: float
    door_openings: int
    avg_temperature_c: float


class StoreApiError(Exception):
    """The store API answered with an error. `code` is its machine code (e.g. `offline`)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"store API {status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def http_client(url: str, token: str) -> httpx.Client:
    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=10,
                        follow_redirects=True)


class StoreApi:
    """One method per call of the store API. `client` has the store endpoint as base URL."""

    def __init__(self, client: httpx.Client):
        self.client = client

    def _call(self, method: str, path: str, model: Any, **kwargs) -> Any:
        response = self.client.request(method, path, **kwargs)
        if response.is_error:
            try:
                detail = response.json()["detail"]
                code, message = detail["code"], detail["message"]
            except (ValueError, KeyError, TypeError):
                code, message = "error", response.text[:200]
            raise StoreApiError(response.status_code, code, message)
        return TypeAdapter(model).validate_python(response.json())

    def store(self) -> StoreInfo:
        return self._call("GET", "/", StoreInfo)

    def freezers(self) -> list[FreezerState]:
        return self._call("GET", "/freezers", list[FreezerState])

    def freezer(self, freezer_id: str) -> FreezerState:
        return self._call("GET", f"/freezers/{freezer_id}", FreezerState)

    def readings(self, freezer_id: str, since: datetime | None = None, limit: int = 200) -> list[StoreReading]:
        params: dict[str, Any] = {"limit": limit}
        if since is not None:
            params["since"] = since.isoformat()
        return self._call("GET", f"/freezers/{freezer_id}/readings", list[StoreReading], params=params)

    def software(self) -> list[Software]:
        return self._call("GET", "/software", list[Software])

    def install(self, freezer_id: str, version: str) -> Install:
        return self._call("POST", f"/freezers/{freezer_id}/install", Install, json={"version": version})

    def installs(self) -> list[Install]:
        return self._call("GET", "/installs", list[Install])

    def maintenance_requests(self, status: str | None = None) -> list[MaintenanceRequest]:
        params = {"status": status} if status else {}
        return self._call("GET", "/maintenance-requests", list[MaintenanceRequest], params=params)

    def decide(self, request_id: str, decision: Literal["approve", "reject"],
               reason: str | None = None) -> MaintenanceRequest:
        body = {"decision": decision} | ({"reason": reason} if reason else {})
        return self._call("POST", f"/maintenance-requests/{request_id}/decision", MaintenanceRequest, json=body)

    def technician_pass_key(self) -> PassKey:
        return self._call("GET", "/technician-pass-key", PassKey)

    def energy(self, hours: int = 24) -> list[EnergyHour]:
        return self._call("GET", "/energy", list[EnergyHour], params={"hours": hours})
