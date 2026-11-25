"""Webhook dashboard API (events, attempts, stats, retry, test) and outbox recovery."""
import asyncio

from src.models.tables import DomainEvent, WebhookDelivery
from src.tests.conftest import auth, make_client
from src.tests.test_contract import assert_conforms

ADMIN = auth("platform_admin")


def escalate(client, case_id="CS-10231"):
    etag = client.get(f"/v1/cases/{case_id}", headers=auth("support_lead")).headers["etag"]
    r = client.post(f"/v1/cases/{case_id}/escalate", json={"reason": "Data loss risk"},
                    headers={**auth("support_lead"), "If-Match": etag})
    assert r.status_code == 200
    return r


def test_escalation_event_listed_with_signed_attempt_log(client):
    escalate(client)
    events = client.get("/v1/webhooks/events", headers=ADMIN).json()
    assert events[0]["event_type"] == "case.escalated" and events[0]["status"] == "delivered"

    detail = client.get(f"/v1/webhooks/events/{events[0]['id']}", headers=ADMIN)
    assert_conforms(detail, "/v1/webhooks/events/{event_id}", "GET")
    body = detail.json()
    assert body["payload"]["case_id"] == "CS-10231" and body["payload"]["id"] == events[0]["id"]
    attempt = body["deliveries"][0]["attempt_log"][0]
    assert attempt["status_code"] == 200 and attempt["trigger"] == "auto"
    assert attempt["request_headers"]["Webhook-Id"] == events[0]["id"]
    assert attempt["request_headers"]["Webhook-Signature"].startswith("v1=")

    stats = client.get("/v1/webhooks/stats", headers=ADMIN).json()
    assert stats["events_by_type"]["case.escalated"] == 1 and stats["deliveries"]["delivered"] == 1
    assert stats["attempts_total"] == 1 and stats["failed_attempts"] == 0


def test_comment_publishes_event_once_even_when_replayed(tmp_path):
    client = make_client(tmp_path, webhook_default_event_types=["case.escalated", "case.comment_added"])
    try:
        headers = {**auth("support_agent"), "Idempotency-Key": "dashboard-key-1"}
        for _ in range(2):  # the replay must not publish a second event
            assert client.post("/v1/cases/CS-10232/comments", json={"body": "On it"}, headers=headers).status_code == 201
        events = client.get("/v1/webhooks/events?event_type=case.comment_added", headers=ADMIN).json()
        assert len(events) == 1 and events[0]["status"] == "delivered" and events[0]["resource_id"] == "CS-10232"
        payload = client.get(f"/v1/webhooks/events/{events[0]['id']}", headers=ADMIN).json()["payload"]
        assert payload["comment_id"].startswith("CM-") and "body" not in payload  # ids only, no customer text
    finally:
        client.__exit__(None, None, None)


def test_comment_event_without_subscribers_is_recorded(client):
    # The default test subscription only takes case.escalated.
    client.post("/v1/cases/CS-10232/comments", json={"body": "On it"},
                headers={**auth("support_agent"), "Idempotency-Key": "dashboard-key-2"})
    events = client.get("/v1/webhooks/events", headers=ADMIN).json()
    assert events[0]["event_type"] == "case.comment_added" and events[0]["status"] == "no_subscribers"


def test_test_event_and_failed_delivery_retry(client):
    sub = client.post("/v1/webhooks/subscriptions", headers=ADMIN, json={
        "endpoint_url": "http://testserver/nowhere", "event_types": ["case.escalated"]}).json()
    r = client.post("/v1/webhooks/test", headers=ADMIN, json={"subscription_id": sub["id"]})
    assert r.status_code == 202 and r.json()["deliveries"] == 1
    detail = client.get(f"/v1/webhooks/events/{r.json()['event_id']}", headers=ADMIN).json()
    delivery = detail["deliveries"][0]
    # 404 from the receiver is permanent: one attempt, no automatic retries.
    assert detail["event_type"] == "webhook.test" and delivery["status"] == "failed" and delivery["attempts"] == 1

    retried = client.post(f"/v1/webhooks/deliveries/{delivery['id']}/retry", headers=ADMIN)
    assert retried.status_code == 202
    log = client.get(f"/v1/webhooks/events/{r.json()['event_id']}", headers=ADMIN).json()["deliveries"][0]["attempt_log"]
    assert [a["trigger"] for a in log] == ["auto", "manual_retry"] and [a["attempt_no"] for a in log] == [1, 2]

    ok = client.post("/v1/webhooks/test", headers=ADMIN, json={"subscription_id": "whs_default_local"}).json()
    done = client.get(f"/v1/webhooks/events/{ok['event_id']}", headers=ADMIN).json()
    assert done["status"] == "delivered"
    delivered_id = done["deliveries"][0]["id"]
    assert client.post(f"/v1/webhooks/deliveries/{delivered_id}/retry", headers=ADMIN).status_code == 409


def test_disable_subscription_and_permissions(client):
    r = client.patch("/v1/webhooks/subscriptions/whs_default_local", json={"enabled": False}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert client.post("/v1/webhooks/test", headers=ADMIN).status_code == 409  # nothing enabled
    assert client.post("/v1/webhooks/test", headers=ADMIN,
                       json={"subscription_id": "whs_default_local"}).status_code == 409
    for path in ("/v1/webhooks/stats", "/v1/webhooks/events"):
        assert client.get(path, headers=auth("support_lead")).status_code == 403
    assert client.patch("/v1/webhooks/subscriptions/whs_default_local", json={"event_types": []},
                        headers=ADMIN).status_code == 422


def test_pending_deliveries_are_resumed(client):
    app = client.app

    async def leave_pending() -> str:
        async with app.state.sessionmaker() as session:
            session.add(DomainEvent(id="evt_stuck", event_type="case.escalated", resource_id="CS-10231",
                                    payload={"id": "evt_stuck", "event": "case.escalated", "case_id": "CS-10231",
                                             "customer_id": "ACC-007", "priority": "P1",
                                             "timestamp": "2026-11-23T10:00:00Z"}))
            session.add(WebhookDelivery(event_id="evt_stuck", subscription_id="whs_default_local", status="pending"))
            await session.commit()
        return "evt_stuck"

    portal_run = client.portal.call if hasattr(client, "portal") and client.portal else asyncio.run
    portal_run(leave_pending)
    assert client.get("/v1/webhooks/events/evt_stuck", headers=ADMIN).json()["status"] == "pending"
    portal_run(app.state.dispatcher.redeliver_pending)
    detail = client.get("/v1/webhooks/events/evt_stuck", headers=ADMIN).json()
    assert detail["status"] == "delivered" and detail["deliveries"][0]["attempt_log"][0]["trigger"] == "startup"
