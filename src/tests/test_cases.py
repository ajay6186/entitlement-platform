from src.tests.conftest import auth


def ids(resp):
    return [c["id"] for c in resp.json()["data"]]


def test_search_filters_and_deterministic_sort(client):
    r = client.get("/v1/cases", params={"priority": "P1", "status": "open"}, headers=auth("support_agent"))
    assert r.status_code == 200
    assert ids(r) == ["CS-10231", "CS-10235", "CS-10245", "CS-10238", "CS-10234", "CS-10232", "CS-10233"]
    r = client.get("/v1/cases", params={"priority": "P1", "status": "open", "sort": "-opened_at"},
                   headers=auth("support_agent"))
    assert ids(r)[0] == "CS-10233"


def test_search_cross_system_tier_and_age_filters(client):
    r = client.get("/v1/cases", params={"priority": "P1", "status": "open", "support_tier": "Platinum",
                                        "opened_before": "2026-11-21T10:00:00Z"}, headers=auth("support_agent"))
    assert ids(r) == ["CS-10231", "CS-10245", "CS-10238", "CS-10234", "CS-10232"]


def test_multi_value_filter_ors_values(client):
    r = client.get("/v1/cases?priority=P3&priority=P4", headers=auth("support_agent"))
    assert set(ids(r)) == {"CS-10239", "CS-10242", "CS-10244"}


def test_invalid_filter_is_422(client):
    r = client.get("/v1/cases", params={"priority": "P9"}, headers=auth("support_agent"))
    assert r.status_code == 422 and r.json()["type"].endswith("/validation-error")
    assert client.get("/v1/cases", params={"limit": 0}, headers=auth("support_agent")).status_code == 422


def test_unknown_query_parameter_is_422(client):
    r = client.get("/v1/cases", params={"prioirty": "P1"}, headers=auth("support_agent"))
    assert r.status_code == 422 and "prioirty" in r.json()["detail"]


def test_cursor_pagination_walks_all_results_without_gaps(client):
    seen, cursor = [], None
    while True:
        params = {"limit": 4, **({"cursor": cursor} if cursor else {})}
        body = client.get("/v1/cases", params=params, headers=auth("support_agent")).json()
        seen += [c["id"] for c in body["data"]]
        cursor = body["page"]["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 15 and len(set(seen)) == 15


def test_cursor_bound_to_query(client):
    first = client.get("/v1/cases", params={"limit": 2}, headers=auth("support_agent")).json()
    r = client.get("/v1/cases", params={"limit": 2, "priority": "P1", "cursor": first["page"]["next_cursor"]},
                   headers=auth("support_agent"))
    assert r.status_code == 400 and r.json()["type"].endswith("/invalid-cursor")
    r = client.get("/v1/cases", params={"cursor": "garbage!!"}, headers=auth("support_agent"))
    assert r.status_code == 400


def test_get_case_returns_etag(client):
    r = client.get("/v1/cases/CS-10231", headers=auth("support_agent"))
    assert r.status_code == 200 and r.headers["etag"] == '"4"'
    assert client.get("/v1/cases/CS-99999", headers=auth("support_agent")).status_code == 404


def test_case_system_down_is_503(client):
    client.post("/dev/upstream-faults", json={"system": "case_system", "mode": "down"})
    assert client.get("/v1/cases", headers=auth("support_agent")).status_code == 503


# ---- escalation ------------------------------------------------------------------------------
def audit_rows(client, resource_id):
    return client.get("/v1/audit-logs", params={"resource_id": resource_id}, headers=auth("platform_admin")).json()


def test_unauthorized_escalation_is_403_and_audited(client):
    r = client.post("/v1/cases/CS-10231/escalate", json={"reason": "Critical outage"}, headers=auth("support_agent"))
    assert r.status_code == 403
    rows = audit_rows(client, "CS-10231")
    assert rows[0]["action"] == "case.escalate" and rows[0]["outcome"] == "denied"


def test_closed_case_escalation_is_409(client):
    r = client.post("/v1/cases/CS-10236/escalate", json={"reason": "Critical outage"}, headers=auth("support_lead"))
    assert r.status_code == 409 and r.json()["type"].endswith("/case-closed")


def test_already_escalated_is_409(client):
    r = client.post("/v1/cases/CS-10241/escalate", json={"reason": "Again"}, headers=auth("support_lead"))
    assert r.status_code == 409 and r.json()["type"].endswith("/case-already-escalated")


def test_stale_if_match_is_412(client):
    r = client.post("/v1/cases/CS-10231/escalate", json={"reason": "Critical outage"},
                    headers={**auth("support_lead"), "If-Match": '"3"'})
    assert r.status_code == 412 and r.json()["current_etag"] == '"4"'


def test_concurrent_modification_is_409(client, mock_state, monkeypatch):
    original_patch_version = mock_state.cases["CS-10231"]["version"]
    case_client = client.app.state.container.case_client
    real_get = case_client.get

    async def get_then_someone_else_writes(case_id):
        case = await real_get(case_id)
        mock_state.cases[case_id]["version"] = original_patch_version + 1  # concurrent writer wins the race
        return case

    monkeypatch.setattr(case_client, "get", get_then_someone_else_writes)
    r = client.post("/v1/cases/CS-10231/escalate", json={"reason": "Critical outage"}, headers=auth("support_lead"))
    assert r.status_code == 409 and r.json()["type"].endswith("/concurrent-modification")


def test_successful_escalation_audits_and_delivers_signed_webhook(client):
    etag = client.get("/v1/cases/CS-10231", headers=auth("support_lead")).headers["etag"]
    r = client.post("/v1/cases/CS-10231/escalate", json={"reason": "Critical production outage"},
                    headers={**auth("support_lead"), "If-Match": etag})
    assert r.status_code == 200
    assert r.json()["escalated"] is True and r.json()["escalation_reason"] == "Critical production outage"
    assert r.headers["etag"] == '"5"'

    rows = audit_rows(client, "CS-10231")
    assert any(a["action"] == "case.escalate" and a["outcome"] == "success" for a in rows)

    deliveries = client.get("/v1/webhooks/deliveries", headers=auth("platform_admin")).json()
    assert deliveries[0]["status"] == "delivered" and deliveries[0]["attempts"] == 1
    # second escalation now conflicts
    again = client.post("/v1/cases/CS-10231/escalate", json={"reason": "x" * 5}, headers=auth("support_lead"))
    assert again.status_code == 409


def test_escalation_validates_body(client):
    r = client.post("/v1/cases/CS-10231/escalate", json={"reason": "", "extra": 1}, headers=auth("support_lead"))
    assert r.status_code == 422
