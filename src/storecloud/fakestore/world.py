"""A stand-in for the store behind the store API: ten freezers that behave like the real ones.

Used for local development and tests; the real store is hosted elsewhere. State moves
with the clock: every call first brings the store up to the present (`_advance`).
"""
import math
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from storecloud.storeapi import EnergyHour, FreezerState, Install, MaintenanceRequest, PassKey, Software, StoreReading

SOFTWARE = [
    Software(version="1.4.2", released="2026-03-02", notes="Current software."),
    Software(version="2.0.0", released="2026-11-10", notes="Adds remote maintenance by service technicians."),
]
ISSUER = "service-desk"
SETPOINT_RANGE = (-30.0, -12.0)
DEFROST_RANGE = (5, 60)


class Refused(Exception):
    """The store API answers with this status and code."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Timings:
    """Seconds. The defaults suit a person watching; tests use much shorter ones."""

    download: float = 8.0
    install: float = 5.0
    offline_return: float = 40.0        # frys-07 comes online this long after the first install command
    defrost_on_first_install: float = 20.0   # frys-05 defrosts this long from the first install command
    door_period: float = 45.0           # frys-09: the door is open `door_open` out of every `door_period`
    door_open: float = 12.0
    reading_interval: float = 5.0
    request_ttl: float = 90.0
    technician_interval: float = 0.0    # > 0: a technician sends a request this often (freezers on 2.x)


@dataclass
class _Freezer:
    id: str
    name: str
    software_version: str = "1.4.2"
    setpoint_c: float = -18.0
    last_seen: float = 0.0
    defrost_until: float = 0.0
    install: Install | None = None
    install_fails: str | None = None    # error the running install will end with
    readings: deque = field(default_factory=lambda: deque(maxlen=720))
    next_reading: float = 0.0


def _utc(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


class FakeStore:
    def __init__(self, clock: Callable[[], float] = time.time, timings: Timings | None = None,
                 store_id: str = "ica-teststad", token: str = "dev-token", seed: int = 1):
        self.clock, self.timings, self.id, self.token = clock, timings or Timings(), store_id, token
        self.lock = threading.RLock()
        self.started = clock()
        self.freezers = {f"frys-{n:02d}": _Freezer(f"frys-{n:02d}", f"Frys {n}", last_seen=self.started,
                                                   next_reading=self.started)
                         for n in range(1, 11)}
        self.installs: list[Install] = []
        self.requests: list[MaintenanceRequest] = []
        self.first_install_at: float | None = None
        self._failed_once: set[tuple[str, str]] = set()
        self._random = random.Random(seed)
        self._next_technician = self.started + self.timings.technician_interval
        self._key = Ed25519PrivateKey.generate()
        # Test hooks
        self.always_fail: set[str] = set()      # freezers whose installs always fail ("flash_error")
        self.force_offline: set[str] = set()    # freezers kept offline

    # -- state ------------------------------------------------------------------------

    def _online(self, freezer: _Freezer, now: float) -> bool:
        if freezer.id in self.force_offline:
            return False
        if freezer.id == "frys-07":
            return self.first_install_at is not None and now >= self.first_install_at + self.timings.offline_return
        return True

    def _door_open(self, freezer: _Freezer, now: float) -> bool:
        return freezer.id == "frys-09" and (now - self.started) % self.timings.door_period < self.timings.door_open

    def _temperature(self, freezer: _Freezer, at: float) -> float:
        wobble = 0.4 * math.sin(at / 37 + int(freezer.id[-2:]))
        extra = (1.5 if self._door_open(freezer, at) else 0) + (8 if at < freezer.defrost_until else 0)
        return round(freezer.setpoint_c + wobble + extra, 1)

    def _advance(self) -> float:
        now = self.clock()
        for freezer in self.freezers.values():
            self._advance_install(freezer, now)
            if not self._online(freezer, now):
                freezer.next_reading = now  # no readings while offline
                continue
            freezer.last_seen = now
            if now - freezer.next_reading > freezer.readings.maxlen * self.timings.reading_interval:
                freezer.next_reading = now
            while freezer.next_reading <= now:
                at = freezer.next_reading
                freezer.readings.append(StoreReading(
                    ts=_utc(at), temperature_c=self._temperature(freezer, at), setpoint_c=freezer.setpoint_c,
                    door_open=self._door_open(freezer, at), defrosting=at < freezer.defrost_until))
                freezer.next_reading += self.timings.reading_interval
        for request in self.requests:
            if request.status == "pending" and now >= request.expires_at.timestamp():
                request.status = "expired"
        if self.timings.technician_interval > 0:
            while self._next_technician <= now:
                self._next_technician += self.timings.technician_interval
                ready = [f for f in self.freezers.values()
                         if f.software_version >= "2" and self._online(f, now)]
                if ready:
                    freezer = self._random.choice(ready)
                    if self._random.random() < 0.6:
                        self.enqueue_request(freezer.id, "set_setpoint",
                                             {"setpoint_c": self._random.randrange(-48, -32) / 2})
                    else:
                        self.enqueue_request(freezer.id, "defrost",
                                             {"duration_minutes": self._random.randrange(15, 46)})
        return now

    def _advance_install(self, freezer: _Freezer, now: float) -> None:
        install = freezer.install
        if install is None or install.state in ("installed", "failed"):
            return
        elapsed = now - install.started_at.timestamp()
        timings = self.timings
        if freezer.install_fails and elapsed >= 0.6 * timings.download:
            install.state, install.error, install.progress_pct = "failed", freezer.install_fails, 60
            install.finished_at = _utc(install.started_at.timestamp() + 0.6 * timings.download)
        elif elapsed >= timings.download + timings.install:
            install.state, install.progress_pct = "installed", 100
            install.finished_at = _utc(install.started_at.timestamp() + timings.download + timings.install)
            freezer.software_version = install.version
        elif elapsed >= timings.download:
            install.state, install.progress_pct = "installing", 100
        else:
            install.progress_pct = int(100 * elapsed / timings.download)

    def _freezer(self, freezer_id: str) -> _Freezer:
        if freezer_id not in self.freezers:
            raise Refused(404, "unknown_freezer", f"no freezer {freezer_id}")
        return self.freezers[freezer_id]

    def _state(self, freezer: _Freezer, now: float) -> FreezerState:
        last = freezer.readings[-1] if freezer.readings else None
        return FreezerState(
            id=freezer.id, name=freezer.name, online=self._online(freezer, now), last_seen=_utc(freezer.last_seen),
            software_version=freezer.software_version,
            temperature_c=last.temperature_c if last else freezer.setpoint_c, setpoint_c=freezer.setpoint_c,
            door_open=self._door_open(freezer, now), defrosting=now < freezer.defrost_until,
            install=freezer.install.model_copy() if freezer.install else None,
        )

    # -- what the store API offers ----------------------------------------------------

    def freezer_states(self) -> list[FreezerState]:
        with self.lock:
            now = self._advance()
            return [self._state(f, now) for f in self.freezers.values()]

    def freezer_state(self, freezer_id: str) -> FreezerState:
        with self.lock:
            now = self._advance()
            return self._state(self._freezer(freezer_id), now)

    def readings(self, freezer_id: str, since: datetime | None, limit: int) -> list[StoreReading]:
        with self.lock:
            self._advance()
            found = [r for r in self._freezer(freezer_id).readings if since is None or r.ts > since]
            return found[-limit:]

    def start_install(self, freezer_id: str, version: str) -> Install:
        with self.lock:
            now = self._advance()
            freezer = self._freezer(freezer_id)
            if version not in {s.version for s in SOFTWARE}:
                raise Refused(404, "unknown_version", f"no software {version}")
            if self.first_install_at is None:
                self.first_install_at = now
                self.freezers["frys-05"].defrost_until = now + self.timings.defrost_on_first_install
            if not self._online(freezer, now):
                raise Refused(409, "offline", f"{freezer_id} cannot be reached")
            if freezer.install and freezer.install.state in ("downloading", "installing"):
                raise Refused(409, "install_in_progress", f"{freezer_id} is already installing")
            if freezer.software_version == version:
                raise Refused(409, "already_installed", f"{freezer_id} already runs {version}")
            if self._door_open(freezer, now) or now < freezer.defrost_until:
                raise Refused(409, "busy", f"{freezer_id} has its door open or is defrosting")
            freezer.install_fails = None
            if freezer_id in self.always_fail:
                freezer.install_fails = "flash_error"
            elif freezer_id == "frys-03" and (freezer_id, version) not in self._failed_once:
                self._failed_once.add((freezer_id, version))
                freezer.install_fails = "checksum_mismatch"
            freezer.install = Install(id=f"inst-{len(self.installs) + 1:04d}", freezer_id=freezer_id,
                                      version=version, state="downloading", progress_pct=0, started_at=_utc(now))
            self.installs.append(freezer.install)
            return freezer.install.model_copy()

    def all_installs(self) -> list[Install]:
        with self.lock:
            self._advance()
            return [i.model_copy() for i in self.installs]

    def maintenance_requests(self, status: str | None = None) -> list[MaintenanceRequest]:
        with self.lock:
            self._advance()
            return [r.model_copy() for r in self.requests if status is None or r.status == status][-200:]

    def decide(self, request_id: str, decision: str, reason: str | None) -> MaintenanceRequest:
        with self.lock:
            now = self._advance()
            request = next((r for r in self.requests if r.id == request_id), None)
            if request is None:
                raise Refused(404, "unknown_request", f"no maintenance request {request_id}")
            if request.status != "pending":
                raise Refused(409, "already_decided", f"{request_id} is {request.status}")
            request.status = "approved" if decision == "approve" else "rejected"
            request.decided_at, request.reason = _utc(now), reason
            if request.status == "approved":   # the freezer carries it out at once
                freezer = self.freezers[request.freezer_id]
                if request.action == "set_setpoint":
                    freezer.setpoint_c = float(request.params["setpoint_c"])
                elif request.action == "defrost":
                    freezer.defrost_until = now + 60 * float(request.params["duration_minutes"])
            return request.model_copy()

    def pass_key(self) -> PassKey:
        pem = self._key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)
        return PassKey(algorithm="EdDSA", issuer=ISSUER, audience=self.id, public_key_pem=pem.decode())

    def energy(self, hours: int) -> list[EnergyHour]:
        with self.lock:
            now = self._advance()
        first = int(now // 3600) - hours + 1
        rows = []
        for hour in range(first, first + hours):
            for freezer in self.freezers.values():
                values = random.Random(f"{freezer.id}-{hour}")   # the same hour always gives the same values
                day_time = 8 <= _utc(hour * 3600).hour < 21
                rows.append(EnergyHour(
                    freezer_id=freezer.id, hour=_utc(hour * 3600),
                    energy_kwh=round(values.uniform(1.1, 1.5) + (0.3 if day_time else 0), 2),
                    door_openings=values.randrange(5, 30) if day_time else values.randrange(0, 3),
                    avg_temperature_c=round(freezer.setpoint_c + values.uniform(-0.4, 0.6), 1)))
        return rows

    # -- helpers for tests and local development --------------------------------------

    def mint_pass(self, technician: str = "tech-0417", ttl_seconds: float = 900, **claims: Any) -> str:
        """A technician pass signed by this store's service desk. `claims` override the standard ones."""
        now = self.clock()
        payload = {"iss": ISSUER, "aud": self.id, "sub": technician, "iat": int(now),
                   "exp": int(now + ttl_seconds), "jti": f"{self._random.getrandbits(64):016x}"} | claims
        return jwt.encode(payload, self._key, algorithm="EdDSA")

    def enqueue_request(self, freezer_id: str, action: str, params: dict[str, Any],
                        technician_pass: str | None = None) -> MaintenanceRequest:
        """A technician asks for maintenance on a freezer. Without `technician_pass` a good one is made.
        (The real store only does this for freezers on software 2.0.0 or newer.)"""
        with self.lock:
            now = self.clock()
            request = MaintenanceRequest(
                id=f"mr-{len(self.requests) + 1:04d}", freezer_id=self._freezer(freezer_id).id, action=action,
                params=params, technician_pass=technician_pass or self.mint_pass(), status="pending",
                created_at=_utc(now), expires_at=_utc(now + self.timings.request_ttl))
            self.requests.append(request)
            return request.model_copy()
