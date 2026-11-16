"""The fake store behaves as docs/store-api.md says, including the awkward freezers."""
import pytest

from storecloud.fakestore.world import Refused


def test_ten_freezers_on_current_software_one_offline(store):
    freezers = store.freezer_states()
    assert [f.id for f in freezers] == [f"frys-{n:02d}" for n in range(1, 11)]
    assert {f.software_version for f in freezers} == {"1.4.2"}
    assert [f.id for f in freezers if not f.online] == ["frys-07"]


def test_install_runs_through_its_states(store, clock):
    install = store.start_install("frys-01", "2.0.0")
    assert (install.state, install.progress_pct) == ("downloading", 0)
    clock.advance(4)
    assert store.freezer_state("frys-01").install.progress_pct == 50
    clock.advance(5)
    assert store.freezer_state("frys-01").install.state == "installing"
    clock.advance(5)
    freezer = store.freezer_state("frys-01")
    assert (freezer.install.state, freezer.software_version) == ("installed", "2.0.0")
    assert [i.state for i in store.all_installs()] == ["installed"]


def test_refusals(store, clock):
    def code(freezer_id, version="2.0.0"):
        with pytest.raises(Refused) as refused:
            store.start_install(freezer_id, version)
        return refused.value.code

    assert code("frys-01", "9.9.9") == "unknown_version"
    assert code("frys-07") == "offline"
    assert code("frys-05") == "busy"            # defrosts from the first install command
    assert code("frys-09") == "busy"            # door open at the start of every period
    assert code("frys-01", "1.4.2") == "already_installed"
    store.start_install("frys-01", "2.0.0")
    assert code("frys-01") == "install_in_progress"
    clock.advance(41)
    assert store.start_install("frys-07", "2.0.0").state == "downloading"   # back online
    assert store.start_install("frys-05", "2.0.0").state == "downloading"   # defrost over


def test_frys_03_fails_its_first_install_of_a_version(store, clock):
    store.start_install("frys-03", "2.0.0")
    clock.advance(6)
    install = store.freezer_state("frys-03").install
    assert (install.state, install.error) == ("failed", "checksum_mismatch")
    store.start_install("frys-03", "2.0.0")
    clock.advance(14)
    assert store.freezer_state("frys-03").software_version == "2.0.0"


def test_always_fail_hook(store, clock):
    store.always_fail.add("frys-02")
    store.start_install("frys-02", "2.0.0")
    clock.advance(20)
    freezer = store.freezer_state("frys-02")
    assert (freezer.install.state, freezer.install.error) == ("failed", "flash_error")
    assert freezer.software_version == "1.4.2"


def test_readings_every_five_seconds_but_not_while_offline(store, clock):
    clock.advance(20)
    assert len(store.readings("frys-01", None, 200)) == 5
    assert store.readings("frys-07", None, 200) == []
    newest = store.readings("frys-01", None, 1)[0]
    clock.advance(5)
    assert len(store.readings("frys-01", newest.ts, 200)) == 1


def test_undecided_request_expires(store, clock):
    request = store.enqueue_request("frys-01", "defrost", {"duration_minutes": 20})
    clock.advance(91)
    assert store.maintenance_requests("expired")[0].id == request.id
    with pytest.raises(Refused) as refused:
        store.decide(request.id, "approve", None)
    assert refused.value.code == "already_decided"


def test_energy_is_stable_per_hour(store):
    rows = store.energy(3)
    assert len(rows) == 30 and rows == store.energy(3)
