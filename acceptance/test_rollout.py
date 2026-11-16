"""Acceptance tests of docs/features/remote-software-installation.md.

They fail until the feature exists. Do not change them.
"""
import time

from conftest import ALL, wait_until

NEW = "2.0.0"


def start(service, **body) -> dict:
    response = service.post("/rollouts", json={"version": NEW} | body)
    assert response.status_code == 201, response.text
    return response.json()


def wait_for_state(service, rollout_id: int, state: str, timeout: float = 40.0) -> dict:
    wait_until(lambda: service.rollout(rollout_id)["state"] == state, timeout,
               f"rollout {rollout_id} to be {state} (it is {service.rollout(rollout_id)['state']})")
    return service.rollout(rollout_id)


def by_freezer(rollout: dict) -> dict[str, dict]:
    return {f["freezer_id"]: f for f in rollout["freezers"]}


def versions(store) -> dict[str, str]:
    return {f.id: f.software_version for f in store.freezer_states()}


def commands_settle(store, quiet: float = 1.5) -> int:
    """Waits until running installs are over, then checks that no new install command arrives
    for `quiet` seconds. Returns the number of install commands the store has received."""
    wait_until(lambda: all(i.state in ("installed", "failed") for i in store.all_installs()),
               message="running installs to finish")
    count = len(store.all_installs())
    time.sleep(quiet)
    assert len(store.all_installs()) == count, "install commands were still sent"
    return count


# -- starting and the shape of a rollout -------------------------------------------------


def test_new_rollout_covers_all_freezers_and_marks_the_canaries(service):
    rollout = start(service)
    assert rollout["version"] == NEW
    assert rollout["state"] == "running"
    assert (rollout["canary_count"], rollout["max_attempts"], rollout["max_failed"]) == (2, 3, 1)
    assert rollout["finished_at"] is None
    assert [f["freezer_id"] for f in rollout["freezers"]] == ALL
    assert [f["freezer_id"] for f in rollout["freezers"] if f["canary"]] == ["frys-01", "frys-02"]
    assert service.rollout(rollout["id"])["id"] == rollout["id"]


def test_rollout_brings_every_freezer_to_the_new_version(service, store):
    rollout = wait_for_state(service, start(service)["id"], "completed")
    assert set(versions(store).values()) == {NEW}
    assert {f["state"] for f in rollout["freezers"]} == {"installed"}
    assert rollout["finished_at"] is not None
    wait_until(lambda: {f["software_version"] for f in service.get("/freezers").json()} == {NEW},
               message="the freezer list to show the new version")


def test_failed_install_is_tried_again(service, store):
    """frys-03 fails its first install of a version."""
    rollout = wait_for_state(service, start(service)["id"], "completed")
    frys_03 = by_freezer(rollout)["frys-03"]
    assert (frys_03["state"], frys_03["attempts"], frys_03["error"]) == ("installed", 2, "checksum_mismatch")
    assert by_freezer(rollout)["frys-01"]["attempts"] == 1
    assert by_freezer(rollout)["frys-01"]["error"] is None


def test_only_the_chosen_freezers_in_the_order_given(service, store):
    rollout = start(service, freezer_ids=["frys-04", "frys-02"], canary_count=1)
    assert [(f["freezer_id"], f["canary"]) for f in rollout["freezers"]] == [("frys-04", True), ("frys-02", False)]
    wait_for_state(service, rollout["id"], "completed")
    assert {fid for fid, version in versions(store).items() if version == NEW} == {"frys-02", "frys-04"}


def test_freezers_already_on_the_version_are_left_out_by_default(service, store):
    wait_for_state(service, start(service, freezer_ids=["frys-01"], canary_count=1)["id"], "completed")
    rollout = start(service)
    assert "frys-01" not in by_freezer(rollout)
    assert len(rollout["freezers"]) == 9


# -- canaries first -----------------------------------------------------------------------


