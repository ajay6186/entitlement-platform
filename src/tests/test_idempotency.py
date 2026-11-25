from src.tests.conftest import auth, set_fault

URL = "/v1/cases/CS-10232/comments"


def post(client, body, key, persona="support_agent"):
    return client.post(URL, json={"body": body}, headers={**auth(persona), "Idempotency-Key": key})


def test_add_comment_creates_comment(client, mock_state):
    r = post(client, "We are investigating this issue.", "key-create-0001")
    assert r.status_code == 201
    body = r.json()
    assert body["case_id"] == "CS-10232" and body["body"] == "We are investigating this issue."
    assert body["author_id"] == "u-support-agent-01"
    assert r.headers["location"] == f"{URL}/{body['id']}" and r.headers["idempotent-replayed"] == "false"
    assert len(mock_state.cases["CS-10232"]["comments"]) == 1


def test_duplicate_idempotency_key_does_not_duplicate(client, mock_state):
    first = post(client, "Investigating.", "key-dup-0001")
    second = post(client, "Investigating.", "key-dup-0001")
    assert second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["idempotent-replayed"] == "true"
    assert len(mock_state.cases["CS-10232"]["comments"]) == 1


def test_same_key_different_payload_is_422(client):
    post(client, "First body.", "key-mismatch-01")
    r = post(client, "Different body.", "key-mismatch-01")
    assert r.status_code == 422 and r.json()["type"].endswith("/idempotency-key-reused")


def test_keys_are_scoped_per_caller(client, mock_state):
    post(client, "Same text.", "key-scope-0001", persona="support_agent")
    post(client, "Same text.", "key-scope-0001", persona="support_lead")
    assert len(mock_state.cases["CS-10232"]["comments"]) == 2


def test_missing_or_bad_key_is_400(client):
    r = client.post(URL, json={"body": "x"}, headers=auth("support_agent"))
    assert r.status_code == 400 and r.json()["type"].endswith("/idempotency-key-required")
    r = client.post(URL, json={"body": "x"}, headers={**auth("support_agent"), "Idempotency-Key": "short"})
    assert r.status_code == 400


def test_failed_attempt_can_be_retried_with_same_key(client, mock_state):
    set_fault(client, "case_system", "down")
    assert post(client, "Retry me.", "key-retry-0001").status_code == 503
    set_fault(client, "case_system", "ok")
    r = post(client, "Retry me.", "key-retry-0001")
    assert r.status_code == 201 and r.headers["idempotent-replayed"] == "false"
    assert len(mock_state.cases["CS-10232"]["comments"]) == 1


def test_comment_on_missing_case_is_404_and_blank_body_422(client):
    r = client.post("/v1/cases/CS-99999/comments", json={"body": "x"},
                    headers={**auth("support_agent"), "Idempotency-Key": "key-missing-01"})
    assert r.status_code == 404
    r = post(client, "   ", "key-blank-0001")
    assert r.status_code == 422


def test_comment_requires_scope(client):
    r = client.post(URL, json={"body": "x"}, headers={**auth("finance_analyst"), "Idempotency-Key": "key-scope-fin"})
    assert r.status_code == 403
