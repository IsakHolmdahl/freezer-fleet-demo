"""What the service does today. These pass before any new feature is built and must keep passing."""
import jwt
from conftest import ALL, wait_until
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def test_all_freezers_are_listed_with_their_software(service):
    freezers = service.get("/freezers").json()
    assert [f["id"] for f in freezers] == ALL
    assert {f["software_version"] for f in freezers} == {"1.4.2"}
    assert service.get("/freezers/frys-01").json()["software_version"] == "1.4.2"


def test_unreachable_freezer_is_offline(service):
    assert [f["id"] for f in service.get("/freezers", params={"status": "offline"}).json()] == ["frys-07"]


def test_readings_keep_arriving(service):
    first = wait_until(lambda: service.get("/freezers/frys-01/readings").json(), message="a reading")
    wait_until(lambda: service.get("/freezers/frys-01/readings").json()[0]["ts"] > first[0]["ts"],
               message="a newer reading")


def test_summary_and_software(service):
    summary = service.get("/summary").json()
    assert (summary["freezers"], summary["online"], summary["offline"]) == (10, 9, 1)
    assert [s["version"] for s in service.get("/software").json()] == ["1.4.2", "2.0.0"]


def test_maintenance_request_from_a_technician_is_approved(service, store):
    request = store.enqueue_request("frys-02", "set_setpoint", {"setpoint_c": -22.0})
    wait_until(lambda: store.maintenance_requests("approved"), message="the approval")
    assert store.freezers["frys-02"].setpoint_c == -22.0
    decisions = wait_until(lambda: service.get("/maintenance-requests").json(), message="the decision")
    assert (decisions[0]["request_id"], decisions[0]["decision"]) == (request.id, "approve")


def test_maintenance_request_with_a_forged_pass_is_rejected(service, store):
    forged = jwt.encode({"iss": "service-desk", "aud": store.id, "sub": "tech-0001", "exp": 4102444800},
                        Ed25519PrivateKey.generate(), algorithm="EdDSA")
    store.enqueue_request("frys-02", "set_setpoint", {"setpoint_c": -22.0}, technician_pass=forged)
    wait_until(lambda: store.maintenance_requests("rejected"), message="the rejection")
    assert store.freezers["frys-02"].setpoint_c == -18.0
