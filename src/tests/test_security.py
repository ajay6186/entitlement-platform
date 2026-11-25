import json
import time

import jwt

from src.config import get_settings
from src.core.security import issue_token
from src.services.webhook_service import sign
from src.tests.conftest import auth, make_client

SECRET = "whsec_test_0123456789"


def signed_headers(event: dict, secret: str = SECRET, ts: int | None = None):
    body = json.dumps(event).encode()
    ts = str(ts or int(time.time()))
    return body, {"Content-Type": "application/json", "Webhook-Id": event["id"], "Webhook-Timestamp": ts,
                  "Webhook-Signature": sign(secret, event["id"], ts, body)}


EVENT = {"id": "evt_test_1", "event": "case.escalated", "case_id": "CS-10231", "customer_id": "ACC-007",
         "priority": "P1", "timestamp": "2026-11-23T10:00:00Z"}


def test_valid_webhook_accepted_then_duplicate(client):
    body, headers = signed_headers(EVENT)
    r = client.post("/v1/webhooks/receiver", content=body, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    r = client.post("/v1/webhooks/receiver", content=body, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "duplicate"


def test_invalid_webhook_signature_rejected(client):
    body, headers = signed_headers(EVENT, secret="wrong-secret")
    r = client.post("/v1/webhooks/receiver", content=body, headers=headers)
    assert r.status_code == 401 and r.json()["type"].endswith("/invalid-webhook-signature")


def test_tampered_webhook_body_rejected(client):
    body, headers = signed_headers(EVENT)
    tampered = body.replace(b"CS-10231", b"CS-10232")
    assert client.post("/v1/webhooks/receiver", content=tampered, headers=headers).status_code == 401


def test_replayed_old_webhook_rejected(client):
    body, headers = signed_headers({**EVENT, "id": "evt_old"}, ts=int(time.time()) - 3600)
    assert client.post("/v1/webhooks/receiver", content=body, headers=headers).status_code == 401


def test_missing_signature_headers_rejected(client):
    assert client.post("/v1/webhooks/receiver", json=EVENT).status_code == 401


def test_expired_and_forged_tokens_rejected(client):
    expired = issue_token("u-x", ["cases:read"], ttl_seconds=-10)
    assert client.get("/v1/cases", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    s = get_settings()
    forged = jwt.encode({"sub": "u-x", "scope": "cases:read cases:escalate", "iss": s.jwt_issuer,
                         "aud": s.jwt_audience, "exp": int(time.time()) + 60}, "attacker-secret-0123456789abcdef",
                        algorithm="HS256")
    assert client.get("/v1/cases", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    unsigned = jwt.encode({"sub": "u-x", "scope": "cases:read"}, key=None, algorithm="none")
    assert client.get("/v1/cases", headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401


def test_agent_cannot_exceed_user_scopes(client):
    # support_agent acting through the copilot still lacks cases:escalate
    r = client.post("/v1/cases/CS-10231/escalate", json={"reason": "Critical outage"},
                    headers=auth("support_agent", agent_id="support-copilot"))
    assert r.status_code == 403


def test_agent_requests_are_audited_with_agent_identity(client):
    client.get("/v1/customers/ACC-007", headers=auth("support_agent", agent_id="support-copilot"))
    rows = client.get("/v1/audit-logs", params={"action": "agent.api_request"}, headers=auth("platform_admin")).json()
    assert rows and rows[0]["agent_id"] == "support-copilot" and rows[0]["actor_id"] == "u-support-agent-01"


def test_correlation_id_echoed_and_rate_limit_headers(client):
    r = client.get("/v1/cases", headers={**auth("support_agent"), "X-Correlation-ID": "abc-123"})
    assert r.headers["x-correlation-id"] == "abc-123"
    assert int(r.headers["ratelimit-limit"]) == 120 and "ratelimit-remaining" in r.headers
    r = client.get("/v1/cases", headers={**auth("support_agent"), "X-Correlation-ID": "bad id with spaces"})
    assert r.headers["x-correlation-id"] != "bad id with spaces"


def test_rate_limit_returns_429(tmp_path):
    c = make_client(tmp_path, rate_limit_per_minute=3)
    try:
        statuses = [c.get("/v1/cases", headers=auth("support_agent")).status_code for _ in range(4)]
        assert statuses == [200, 200, 200, 429]
        r = c.get("/v1/cases", headers=auth("support_agent"))
        assert r.headers["retry-after"] and r.json()["type"].endswith("/rate-limited")
    finally:
        c.__exit__(None, None, None)


def test_subscription_secret_must_be_reference(client):
    r = client.post("/v1/webhooks/subscriptions", json={"endpoint_url": "https://example.test/hook",
                                                        "secret_ref": "plain-secret-value"},
                    headers=auth("platform_admin"))
    assert r.status_code == 422
    r = client.post("/v1/webhooks/subscriptions", json={"endpoint_url": "https://example.test/hook"},
                    headers=auth("platform_admin"))
    assert r.status_code == 201 and r.json()["secret_ref"].startswith("env:")
