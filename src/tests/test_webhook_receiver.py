"""The standalone demo receiver (webhook_receiver/): signature checks, de-duplication, failure simulation."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from src.services.webhook_service import sign
from webhook_receiver.main import create_app

SECRET = "whsec_receiver_test"


@pytest.fixture
def receiver():
    with TestClient(create_app(secret=SECRET, db_path=":memory:", tolerance_seconds=300)) as c:
        yield c


def signed(event_id="evt_r1", secret=SECRET, ts=None, body=None):
    body = body or json.dumps({"id": event_id, "event": "case.escalated", "case_id": "CS-10231"}).encode()
    ts = ts or str(int(time.time()))
    return {"content": body, "headers": {"Content-Type": "application/json", "Webhook-Id": event_id,
                                         "Webhook-Timestamp": ts, "Webhook-Signature": sign(secret, event_id, ts, body)}}


def test_accepts_then_flags_duplicate(receiver):
    assert receiver.post("/webhooks/receive", **signed()).json()["status"] == "accepted"
    assert receiver.post("/webhooks/receive", **signed()).json()["status"] == "duplicate"
    data = receiver.get("/events").json()
    assert data["counts"]["accepted"] == 1 and data["counts"]["duplicate"] == 1
    newest = data["events"][0]
    assert newest["event_type"] == "case.escalated" and newest["signature_valid"] is True
    assert newest["payload"]["case_id"] == "CS-10231" and newest["headers"]["webhook-id"] == "evt_r1"


@pytest.mark.parametrize("kwargs", [
    {"secret": "wrong-secret"},
    {"ts": str(int(time.time()) - 3600)},
])
def test_rejects_bad_signature_and_replays(receiver, kwargs):
    r = receiver.post("/webhooks/receive", **signed(**kwargs))
    assert r.status_code == 401
    assert receiver.get("/events").json()["events"][0]["outcome"] == "rejected"


def test_tampered_body_rejected(receiver):
    req = signed()
    req["content"] = req["content"].replace(b"CS-10231", b"CS-99999")
    assert receiver.post("/webhooks/receive", **req).status_code == 401


def test_fail_next_then_recovers(receiver):
    receiver.put("/control", json={"mode": "fail_next", "fail_count": 2})
    codes = [receiver.post("/webhooks/receive", **signed()).status_code for _ in range(3)]
    assert codes == [500, 500, 200]
    assert receiver.get("/control").json()["mode"] == "ok"


def test_reject_mode_returns_400(receiver):
    receiver.put("/control", json={"mode": "reject"})
    assert receiver.post("/webhooks/receive", **signed()).status_code == 400
    receiver.put("/control", json={"mode": "ok"})
    assert receiver.delete("/events").json()["deleted"] == 1


def test_every_received_request_is_pushed_to_live_listeners(receiver):
    import queue
    listener = queue.Queue()  # stands in for one open /events/stream connection
    receiver.app.state.hub.listeners.add(listener)
    receiver.post("/webhooks/receive", **signed("evt_push"))
    receiver.post("/webhooks/receive", **signed("evt_push"))
    receiver.post("/webhooks/receive", **signed("evt_push", secret="wrong"))
    pushed = [listener.get_nowait() for _ in range(3)]
    assert [p["outcome"] for p in pushed] == ["accepted", "duplicate", "rejected"]
    assert pushed[0]["event_id"] == "evt_push" and pushed[0]["payload"]["case_id"] == "CS-10231"
