"""MCP evaluation scenarios (see TESTEVALS.md), exercised through a real MCP client<->server session.

The platform API runs in-process; the MCP server talks to it over HTTP (ASGI transport) with the user's token,
exactly as in production, so authorization, redaction and business rules are the API's, not mocks.
"""
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_server.api_client import PlatformClient
from mcp_server.server import Runtime, create_server
from mcp_server.guardrails import AuditLog
from src.config import Settings
from src.core.security import issue_persona_token
from src.main import create_app

SAMPLE_MATCHES = {"CS-10231", "CS-10234", "CS-10238"}  # Platinum, P1, open > 48h, renewal <= 90 days


@asynccontextmanager
async def mcp_session(tmp_path, persona="support_lead", elicitation=None, fault=None):
    app = create_app(Settings(database_url=f"sqlite+aiosqlite:///{(tmp_path / 'mcp.db').as_posix()}"))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        app.state.dispatcher.http = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            if fault:
                await http.post("/dev/upstream-faults", json=fault)
            api = PlatformClient(http, issue_persona_token(persona, agent_id="support-copilot"))
            rt = Runtime(api=api, audit=AuditLog(str(tmp_path / "mcp_audit.jsonl")))
            kwargs = {"elicitation_callback": elicitation} if elicitation else {}
            async with create_connected_server_and_client_session(create_server(rt), **kwargs) as session:
                yield session, app, rt


async def call(session, name, args) -> tuple[dict, bool]:
    result = await session.call_tool(name, args)
    return json.loads(result.content[0].text), bool(result.isError)


def approve(answer: bool):
    async def _cb(context, params):
        return types.ElicitResult(action="accept", content={"decision": "approve" if answer else "reject"})
    return _cb


async def answer_sample_question(session) -> list[dict]:
    """The workflow an LLM follows for the sample question, expressed as tool calls."""
    found, _ = await call(session, "search_cases", {"priority": "P1", "status": "open", "support_tier": "Platinum",
                                                    "min_age_hours": 48, "limit": 25})
    rows = []
    for customer_id in sorted({c["customer_id"] for c in found["cases"]}):
        summary, _ = await call(session, "get_customer_summary", {"customer_id": customer_id})
        if summary["days_until_renewal"] <= 90:
            rows += [{**c, "customer": summary["name"], "renewal_date": summary["renewal_date"]}
                     for c in found["cases"] if c["customer_id"] == customer_id]
    return rows


async def test_tools_listed_with_strict_schemas(tmp_path):
    async with mcp_session(tmp_path) as (session, _, _):
        listed = (await session.list_tools()).tools
        assert {t.name for t in listed} == {"search_cases", "get_customer_summary", "add_case_comment", "escalate_case"}
        assert all(t.inputSchema.get("additionalProperties") is False for t in listed)
        escalate = next(t for t in listed if t.name == "escalate_case")
        assert escalate.annotations.destructiveHint is True


async def test_eval_find_platinum_p1_cases_returns_only_matches(tmp_path):
    async with mcp_session(tmp_path) as (session, _, _):
        found, _ = await call(session, "search_cases", {"priority": "P1", "status": "open", "support_tier": "Platinum"})
        assert {c["case_id"] for c in found["cases"]} == {"CS-10231", "CS-10245", "CS-10238", "CS-10234",
                                                          "CS-10232", "CS-10233"}
        assert all(c["priority"] == "P1" and c["status"] == "open" for c in found["cases"])


async def test_eval_sample_question_end_to_end(tmp_path):
    async with mcp_session(tmp_path) as (session, _, _):
        rows = await answer_sample_question(session)
        assert {r["case_id"] for r in rows} == SAMPLE_MATCHES


async def test_eval_case_younger_than_48h_excluded(tmp_path):
    async with mcp_session(tmp_path) as (session, _, _):
        found, _ = await call(session, "search_cases", {"priority": "P1", "status": "open", "min_age_hours": 48})
        assert "CS-10233" not in {c["case_id"] for c in found["cases"]}  # opened 8h ago
        assert all(c["age_hours"] > 48 for c in found["cases"])


async def test_eval_renewal_outside_90_days_excluded(tmp_path):
    async with mcp_session(tmp_path) as (session, _, _):
        globex, _ = await call(session, "get_customer_summary", {"customer_id": "ACC-012"})
        assert globex["days_until_renewal"] > 90
        rows = await answer_sample_question(session)
        assert not {"CS-10232", "CS-10245"} & {r["case_id"] for r in rows}


async def test_eval_usage_unavailable_is_explained(tmp_path):
    async with mcp_session(tmp_path, fault={"system": "usage_warehouse", "mode": "down"}) as (session, _, _):
        summary, is_error = await call(session, "get_customer_summary", {"customer_id": "ACC-007"})
        assert not is_error and summary["degraded"] is True and summary["usage"] is None
        assert any("unavailable" in n.lower() for n in summary["data_notes"])
        assert summary["renewal_date"] == "2027-01-15"  # entitlement data still answered


async def test_eval_unauthorized_arr_is_redacted(tmp_path):
    async with mcp_session(tmp_path, persona="support_agent") as (session, _, _):
        summary, _ = await call(session, "get_customer_summary", {"customer_id": "ACC-007"})
        assert "arr_usd" not in summary and any("ARR is withheld" in n for n in summary["data_notes"])
    async with mcp_session(tmp_path / "fin", persona="finance_analyst") as (session, _, _):
        summary, _ = await call(session, "get_customer_summary", {"customer_id": "ACC-007"})
        assert summary["arr_usd"] == 480000


