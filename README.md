# Store cloud

The cloud service of one store. It watches the store's ten freezers, keeps their
temperature history, decides maintenance requests from service technicians and shows
it all on a dashboard.

The freezers are reached through the **store API** (`docs/store-api.md`). The service
calls that API; nothing needs to reach the service from outside, so it runs on your
own machine.

```
freezers  <──>  store API (hosted)  <── HTTPS ──  store cloud (this repo)  ──>  dashboard
```

## Quick start

Needs Python 3.11 or newer.

```bash
make install
make test
```

### With the fake store (no network)

```bash
make dev
```

Starts a stand-in for the store API on port 8100 and the service on
<http://localhost:8000>. The fake store has ten freezers that behave like the real
ones, and a technician who asks for maintenance on freezers with software 2.x.

### With your real store

Copy `.env.example` to `.env` and fill in the store endpoint and token you got at
sign-up, then:

```bash
make run
```

| Variable | Default | Meaning |
|---|---|---|
| `STORE_API_URL` | the fake store | Store endpoint, e.g. `https://<host>/api/v1/stores/ica-kiruna` |
| `STORE_API_TOKEN` | `dev-token` | Bearer token of the store |
| `DATABASE_URL` | `sqlite:///storecloud.db` | Where the service keeps its data |
| `POLL_INTERVAL_SECONDS` | `5` | How often the store API is read |
| `OFFLINE_AFTER_SECONDS` | `30` | A freezer not seen for this long counts as offline |
| `ALARM_ABOVE_C` | `-15` | Warmer than this is an alarm |
| `DEFROST_ALARM_ABOVE_C` | `0` | The alarm limit while a freezer defrosts |

## Commands

| Command | Does |
|---|---|
| `make install` | Creates `.venv` and installs the project with dev tools |
| `make test` | Unit tests |
| `make lint` | Ruff |
| `make acceptance` | Acceptance tests: the running service against the fake store, in one process |
| `make dev` | Fake store and service together |
| `make run` | Service against the store in `.env` |

None of them needs Docker or a network.

## Acceptance tests

`acceptance/` describes behaviour through the HTTP API of the service only.

- `test_baseline.py` passes today and must keep passing.
- `test_rollout.py` belongs to the feature in
  `docs/features/remote-software-installation.md`. **It fails today: the feature is
  not built yet.** The feature is done when it passes.

## Documentation

- `docs/store-api.md`: the store API (fixed).
- `docs/architecture.md`: how the service is built, its HTTP API, design rules.
- `docs/features/`: features asked for.
