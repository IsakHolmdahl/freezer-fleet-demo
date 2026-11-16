"""Persistence (SQLAlchemy; SQLite by default). Timestamps are Unix seconds (UTC)."""
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, UniqueConstraint, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from storecloud.storeapi import FreezerState, StoreReading


class Base(DeclarativeBase):
    pass


class Freezer(Base):
    """Latest known state of a freezer, as last read from the store API."""

    __tablename__ = "freezers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    online: Mapped[bool] = mapped_column(Boolean)      # what the store API reported
    last_seen: Mapped[float] = mapped_column(Float)    # when the store last heard from the freezer
    software_version: Mapped[str] = mapped_column(String(32))
    temperature_c: Mapped[float] = mapped_column(Float)
    setpoint_c: Mapped[float] = mapped_column(Float)
    door_open: Mapped[bool] = mapped_column(Boolean)
    defrosting: Mapped[bool] = mapped_column(Boolean)


class Reading(Base):
    __tablename__ = "readings"
    __table_args__ = (UniqueConstraint("freezer_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    freezer_id: Mapped[str] = mapped_column(ForeignKey("freezers.id"), index=True)
    ts: Mapped[float] = mapped_column(Float)
    temperature_c: Mapped[float] = mapped_column(Float)
    setpoint_c: Mapped[float] = mapped_column(Float)
    door_open: Mapped[bool] = mapped_column(Boolean)
    defrosting: Mapped[bool] = mapped_column(Boolean)


class MaintenanceDecision(Base):
    """A maintenance request this service approved or rejected."""

    __tablename__ = "maintenance_decisions"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    freezer_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    technician: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str] = mapped_column(String(16))   # approve | reject
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decided_at: Mapped[float] = mapped_column(Float)


class Database:
    def __init__(self, url: str):
        options: dict[str, Any] = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        if url in ("sqlite://", "sqlite:///:memory:"):
            options["poolclass"] = StaticPool  # one shared in-memory database
        self.engine = create_engine(url, **options)
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def save_freezer(self, state: FreezerState) -> None:
        with self.session() as session, session.begin():
            freezer = session.get(Freezer, state.id)
            if freezer is None:
                freezer = Freezer(id=state.id)
                session.add(freezer)
            freezer.name = state.name
            freezer.online = state.online
            freezer.last_seen = state.last_seen.timestamp()
            freezer.software_version = state.software_version
            freezer.temperature_c = state.temperature_c
            freezer.setpoint_c = state.setpoint_c
            freezer.door_open = state.door_open
            freezer.defrosting = state.defrosting

    def add_readings(self, freezer_id: str, readings: list[StoreReading]) -> None:
        """Adds readings newer than the newest one stored for this freezer."""
        newest = self.newest_reading_ts(freezer_id)
        with self.session() as session, session.begin():
            for reading in readings:
                if newest is None or reading.ts.timestamp() > newest:
                    session.add(Reading(
                        freezer_id=freezer_id, ts=reading.ts.timestamp(), temperature_c=reading.temperature_c,
                        setpoint_c=reading.setpoint_c, door_open=reading.door_open, defrosting=reading.defrosting,
                    ))

    def newest_reading_ts(self, freezer_id: str) -> float | None:
        with self.session() as session:
            return session.scalar(select(func.max(Reading.ts)).where(Reading.freezer_id == freezer_id))

    def freezers(self) -> list[Freezer]:
        with self.session() as session:
            return list(session.scalars(select(Freezer).order_by(Freezer.id)))

    def freezer(self, freezer_id: str) -> Freezer | None:
        with self.session() as session:
            return session.get(Freezer, freezer_id)

    def readings(self, freezer_id: str, limit: int) -> list[Reading]:
        """Newest first."""
        with self.session() as session:
            query = (select(Reading).where(Reading.freezer_id == freezer_id)
                     .order_by(Reading.ts.desc()).limit(limit))
            return list(session.scalars(query))

    def save_decision(self, decision: MaintenanceDecision) -> None:
        with self.session() as session, session.begin():
            session.merge(decision)

    def decisions(self, limit: int) -> list[MaintenanceDecision]:
        """Newest first."""
        with self.session() as session:
            query = (select(MaintenanceDecision)
                     .order_by(MaintenanceDecision.decided_at.desc(), MaintenanceDecision.request_id.desc())
                     .limit(limit))
            return list(session.scalars(query))
