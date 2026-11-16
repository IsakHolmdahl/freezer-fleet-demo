"""The service: what it reads from the store and what its own API returns."""


def test_freezers_after_a_pass(worker, client):
    assert client.get("/freezers").json() == []
    worker.run_once()
    freezers = client.get("/freezers").json()
    assert len(freezers) == 10
    first = freezers[0]
    assert (first["id"], first["name"], first["status"], first["software_version"]) == (
        "frys-01", "Frys 1", "online", "1.4.2")
    assert first["alarm"] is False
    assert [f["id"] for f in client.get("/freezers", params={"status": "offline"}).json()] == ["frys-07"]
    assert client.get("/freezers/frys-07").json()["status"] == "offline"
    assert client.get("/freezers/nope").status_code == 404


def test_freezer_not_seen_for_long_counts_as_offline(worker, client, clock):
    worker.run_once()
    clock.advance(31)   # no pass since
    assert client.get("/freezers/frys-01").json()["status"] == "offline"


def test_readings_are_collected_once_each(worker, client, clock):
    worker.run_once()
    clock.advance(10)
    worker.run_once()
    worker.run_once()
    readings = client.get("/freezers/frys-01/readings").json()
    assert len(readings) == 3
    assert readings[0]["ts"] > readings[1]["ts"]   # newest first
    assert client.get("/freezers/frys-01/readings", params={"limit": 1}).json() == readings[:1]
    assert client.get("/freezers/nope/readings").status_code == 404


def test_warm_freezer_raises_an_alarm(worker, client, store):
    store.freezers["frys-02"].setpoint_c = -10.0
    worker.run_once()
    assert client.get("/freezers/frys-02").json()["alarm"] is True
    assert client.get("/summary").json()["alarms"] == 1


def test_defrost_warms_a_freezer_a_little_without_alarm_but_not_a_lot(worker, client, store):
    store.freezers["frys-03"].defrost_until = float("inf")
    store.freezers["frys-04"].defrost_until = float("inf")
    store.freezers["frys-04"].setpoint_c = 20.0
    worker.run_once()
    mild = client.get("/freezers/frys-03").json()
    assert (mild["defrosting"], mild["alarm"]) == (True, False)
    assert client.get("/freezers/frys-04").json()["alarm"] is True


def test_summary(worker, client):
    assert client.get("/summary").json()["last_sync"] is None
    worker.run_once()
    summary = client.get("/summary").json()
    assert (summary["store"], summary["freezers"], summary["online"], summary["offline"]) == ("Ica Teststad", 10, 9, 1)
    assert summary["software"] == {"1.4.2": 10}
    assert summary["last_sync"] is not None and summary["sync_error"] is None


def test_failed_pass_is_reported(worker, client, store):
    store.token = "changed"
    worker.run_once()
    assert "401" in client.get("/summary").json()["sync_error"]


def test_software_is_passed_through(client):
    assert [s["version"] for s in client.get("/software").json()] == ["1.4.2", "2.0.0"]


def test_health_and_dashboard(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert "<html" in client.get("/").text
