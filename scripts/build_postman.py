"""Generates postman/collection.json and postman/environment.json (run: python -m scripts.build_postman)."""
import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "postman"


def req(name, method, path, tests, body=None, headers=None, auth_var="supportToken", prerequest=None):
    h = [{"key": k, "value": v} for k, v in (headers or {}).items()]
    if auth_var:
        h.append({"key": "Authorization", "value": "Bearer {{" + auth_var + "}}"})
    if body is not None:
        h.append({"key": "Content-Type", "value": "application/json"})
    item = {"name": name, "request": {"method": method, "header": h,
                                      "url": {"raw": "{{baseUrl}}" + path, "host": ["{{baseUrl}}"],
                                              "path": [p for p in path.split("?")[0].split("/") if p],
                                              **({"query": [{"key": k, "value": v} for k, v in
                                                            (q.split("=", 1) for q in path.split("?")[1].split("&"))]}
                                                 if "?" in path else {})}},
            "event": [{"listen": "test", "script": {"type": "text/javascript", "exec": tests.strip().splitlines()}}]}
    if body is not None:
        item["request"]["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2)}
    if prerequest:
        item["event"].append({"listen": "prerequest",
                              "script": {"type": "text/javascript", "exec": prerequest.strip().splitlines()}})
    return item


def token(persona, var, agent=None):
    body = {"persona": persona, **({"agent_id": agent} if agent else {})}
    return req(f"Token: {persona}{' via ' + agent if agent else ''}", "POST", "/dev/token", f"""
pm.test("200 token issued", () => pm.response.to.have.status(200));
pm.collectionVariables.set("{var}", pm.response.json().access_token);""", body=body, auth_var=None)


PROBLEM = """pm.test("problem+json", () => pm.expect(pm.response.headers.get("Content-Type")).to.include("application/problem+json"));
pm.test("has correlation id", () => pm.expect(pm.response.json().correlation_id).to.be.a("string"));"""

folders = [
    {"name": "00 Setup", "item": [
        req("Reset mock upstreams", "POST", "/dev/reset", 'pm.test("200", () => pm.response.to.have.status(200));',
            auth_var=None),
        token("support_agent", "supportToken"), token("support_lead", "leadToken"),
        token("finance_analyst", "financeToken"), token("platform_admin", "adminToken"),
        token("support_agent", "agentToken", agent="support-copilot"),
    ]},
    {"name": "01 Customers", "item": [
        req("Customer exists (ARR redacted for support)", "GET", "/v1/customers/ACC-007", """
pm.test("200", () => pm.response.to.have.status(200));
const b = pm.response.json();
pm.test("aggregated entitlement + usage", () => { pm.expect(b.support_tier).to.eql("Platinum"); pm.expect(b.usage.protected_tb).to.eql(412.5); });
pm.test("ARR redacted", () => { pm.expect(b).to.not.have.property("arr_usd"); pm.expect(b.redacted_fields).to.include("arr_usd"); });
pm.test("rate limit headers", () => pm.expect(pm.response.headers.has("RateLimit-Remaining")).to.be.true);"""),
        req("Customer as Finance (ARR visible)", "GET", "/v1/customers/ACC-007", """
pm.test("ARR visible", () => pm.expect(pm.response.json().arr_usd).to.eql(480000));""", auth_var="financeToken"),
        req("Customer not found -> 404", "GET", "/v1/customers/ACC-999",
            'pm.test("404", () => pm.response.to.have.status(404));\n' + PROBLEM),
        req("No token -> 401", "GET", "/v1/customers/ACC-007",
            'pm.test("401", () => pm.response.to.have.status(401));\n' + PROBLEM, auth_var=None),
        req("Fault: usage warehouse down", "POST", "/dev/upstream-faults",
            'pm.test("200", () => pm.response.to.have.status(200));',
            body={"system": "usage_warehouse", "mode": "down"}, auth_var=None),
        req("Usage down -> degraded 200", "GET", "/v1/customers/ACC-007", """
pm.test("200", () => pm.response.to.have.status(200));
pm.test("degraded", () => { const b = pm.response.json(); pm.expect(b.degraded).to.be.true; pm.expect(b.usage).to.be.null; pm.expect(b.degraded_sources).to.eql(["usage"]); });"""),
        req("Fault: usage warehouse ok", "POST", "/dev/upstream-faults",
            'pm.test("200", () => pm.response.to.have.status(200));',
            body={"system": "usage_warehouse", "mode": "ok"}, auth_var=None),
    ]},
    {"name": "02 Cases search", "item": [
        req("Search P1 open Platinum, page 1", "GET", "/v1/cases?priority=P1&status=open&support_tier=Platinum&limit=3", """
pm.test("200", () => pm.response.to.have.status(200));
const b = pm.response.json();
pm.test("3 results, sorted", () => pm.expect(b.data.map(c => c.id)).to.eql(["CS-10231","CS-10245","CS-10238"]));
pm.test("has cursor", () => pm.expect(b.page.has_more).to.be.true);
pm.collectionVariables.set("nextCursor", b.page.next_cursor);"""),
        req("Search page 2 with cursor", "GET",
            "/v1/cases?priority=P1&status=open&support_tier=Platinum&limit=3&cursor={{nextCursor}}", """
pm.test("200", () => pm.response.to.have.status(200));
pm.test("remaining results, no overlap", () => pm.expect(pm.response.json().data.map(c => c.id)).to.eql(["CS-10234","CS-10232","CS-10233"]));"""),
        req("Invalid filter -> 422", "GET", "/v1/cases?priority=P9",
            'pm.test("422", () => pm.response.to.have.status(422));\n' + PROBLEM),
        req("Unknown parameter -> 422", "GET", "/v1/cases?prioirty=P1",
            'pm.test("422", () => pm.response.to.have.status(422));'),
        req("Get case (ETag)", "GET", "/v1/cases/CS-10231", """
pm.test("200", () => pm.response.to.have.status(200));
pm.test("ETag", () => pm.expect(pm.response.headers.get("ETag")).to.eql('"4"'));
pm.collectionVariables.set("caseEtag", pm.response.headers.get("ETag"));"""),
    ]},
    {"name": "03 Comments (idempotency)", "item": [
        req("Add comment", "POST", "/v1/cases/CS-10232/comments", """
pm.test("201", () => pm.response.to.have.status(201));
pm.test("not replayed", () => pm.expect(pm.response.headers.get("Idempotent-Replayed")).to.eql("false"));
pm.collectionVariables.set("commentId", pm.response.json().id);""",
            body={"body": "We are investigating this issue."}, headers={"Idempotency-Key": "{{idemKey}}"},
            prerequest='pm.collectionVariables.set("idemKey", "pm-" + Date.now());'),
        req("Retry same key -> replayed, no duplicate", "POST", "/v1/cases/CS-10232/comments", """
pm.test("201", () => pm.response.to.have.status(201));
pm.test("replayed", () => pm.expect(pm.response.headers.get("Idempotent-Replayed")).to.eql("true"));
pm.test("same comment", () => pm.expect(pm.response.json().id).to.eql(pm.collectionVariables.get("commentId")));""",
            body={"body": "We are investigating this issue."}, headers={"Idempotency-Key": "{{idemKey}}"}),
        req("Same key, different body -> 422", "POST", "/v1/cases/CS-10232/comments",
            'pm.test("422", () => pm.response.to.have.status(422));\n' + PROBLEM,
            body={"body": "Something else."}, headers={"Idempotency-Key": "{{idemKey}}"}),
        req("Missing Idempotency-Key -> 400", "POST", "/v1/cases/CS-10232/comments",
            'pm.test("400", () => pm.response.to.have.status(400));', body={"body": "x"}),
    ]},
    {"name": "04 Escalation", "item": [
        req("Support agent -> 403", "POST", "/v1/cases/CS-10231/escalate",
            'pm.test("403", () => pm.response.to.have.status(403));\n' + PROBLEM,
            body={"reason": "Critical production outage"}),
        req("Agent token cannot exceed user scopes -> 403", "POST", "/v1/cases/CS-10231/escalate",
            'pm.test("403", () => pm.response.to.have.status(403));', body={"reason": "Critical production outage"},
            auth_var="agentToken"),
        req("Closed case -> 409", "POST", "/v1/cases/CS-10236/escalate", """
pm.test("409", () => pm.response.to.have.status(409));
pm.test("case-closed", () => pm.expect(pm.response.json().type).to.include("case-closed"));""",
            body={"reason": "Critical production outage"}, auth_var="leadToken"),
        req("Stale If-Match -> 412", "POST", "/v1/cases/CS-10231/escalate",
            'pm.test("412", () => pm.response.to.have.status(412));', body={"reason": "Critical production outage"},
            headers={"If-Match": '"1"'}, auth_var="leadToken"),
        req("Escalate with If-Match -> 200", "POST", "/v1/cases/CS-10231/escalate", """
pm.test("200", () => pm.response.to.have.status(200));
pm.test("escalated", () => pm.expect(pm.response.json().escalated).to.be.true);""",
            body={"reason": "Critical production outage"}, headers={"If-Match": "{{caseEtag}}"}, auth_var="leadToken"),
        req("Escalate again -> 409", "POST", "/v1/cases/CS-10231/escalate", """
pm.test("409", () => pm.response.to.have.status(409));
pm.test("already escalated", () => pm.expect(pm.response.json().type).to.include("case-already-escalated"));""",
            body={"reason": "Critical production outage"}, auth_var="leadToken"),
        req("Audit trail for CS-10231", "GET", "/v1/audit-logs?resource_id=CS-10231", """
pm.test("200", () => pm.response.to.have.status(200));
const actions = pm.response.json().map(a => a.action + ":" + a.outcome);
pm.test("denied and success recorded", () => { pm.expect(actions).to.include("case.escalate:denied"); pm.expect(actions).to.include("case.escalate:success"); });""",
            auth_var="adminToken"),
        req("Webhook deliveries", "GET", "/v1/webhooks/deliveries?limit=5", """
pm.test("200", () => pm.response.to.have.status(200));
pm.test("delivery recorded", () => pm.expect(pm.response.json().length).to.be.above(0));""", auth_var="adminToken"),
    ]},
    {"name": "05 Webhook receiver", "item": [
        req("Valid signature -> accepted", "POST", "/v1/webhooks/receiver", """
pm.test("200", () => pm.response.to.have.status(200));
pm.test("accepted", () => pm.expect(pm.response.json().status).to.eql("accepted"));""",
            body=None, auth_var=None, headers={"Webhook-Id": "{{whId}}", "Webhook-Timestamp": "{{whTs}}",
                                               "Webhook-Signature": "{{whSig}}", "Content-Type": "application/json"},
            prerequest="""
const id = "evt_pm_" + Date.now();
const ts = Math.floor(Date.now() / 1000).toString();
const body = JSON.stringify({id: id, event: "case.escalated", case_id: "CS-10231", customer_id: "ACC-007", priority: "P1", timestamp: new Date().toISOString()});
const sig = CryptoJS.HmacSHA256(id + "." + ts + "." + body, pm.environment.get("webhookSecret")).toString(CryptoJS.enc.Hex);
pm.collectionVariables.set("whId", id); pm.collectionVariables.set("whTs", ts);
pm.collectionVariables.set("whSig", "v1=" + sig); pm.collectionVariables.set("whBody", body);"""),
        req("Invalid signature -> 401", "POST", "/v1/webhooks/receiver",
            'pm.test("401", () => pm.response.to.have.status(401));\n' + PROBLEM, auth_var=None,
            headers={"Webhook-Id": "{{whId}}", "Webhook-Timestamp": "{{whTs}}",
                     "Webhook-Signature": "v1=deadbeef", "Content-Type": "application/json"}),
    ]},
]
# The receiver requests send the exact signed body.
for item in folders[5]["item"]:
    item["request"]["body"] = {"mode": "raw", "raw": "{{whBody}}"}

collection = {
    "info": {"_postman_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "entitlement-platform")),
             "name": "Customer Entitlement Platform", "description": "Functional + regression checks. Run in order "
             "(Collection Runner or `newman run postman/collection.json -e postman/environment.json`). "
             "Start the API first: docker compose up (or uvicorn src.main:app).",
             "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
    "item": folders,
    "variable": [{"key": k, "value": ""} for k in ("supportToken", "leadToken", "financeToken", "adminToken",
                                                   "agentToken", "nextCursor", "caseEtag", "idemKey", "commentId",
                                                   "whId", "whTs", "whSig", "whBody")],
}
environment = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "entitlement-platform-env")), "name": "Local",
               "values": [{"key": "baseUrl", "value": "http://localhost:8000", "enabled": True},
                          {"key": "webhookSecret", "value": "whsec_dev_only_change_me_0123456789", "enabled": True}],
               "_postman_variable_scope": "environment"}

OUT.mkdir(exist_ok=True)
(OUT / "collection.json").write_text(json.dumps(collection, indent=2), encoding="utf-8")
(OUT / "environment.json").write_text(json.dumps(environment, indent=2), encoding="utf-8")
print(f"wrote {sum(len(f['item']) for f in folders)} requests")
