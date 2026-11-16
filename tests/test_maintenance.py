"""Approval of maintenance requests."""
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def test_request_with_a_good_pass_is_approved_and_carried_out(worker, client, store):
    request = store.enqueue_request("frys-01", "set_setpoint", {"setpoint_c": -21.0})
    worker.run_once()
    assert store.maintenance_requests("approved")[0].id == request.id
    assert store.freezers["frys-01"].setpoint_c == -21.0
    (decision,) = client.get("/maintenance-requests").json()
    assert (decision["request_id"], decision["decision"], decision["technician"], decision["reason"]) == (
        request.id, "approve", "tech-0417", None)


def test_defrost_request_is_approved(worker, store):
    store.enqueue_request("frys-02", "defrost", {"duration_minutes": 20})
    worker.run_once()
    assert store.freezer_state("frys-02").defrosting is True


def test_pass_signed_by_someone_else_is_rejected(worker, client, store):
    forged = jwt.encode({"iss": "service-desk", "aud": store.id, "sub": "tech-0001", "exp": int(store.clock()) + 900},
                        Ed25519PrivateKey.generate(), algorithm="EdDSA")
    store.enqueue_request("frys-01", "set_setpoint", {"setpoint_c": -21.0}, technician_pass=forged)
    worker.run_once()
    assert store.maintenance_requests("rejected") and store.freezers["frys-01"].setpoint_c == -18.0
    (decision,) = client.get("/maintenance-requests").json()
    assert decision["decision"] == "reject" and "pass" in decision["reason"]


def test_pass_from_another_issuer_or_garbage_is_rejected(worker, store):
    store.enqueue_request("frys-01", "defrost", {"duration_minutes": 20}, technician_pass=store.mint_pass(iss="x"))
    store.enqueue_request("frys-01", "defrost", {"duration_minutes": 20}, technician_pass="not-a-pass")
    worker.run_once()
    assert len(store.maintenance_requests("rejected")) == 2


def test_malformed_request_is_rejected(worker, store):
    store.enqueue_request("frys-01", "open_door", {})
    store.enqueue_request("frys-01", "set_setpoint", {"setpoint_c": "cold"})
    store.enqueue_request("frys-01", "defrost", {})
    worker.run_once()
    assert len(store.maintenance_requests("rejected")) == 3


def test_each_request_is_decided_once(worker, client, store):
    store.enqueue_request("frys-01", "defrost", {"duration_minutes": 20})
    worker.run_once()
    worker.run_once()
    assert len(client.get("/maintenance-requests").json()) == 1
