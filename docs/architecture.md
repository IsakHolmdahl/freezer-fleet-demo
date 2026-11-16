# Architecture

## Parts

| Part | Role |
|---|---|
| Freezers | Ten freezers in the store. They measure temperature, install software and carry out maintenance. |
| Store API | The only way to reach the freezers (`docs/store-api.md`). Hosted elsewhere; not ours to change. |
| Store cloud (`storecloud`) | This service. Reads the freezers through the store API, keeps their history, decides maintenance requests, serves an HTTP API and a dashboard. |
| SQLite | Freezers, readings and maintenance decisions. |
| Fake store (`storecloud.fakestore`) | A stand-in for freezers and store API, for development and tests. |

The service only makes outgoing calls to the store API. Nothing calls the service
except its own users, so it can run on a laptop.

## Modules

| Module | Content |
|---|---|
| `config.py` | Settings from the environment. |
| `storeapi.py` | Data shapes of the store API and `StoreApi`, one method per call. |
| `db.py` | Tables and `Database`, the only code that touches SQLAlchemy sessions. |
| `sync.py` | Copies freezer states and new readings from the store API to the database. |
| `maintenance.py` | Decides pending maintenance requests: checks the technician pass and the request. |
| `app.py` | `create_app`, the HTTP API, the dashboard and `Worker`, the background loop. |
| `static/index.html` | The dashboard: one page that reads the HTTP API. |
| `fakestore/world.py` | `FakeStore`: the ten freezers and their behaviour. |
| `fakestore/app.py` | The store API served from a `FakeStore`; `in_process_api` for tests. |

## Flow

Every `POLL_INTERVAL_SECONDS` the `Worker` does one pass:

1. `sync_freezers`: read all freezers, save their state, fetch the readings that
   are newer than the newest one stored.
2. `Approver.run_once`: fetch the pending maintenance requests, decide each one,
   send the decision to the store API and record it.

A failed pass is logged and shown as `sync_error` in `GET /summary`; the next pass
tries again.

A freezer is **offline** when the store API says so or when its `last_seen` is more
than `OFFLINE_AFTER_SECONDS` old. It has an **alarm** when it is warmer than
`ALARM_ABOVE_C`; while it defrosts the limit is `DEFROST_ALARM_ABOVE_C`, because a
defrost warms it a little. Both are computed when the API is asked.

## HTTP API of the service

| Call | Returns |
|---|---|
| `GET /health` | `{"status": "ok"}` |
| `GET /freezers?status=online\|offline` | Freezers with status, temperature, setpoint, door, defrost, software version, alarm |
| `GET /freezers/{id}` | One freezer |
| `GET /freezers/{id}/readings?limit=` | Readings, newest first |
| `GET /summary` | Counts, software versions in use, time and error of the last pass |
| `GET /software` | Installable software, as listed by the store API |
| `GET /maintenance-requests?limit=` | Decided maintenance requests, newest first |
| `GET /` | Dashboard |

## The fake store

`FakeStore` follows `docs/store-api.md`. Its state moves with a clock that is passed
in, so unit tests control time. Its freezers have the habits of the real ones:

- `frys-07` is offline until some time after the first install command to the store.
- `frys-03` fails its first install of each version (`checksum_mismatch`).
- `frys-09` has its door open part of the time; `frys-05` defrosts for a while from
  the first install command. Both answer `busy` meanwhile.

Hooks for tests: `always_fail` and `force_offline` (sets of freezer ids),
`enqueue_request` (a technician asks for maintenance) and `mint_pass`.

## Design rules

- Only `storeapi.py` knows HTTP details of the store API; only `db.py` opens sessions.
- Logic takes its clock and its `StoreApi` as parameters, so tests need no network
  and no waiting.
- Background work starts in the lifespan of the app and stops with it. The service
  must come back in the same state after a restart, so state lives in the database.
- `create_app(settings, store_api=...)` is how the acceptance tests start the
  service; keep that signature.
- Timestamps are stored as Unix seconds in UTC and returned as ISO 8601.

## Limits

One process, no user accounts on the service itself, an unbounded readings table.
