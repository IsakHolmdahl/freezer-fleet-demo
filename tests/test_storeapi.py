"""The store API client against the fake store."""
import pytest

from storecloud.fakestore.app import in_process_api
from storecloud.storeapi import StoreApiError


def test_reads_store_freezers_and_software(api):
    assert api.store().id == "ica-teststad"
    assert len(api.freezers()) == 10
    assert api.freezer("frys-02").software_version == "1.4.2"
    assert [s.version for s in api.software()] == ["1.4.2", "2.0.0"]
    assert api.technician_pass_key().algorithm == "EdDSA"
    assert len(api.energy(hours=2)) == 20


def test_install_and_installs(api):
    install = api.install("frys-01", "2.0.0")
    assert install.state == "downloading"
    assert [i.id for i in api.installs()] == [install.id]


def test_refusal_carries_the_code(api):
    with pytest.raises(StoreApiError) as error:
        api.install("frys-07", "2.0.0")
    assert (error.value.status, error.value.code) == (409, "offline")


def test_wrong_token_and_wrong_store(store):
    with pytest.raises(StoreApiError) as error:
        in_process_api(store, token="nope").freezers()
    assert error.value.status == 401
    with pytest.raises(StoreApiError) as error:
        in_process_api(store, store_id="ica-annanstad").freezers()
    assert error.value.status == 403


def test_maintenance_request_round_trip(api, store):
    request = store.enqueue_request("frys-01", "set_setpoint", {"setpoint_c": -20.0})
    (pending,) = api.maintenance_requests("pending")
    assert pending.id == request.id and pending.technician_pass == request.technician_pass
    assert api.decide(request.id, "reject", "test").status == "rejected"
    with pytest.raises(StoreApiError) as error:
        api.decide(request.id, "approve")
    assert error.value.code == "already_decided"
