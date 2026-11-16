# Agent instructions

Read `README.md`, `docs/architecture.md` and `docs/store-api.md` first. A feature you
are asked to build is described in `docs/features/`.

## Commands

- `make install` sets up. `make test`, `make lint` and `make acceptance` must pass
  before you report work as done. None needs Docker or a network.
- `acceptance/test_rollout.py` fails until the remote software installation feature
  is built. Any other failing acceptance test is a regression.
- `make dev` runs the service against the fake store for a look at the dashboard.

## Rules

- Do not change `acceptance/`, `docs/features/` or `docs/store-api.md`: they define
  what is asked for. The store API is hosted elsewhere and cannot be changed.
- Keep the fake store (`src/storecloud/fakestore/`) true to `docs/store-api.md`. Do
  not bend it to make a test pass.
- Follow the design rules in `docs/architecture.md`. Keep
  `create_app(settings, store_api=...)`: the acceptance tests start the service with it.
- New behaviour gets unit tests in `tests/`, on the fake store and a controlled clock.
- Use the libraries already in `pyproject.toml` before adding new ones. Prefer a
  popular, maintained library over code of your own for anything non-trivial; say
  which one and why.
- New screens follow the look of the existing dashboard.
- Update `README.md` and `docs/architecture.md` when behaviour changes.