def test_others_wait_until_all_canaries_are_installed(service, store):
    rollout = start(service)
    deadline = time.monotonic() + 40
    while service.rollout(rollout["id"])["state"] != "completed":
        assert time.monotonic() < deadline, "rollout did not complete"
        # read the versions first: whatever was commanded after that had the canaries installed
        installed = versions(store)
        commanded = {i.freezer_id for i in store.all_installs()}
        if commanded - {"frys-01", "frys-02"}:
            assert installed["frys-01"] == NEW and installed["frys-02"] == NEW
            break
        time.sleep(0.02)


def test_failing_canary_stops_the_rollout_before_the_others(service, store):
    store.always_fail.add("frys-01")
    rollout = wait_for_state(service, start(service, max_attempts=2, max_failed=0)["id"], "paused")
    frys_01 = by_freezer(rollout)["frys-01"]
    assert (frys_01["state"], frys_01["attempts"], frys_01["error"]) == ("failed", 2, "flash_error")
    commands_settle(store)
    assert {i.freezer_id for i in store.all_installs()} <= {"frys-01", "frys-02"}
    assert all(by_freezer(service.rollout(rollout["id"]))[fid]["state"] == "pending" for fid in ALL[2:])


# -- freezers that cannot be reached or are busy -------------------------------------------


def test_offline_freezer_is_waited_for_and_caught_up(service, store):
    store.force_offline.add("frys-07")
    rollout = start(service)

    def others_installed():
        freezers = by_freezer(service.rollout(rollout["id"]))
        return all(f["state"] == "installed" for fid, f in freezers.items() if fid != "frys-07")

    wait_until(others_installed, 40, "the nine reachable freezers to be installed")
    time.sleep(0.5)
    waiting = service.rollout(rollout["id"])
    assert waiting["state"] == "running"
    frys_07 = by_freezer(waiting)["frys-07"]
    assert (frys_07["state"], frys_07["attempts"]) == ("pending", 0)

    store.force_offline.clear()
    done = wait_for_state(service, rollout["id"], "completed")
    assert by_freezer(done)["frys-07"]["attempts"] == 1
    assert versions(store)["frys-07"] == NEW


def test_busy_freezers_do_not_count_as_failed(service, store):
    """frys-05 is defrosting when the rollout starts and the door of frys-09 is often open."""
    rollout = wait_for_state(service, start(service, max_attempts=1, freezer_ids=["frys-05", "frys-09"],
                                            canary_count=1)["id"], "completed")
    assert [(f["state"], f["attempts"]) for f in rollout["freezers"]] == [("installed", 1), ("installed", 1)]


# -- stopping when it goes wrong -------------------------------------------------------------


def test_rollout_pauses_itself_when_too_many_freezers_fail_and_can_be_resumed(service, store):
    store.always_fail.update({"frys-04", "frys-06"})
    rollout = wait_for_state(service, start(service, max_attempts=2, max_failed=1)["id"], "paused")
    failed = {fid: f for fid, f in by_freezer(rollout).items() if f["state"] == "failed"}
    assert set(failed) == {"frys-04", "frys-06"}
    assert all((f["attempts"], f["error"]) == (2, "flash_error") for f in failed.values())
    commands_settle(store)
    assert service.rollout(rollout["id"])["state"] == "paused"

    store.always_fail.clear()
    response = service.post(f"/rollouts/{rollout['id']}/resume")
    assert response.status_code == 200 and response.json()["state"] == "running"
    wait_for_state(service, rollout["id"], "completed")
    assert set(versions(store).values()) == {NEW}


def test_one_failed_freezer_within_the_limit_does_not_stop_the_others(service, store):
    store.always_fail.add("frys-04")
    rollout = start(service, max_attempts=2, max_failed=1)

    def rest_installed():
        freezers = by_freezer(service.rollout(rollout["id"]))
        return freezers["frys-04"]["state"] == "failed" and all(
            f["state"] == "installed" for fid, f in freezers.items() if fid != "frys-04")

    wait_until(rest_installed, 40, "nine installed and frys-04 failed")
    wait_for_state(service, rollout["id"], "paused")   # nothing left to do: waits for the operator

    store.always_fail.clear()
    assert service.post(f"/rollouts/{rollout['id']}/resume").status_code == 200
    wait_for_state(service, rollout["id"], "completed")