async def test_eval_prompt_injection_in_comment_is_marked_and_not_executed(tmp_path):
    async with mcp_session(tmp_path) as (session, app, rt):
        found, _ = await call(session, "search_cases", {"customer_id": "ACC-033", "status": "open"})
        case = next(c for c in found["cases"] if c["case_id"] == "CS-10234")
        comment = case["latest_comment"]
        assert comment["untrusted"] is True and comment["suspected_prompt_injection"] is True
        assert "[email redacted]" in comment["text"] and "[phone redacted]" in comment["text"]
        assert "jane.doe" not in json.dumps(found)
        assert "warning" in found
        # Reading content has no side effects: nothing was escalated.
        mock = app.state.mock_upstream.state.mock
        assert not any(c["isEscalated"] for cid, c in mock.cases.items() if cid != "CS-10241")


async def test_eval_escalation_without_confirmation_not_executed(tmp_path):
    async with mcp_session(tmp_path) as (session, app, _):
        args = {"case_id": "CS-10231", "reason_category": "production_outage",
                "reason_details": "Backups failing on node 3 for 30 days"}
        result, is_error = await call(session, "escalate_case", args)
        assert not is_error and result["status"] == "confirmation_required" and result["executed"] is False
        assert app.state.mock_upstream.state.mock.cases["CS-10231"]["isEscalated"] is False
        forged, is_error = await call(session, "escalate_case", {**args, "confirmation_token": "abc.def"})
        assert is_error and forged["error"] == "invalid_confirmation"


async def test_eval_escalation_after_token_confirmation_executes_once(tmp_path):
    async with mcp_session(tmp_path) as (session, app, _):
        args = {"case_id": "CS-10231", "reason_category": "production_outage",
                "reason_details": "Backups failing on node 3 for 30 days"}
        pending, _ = await call(session, "escalate_case", args)
        done, is_error = await call(session, "escalate_case", {**args, "confirmation_token": pending["confirmation_token"]})
        assert not is_error and done["status"] == "escalated"
        assert app.state.mock_upstream.state.mock.cases["CS-10231"]["isEscalated"] is True
        reused, is_error = await call(session, "escalate_case", {**args, "confirmation_token": pending["confirmation_token"]})
        assert is_error  # single use
        tampered, is_error = await call(session, "escalate_case", {**args, "reason_details": "Something else entirely",
                                                                   "confirmation_token": pending["confirmation_token"]})
        assert is_error


async def test_eval_escalation_with_elicitation_accept_and_decline(tmp_path):
    args = {"case_id": "CS-10238", "reason_category": "sla_breach_risk",
            "reason_details": "Snapshots skipped two nights in a row"}
    async with mcp_session(tmp_path / "no", elicitation=approve(False)) as (session, app, _):
        result, _ = await call(session, "escalate_case", args)
        assert result["status"] == "not_executed"
        assert app.state.mock_upstream.state.mock.cases["CS-10238"]["isEscalated"] is False
    async with mcp_session(tmp_path / "yes", elicitation=approve(True)) as (session, app, _):
        result, _ = await call(session, "escalate_case", args)
        assert result["status"] == "escalated"


async def test_eval_confirmed_escalation_still_enforces_backend_rules(tmp_path):
    async with mcp_session(tmp_path, persona="support_agent", elicitation=approve(True)) as (session, app, _):
        result, is_error = await call(session, "escalate_case", {
            "case_id": "CS-10231", "reason_category": "production_outage", "reason_details": "Backups failing badly"})
        assert is_error and result["error"] == "forbidden"
    async with mcp_session(tmp_path / "lead", elicitation=approve(True)) as (session, app, _):
        result, is_error = await call(session, "escalate_case", {
            "case_id": "CS-10236", "reason_category": "production_outage", "reason_details": "Closed case attempt"})
        assert is_error and result["error"] == "case_closed"


async def test_strict_schema_rejects_unknown_or_invalid_arguments(tmp_path):
    async with mcp_session(tmp_path) as (session, _, _):
        r = await session.call_tool("search_cases", {"priority": "urgent"})
        assert r.isError
        r = await session.call_tool("get_customer_summary", {"customer_id": "ACC-007", "include_arr": True})
        assert r.isError


async def test_comment_tool_is_retry_safe_and_every_call_audited(tmp_path):
    async with mcp_session(tmp_path) as (session, app, rt):
        args = {"case_id": "CS-10232", "comment": "Engineering is investigating the restore hang."}
        first, _ = await call(session, "add_case_comment", args)
        second, _ = await call(session, "add_case_comment", args)
        assert first["status"] == "added" and second["status"] == "already_added"
        assert first["comment_id"] == second["comment_id"]
        assert len(app.state.mock_upstream.state.mock.cases["CS-10232"]["comments"]) == 1
        lines = [json.loads(x) for x in rt.audit.path.read_text().splitlines()]
        assert [x["tool"] for x in lines] == ["add_case_comment", "add_case_comment"]
        assert all(x["actor_id"] == "u-support-lead-01" and x["agent_id"] == "support-copilot" for x in lines)
        assert "comment" not in lines[0]["arguments"]  # free text is not copied into the audit log
