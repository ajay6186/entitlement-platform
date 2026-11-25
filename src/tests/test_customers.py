from src.tests.conftest import auth, set_fault


def test_customer_exists_returns_aggregated_view(client):
    r = client.get("/v1/customers/ACC-007", headers=auth("support_agent"))
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Acme Corp" and body["support_tier"] == "Platinum"
    assert body["renewal_date"] == "2027-01-15" and body["days_until_renewal"] == 53
    assert body["usage"] == {"protected_tb": 412.5, "api_calls": 1203331, "active_users": 87, "as_of": "2026-11-22"}
    assert body["degraded"] is False


def test_arr_redacted_for_support_visible_for_finance(client):
    support = client.get("/v1/customers/ACC-007", headers=auth("support_agent")).json()
    assert "arr_usd" not in support and support["redacted_fields"] == ["arr_usd"]
    finance = client.get("/v1/customers/ACC-007", headers=auth("finance_analyst")).json()
    assert finance["arr_usd"] == 480000 and finance["redacted_fields"] == []


def test_customer_not_found_is_404_problem(client):
    r = client.get("/v1/customers/ACC-999", headers=auth("support_agent"))
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"].endswith("/not-found") and r.json()["correlation_id"]


def test_malformed_customer_id_is_422(client):
    r = client.get("/v1/customers/not-an-id", headers=auth("support_agent"))
    assert r.status_code == 422 and r.json()["errors"]


def test_usage_down_returns_degraded_partial_view(client):
    set_fault(client, "usage_warehouse", "down")
    r = client.get("/v1/customers/ACC-007", headers=auth("support_agent"))
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True and body["degraded_sources"] == ["usage"] and body["usage"] is None
    assert body["name"] == "Acme Corp"  # entitlement data still served


def test_usage_timeout_returns_degraded(client):
    set_fault(client, "usage_warehouse", "slow")
    r = client.get("/v1/customers/ACC-007", headers=auth("support_agent"))
    assert r.status_code == 200 and r.json()["degraded"] is True


def test_flaky_usage_recovered_by_retry(client):
    set_fault(client, "usage_warehouse", "flaky")  # every other call fails with 502
    r = client.get("/v1/customers/ACC-007", headers=auth("support_agent"))
    assert r.status_code == 200 and r.json()["degraded"] is False


def test_entitlement_down_is_503_problem(client):
    set_fault(client, "entitlement_system", "down")
    r = client.get("/v1/customers/ACC-007", headers=auth("support_agent"))
    assert r.status_code == 503
    assert r.json()["upstream"] == "entitlement system" and r.headers["retry-after"]


def test_circuit_breaker_opens_and_stops_calling_upstream(client, mock_state):
    set_fault(client, "usage_warehouse", "down")
    for _ in range(5):  # threshold = 5 failed requests
        client.get("/v1/customers/ACC-007", headers=auth("support_agent"))
    assert client.get("/healthz").json()["upstreams"]["usage_warehouse"] == "open"
    calls_before = mock_state.calls["usage_warehouse"]
    r = client.get("/v1/customers/ACC-007", headers=auth("support_agent"))
    assert r.json()["degraded"] is True
    assert mock_state.calls["usage_warehouse"] == calls_before  # short-circuited, no upstream call


def test_missing_token_401_and_missing_scope_403(client):
    r = client.get("/v1/customers/ACC-007")
    assert r.status_code == 401 and "Bearer" in r.headers["www-authenticate"]
    r = client.get("/v1/cases", headers=auth("finance_analyst"))  # finance has no cases:read
    assert r.status_code == 403