# -- operator controls -----------------------------------------------------------------------


def test_pause_and_resume_by_the_operator(service, store):
    rollout = start(service)
    paused = service.post(f"/rollouts/{rollout['id']}/pause")
    assert paused.status_code == 200 and paused.json()["state"] == "paused"
    assert service.post(f"/rollouts/{rollout['id']}/pause").status_code == 409
    commands_settle(store)
    assert set(versions(store).values()) != {NEW}
    resumed = service.post(f"/rollouts/{rollout['id']}/resume")
    assert resumed.status_code == 200 and resumed.json()["state"] == "running"
    assert service.post(f"/rollouts/{rollout['id']}/resume").status_code == 409
    wait_for_state(service, rollout["id"], "completed")


def test_cancel_stops_the_rollout_for_good(service, store):
    rollout = start(service)
    cancelled = service.post(f"/rollouts/{rollout['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert commands_settle(store) <= 2   # at most the canaries were told to install
    assert service.rollout(rollout["id"])["state"] == "cancelled"
    for action in ("pause", "resume", "cancel"):
        assert service.post(f"/rollouts/{rollout['id']}/{action}").status_code == 409
    assert service.post("/rollouts", json={"version": NEW}).status_code == 201   # a new one may start


def test_install_already_started_is_still_recorded_after_a_pause(service, store):
    rollout = start(service)
    wait_until(lambda: store.all_installs(), message="the first install command")
    service.post(f"/rollouts/{rollout['id']}/pause")
    commands_settle(store)
    started = {i.freezer_id for i in store.all_installs()}
    freezers = by_freezer(service.rollout(rollout["id"]))
    wait_until(lambda: all(by_freezer(service.rollout(rollout["id"]))[fid]["state"] == "installed"
                           for fid in started if fid != "frys-03"),
               10, "the results of started installs to be recorded")
    assert all(freezers[fid]["state"] == "pending" for fid in ALL if fid not in started)


# -- refusals ----------------------------------------------------------------------------------


def test_refused_requests(service):
    assert service.post("/rollouts", json={"version": "9.9.9"}).status_code == 404
    assert service.post("/rollouts", json={"version": NEW, "freezer_ids": ["frys-99"]}).status_code == 422
    assert service.post("/rollouts", json={}).status_code == 422
    assert service.get("/rollouts/999").status_code == 404
    assert service.post("/rollouts/999/pause").status_code == 404
    assert service.get("/rollouts").json() == []


def test_one_rollout_at_a_time_and_newest_first(service):
    first = start(service, freezer_ids=["frys-01"], canary_count=1)
    assert service.post("/rollouts", json={"version": NEW}).status_code == 409
    service.post(f"/rollouts/{first['id']}/pause")
    assert service.post("/rollouts", json={"version": NEW}).status_code == 409   # paused still blocks
    service.post(f"/rollouts/{first['id']}/resume")
    wait_for_state(service, first["id"], "completed")
    second = start(service, freezer_ids=["frys-02"], canary_count=1)
    assert [r["id"] for r in service.get("/rollouts").json()] == [second["id"], first["id"]]


# -- restart -------------------------------------------------------------------------------------


def test_rollout_continues_after_a_restart_of_the_service(service, store):
    store.force_offline.add("frys-07")
    rollout = start(service)
    wait_until(lambda: by_freezer(service.rollout(rollout["id"]))["frys-01"]["state"] == "installed",
               message="the first canary")
    service.restart()
    assert service.rollout(rollout["id"])["state"] == "running"
    store.force_offline.clear()
    wait_for_state(service, rollout["id"], "completed")
    assert set(versions(store).values()) == {NEW}
