"""Contract tests: implementation vs. the hand-written openapi.yaml.

1. Route parity: every documented operation is implemented and every implemented route is documented.
2. Response conformance: real responses (success and error) use a documented status code and media type,
   and their bodies validate against the documented JSON Schema (OpenAPI 3.1 == JSON Schema 2020-12).
"""
import json
import time

import pytest
import yaml
from fastapi.routing import APIRoute
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from src.main import OPENAPI_PATH
from src.services.webhook_service import sign
from src.tests.conftest import auth

SPEC = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
REGISTRY = Registry().with_resource("urn:openapi", DRAFT202012.create_resource(SPEC))


def _absolute_refs(node):
    if isinstance(node, dict):
        return {k: ("urn:openapi" + v if k == "$ref" and v.startswith("#") else _absolute_refs(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_absolute_refs(v) for v in node]
    return node


def _resolve(node):
    while "$ref" in node:
        parts = node["$ref"].lstrip("#/").split("/")
        node = SPEC
        for p in parts:
            node = node[p]
    return node


def assert_conforms(resp, template: str, method: str):
    op = SPEC["paths"][template][method.lower()]
    status = str(resp.status_code)
    assert status in op["responses"], f"{method} {template}: undocumented status {status}"
    documented = _resolve(op["responses"][status])
    if "content" not in documented:
        return
    media = resp.headers["content-type"].split(";")[0]
    assert media in documented["content"], f"{method} {template} {status}: undocumented media type {media}"
    schema = _absolute_refs(documented["content"][media]["schema"])
    Draft202012Validator(schema, registry=REGISTRY).validate(resp.json())


def _api_routes(routes):
    """Flatten routes; newer FastAPI wraps included routers (prefixes are already part of route.path)."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            yield from _api_routes(r.original_router.routes)


def test_every_documented_operation_is_implemented_and_vice_versa(client):
    implemented = {(r.path, m.lower()) for r in _api_routes(client.app.routes) if r.include_in_schema
                   for m in r.methods}
    assert implemented
    documented = {(p, m) for p, ops in SPEC["paths"].items() for m in ops if m in {"get", "post", "put", "patch", "delete"}}
    assert documented == implemented


def test_spec_is_served(client):
    assert client.get("/openapi.json").json()["info"]["title"] == SPEC["info"]["title"]
    assert client.get("/openapi.yaml").status_code == 200


def _webhook_request():
    event = {"id": "evt_contract", "event": "case.escalated", "case_id": "CS-10231", "customer_id": "ACC-007",
             "priority": "P1", "timestamp": "2026-11-23T10:00:00Z"}
    body, ts = json.dumps(event).encode(), str(int(time.time()))
    secret = "whsec_test_0123456789"
    return {"content": body, "headers": {"Content-Type": "application/json", "Webhook-Id": event["id"],
                                         "Webhook-Timestamp": ts, "Webhook-Signature": sign(secret, event["id"], ts, body)}}


SUPPORT, LEAD, FIN, ADMIN = (lambda: auth("support_agent")), (lambda: auth("support_lead")), \
    (lambda: auth("finance_analyst")), (lambda: auth("platform_admin"))

CASES = [
    # (method, url, template, request kwargs factory)
    ("GET", "/v1/customers/ACC-007", "/v1/customers/{customer_id}", lambda: {"headers": SUPPORT()}),
    ("GET", "/v1/customers/ACC-007", "/v1/customers/{customer_id}", lambda: {"headers": FIN()}),
    ("GET", "/v1/customers/ACC-999", "/v1/customers/{customer_id}", lambda: {"headers": SUPPORT()}),
    ("GET", "/v1/customers/bad", "/v1/customers/{customer_id}", lambda: {"headers": SUPPORT()}),
    ("GET", "/v1/customers/ACC-007", "/v1/customers/{customer_id}", lambda: {}),
    ("GET", "/v1/cases?priority=P1&status=open&limit=3", "/v1/cases", lambda: {"headers": SUPPORT()}),
    ("GET", "/v1/cases?priority=P9", "/v1/cases", lambda: {"headers": SUPPORT()}),
    ("GET", "/v1/cases?cursor=nope", "/v1/cases", lambda: {"headers": SUPPORT()}),
    ("GET", "/v1/cases", "/v1/cases", lambda: {"headers": FIN()}),
    ("GET", "/v1/cases/CS-10234", "/v1/cases/{case_id}", lambda: {"headers": SUPPORT()}),
    ("GET", "/v1/cases/CS-99999", "/v1/cases/{case_id}", lambda: {"headers": SUPPORT()}),
    ("POST", "/v1/cases/CS-10232/comments", "/v1/cases/{case_id}/comments",
     lambda: {"headers": {**SUPPORT(), "Idempotency-Key": "contract-key-1"}, "json": {"body": "hello"}}),
    ("POST", "/v1/cases/CS-10232/comments", "/v1/cases/{case_id}/comments",
     lambda: {"headers": SUPPORT(), "json": {"body": "hello"}}),
    ("POST", "/v1/cases/CS-10231/escalate", "/v1/cases/{case_id}/escalate",
     lambda: {"headers": SUPPORT(), "json": {"reason": "Outage"}}),
    ("POST", "/v1/cases/CS-10236/escalate", "/v1/cases/{case_id}/escalate",
     lambda: {"headers": LEAD(), "json": {"reason": "Outage"}}),
    ("POST", "/v1/cases/CS-10231/escalate", "/v1/cases/{case_id}/escalate",
     lambda: {"headers": {**LEAD(), "If-Match": '"1"'}, "json": {"reason": "Outage"}}),
    ("POST", "/v1/cases/CS-10231/escalate", "/v1/cases/{case_id}/escalate",
     lambda: {"headers": LEAD(), "json": {"reason": "Outage"}}),
    ("POST", "/v1/webhooks/receiver", "/v1/webhooks/receiver", _webhook_request),
    ("POST", "/v1/webhooks/receiver", "/v1/webhooks/receiver", lambda: {"json": {"id": "x"}}),
    ("GET", "/v1/webhooks/deliveries", "/v1/webhooks/deliveries", lambda: {"headers": ADMIN()}),
    ("GET", "/v1/webhooks/subscriptions", "/v1/webhooks/subscriptions", lambda: {"headers": ADMIN()}),
    ("POST", "/v1/webhooks/subscriptions", "/v1/webhooks/subscriptions",
     lambda: {"headers": ADMIN(), "json": {"endpoint_url": "https://example.test/h"}}),
    ("POST", "/v1/webhooks/subscriptions", "/v1/webhooks/subscriptions",
     lambda: {"headers": ADMIN(), "json": {"endpoint_url": "https://example.test/h", "event_types": ["nope"]}}),
    ("PATCH", "/v1/webhooks/subscriptions/whs_default_local", "/v1/webhooks/subscriptions/{subscription_id}",
     lambda: {"headers": ADMIN(), "json": {"event_types": ["case.escalated", "case.comment_added"]}}),
    ("PATCH", "/v1/webhooks/subscriptions/whs_missing", "/v1/webhooks/subscriptions/{subscription_id}",
     lambda: {"headers": ADMIN(), "json": {"enabled": False}}),
    ("GET", "/v1/webhooks/stats", "/v1/webhooks/stats", lambda: {"headers": ADMIN()}),
    ("GET", "/v1/webhooks/stats", "/v1/webhooks/stats", lambda: {"headers": LEAD()}),
    ("GET", "/v1/webhooks/events", "/v1/webhooks/events", lambda: {"headers": ADMIN()}),
    ("GET", "/v1/webhooks/events/evt_missing", "/v1/webhooks/events/{event_id}", lambda: {"headers": ADMIN()}),
    ("POST", "/v1/webhooks/test", "/v1/webhooks/test", lambda: {"headers": ADMIN()}),
    ("POST", "/v1/webhooks/test", "/v1/webhooks/test",
     lambda: {"headers": ADMIN(), "json": {"subscription_id": "whs_missing"}}),
    ("POST", "/v1/webhooks/deliveries/999/retry", "/v1/webhooks/deliveries/{delivery_id}/retry",
     lambda: {"headers": ADMIN()}),
    ("GET", "/v1/audit-logs", "/v1/audit-logs", lambda: {"headers": ADMIN()}),
    ("GET", "/v1/audit-logs", "/v1/audit-logs", lambda: {"headers": SUPPORT()}),
    ("GET", "/healthz", "/healthz", lambda: {}),
    ("POST", "/dev/token", "/dev/token", lambda: {"json": {"persona": "support_lead", "agent_id": "support-copilot"}}),
    ("POST", "/dev/token", "/dev/token", lambda: {"json": {"persona": "root"}}),
    ("POST", "/dev/upstream-faults", "/dev/upstream-faults", lambda: {"json": {"system": "usage_warehouse", "mode": "ok"}}),
    ("POST", "/dev/reset", "/dev/reset", lambda: {}),
]


@pytest.mark.parametrize("method,url,template,kwargs", CASES,
                         ids=[f"{c[0]} {c[1]} #{i}" for i, c in enumerate(CASES)])
def test_response_conforms_to_contract(client, method, url, template, kwargs):
    resp = client.request(method, url, **kwargs())
    assert_conforms(resp, template, method)


def test_degraded_and_upstream_error_responses_conform(client):
    client.post("/dev/upstream-faults", json={"system": "usage_warehouse", "mode": "down"})
    assert_conforms(client.get("/v1/customers/ACC-007", headers=SUPPORT()), "/v1/customers/{customer_id}", "GET")
    client.post("/dev/upstream-faults", json={"system": "entitlement_system", "mode": "down"})
    r = client.get("/v1/customers/ACC-007", headers=SUPPORT())
    assert r.status_code == 503
    assert_conforms(r, "/v1/customers/{customer_id}", "GET")
