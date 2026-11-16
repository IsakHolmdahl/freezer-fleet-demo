# Feature: remote software installation

## Why

The freezers in the store run software 1.4.2. Version 2.0.0 is ready. Today a
technician must visit the store to install it. The store's cloud service shall
install software on the freezers remotely, carefully enough that a bad version or an
unreliable freezer never takes the whole store down.

## What already exists

- The store API (`docs/store-api.md`) can list the installable software, tell **one**
  freezer to install a version, and report how that install goes. It does nothing
  more: no rollout across freezers, no retry, no order.
- This service already reads the freezers and shows them (`docs/architecture.md`).

## What to build

A **rollout**: one decision by an operator to bring a set of freezers to one software
version. The service carries it out in the background and survives a restart.

1. **Show what is installed.** The freezer list shows the software version of each
   freezer. The installable versions are listed.
2. **Start a rollout.** The operator chooses a version and, optionally, which
   freezers (default: all that do not run the version yet).
3. **First a few, then the rest.** The rollout starts with `canary_count` freezers.
   Only when all of these are installed does it continue with the others.
4. **Follow every freezer.** Each freezer in the rollout has a state:
   `pending` → `sent` → `downloading` → `installing` → `installed`, or `failed`.
5. **Freezers that cannot be reached or are busy.** The store API refuses with
   `offline` or `busy`. That is not a failure: the freezer stays `pending` and is
   tried again until it works. A rollout waits for such freezers and does not
   complete without them.
6. **Failed installs.** An install that ends as `failed` is tried again, up to
   `max_attempts` in total for that freezer. After that the freezer is `failed`.
7. **Stop when it goes wrong.** When more than `max_failed` freezers are `failed`,
   the rollout pauses by itself and sends no more commands. With fewer failures it
   goes on with the other freezers, and pauses when only `failed` freezers are left.
   The operator can resume it (every failed freezer becomes `pending` again and gets
   `max_attempts` new attempts; `attempts` keeps counting), or cancel it.
8. **Rollout view.** A screen that shows the rollouts and, for one rollout, every
   freezer with its state, attempts and last error, with buttons for pause, resume
   and cancel. It follows the look of the existing screens.

The freezers in the store are not all well behaved. Expect one that is offline for a
few minutes, one whose first install fails, and one whose door is often open.

## HTTP contract of this service

The acceptance tests use exactly this. Do not change names or shapes.

`GET /software` → `[{"version": "2.0.0", "released": "2026-11-10", "notes": "..."}]`
(the list of the store API).

`GET /freezers` and `GET /freezers/{id}` include `"software_version"`.

`POST /rollouts`

```json
{"version": "2.0.0", "freezer_ids": ["frys-01", "frys-02"],
 "canary_count": 2, "max_attempts": 3, "max_failed": 1}
```

Only `version` is required. Defaults: all freezers not on the version,
`canary_count` 2, `max_attempts` 3, `max_failed` 1. Answers `201` with the rollout.
`404` when the version is not installable, `422` when a freezer id is unknown, `409`
when another rollout is `running` or `paused`.

`GET /rollouts` (newest first) and `GET /rollouts/{id}` → the rollout:

```json
{
  "id": 1,
  "version": "2.0.0",
  "state": "running",
  "canary_count": 2,
  "max_attempts": 3,
  "max_failed": 1,
  "created_at": "2026-11-20T09:16:00Z",
  "finished_at": null,
  "freezers": [
    {"freezer_id": "frys-01", "canary": true, "state": "installed", "attempts": 1,
     "error": null},
    {"freezer_id": "frys-03", "canary": false, "state": "pending", "attempts": 1,
     "error": "checksum_mismatch"}
  ]
}
```

Rollout `state`: `running`, `paused`, `completed` (every freezer `installed`),
`cancelled`. Canaries are the first `canary_count` freezers in the order given (or in
freezer id order by default). `attempts` counts install commands the store API
accepted. `error` is the last error of that freezer and stays after a retry.

`POST /rollouts/{id}/pause`, `/resume`, `/cancel` → `200` with the rollout; `409` when
the rollout is not in a state that allows it (pause: `running`; resume: `paused`;
cancel: `running` or `paused`). Pausing or cancelling does not stop an install a
freezer has already started; its result is still recorded.

## Done when

- `make test` and `make lint` pass, with tests for the new behaviour.
- `make acceptance` passes, including `acceptance/test_rollout.py`.
- In the real store: all ten freezers run 2.0.0, by one rollout started from the
  rollout view.

## Out of scope

Building or signing freezer software, changing the store API, user accounts.
