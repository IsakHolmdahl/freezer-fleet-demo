"""Copies the state of the freezers and their new readings from the store API into the database."""
from datetime import UTC, datetime

from storecloud.db import Database
from storecloud.storeapi import StoreApi


def sync_freezers(api: StoreApi, db: Database) -> int:
    """One pass over all freezers. Returns the number of freezers read."""
    freezers = api.freezers()
    for state in freezers:
        db.save_freezer(state)
        newest = db.newest_reading_ts(state.id)
        since = datetime.fromtimestamp(newest, UTC) if newest is not None else None
        db.add_readings(state.id, api.readings(state.id, since=since, limit=1000))
    return len(freezers)
