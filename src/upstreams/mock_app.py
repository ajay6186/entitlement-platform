"""Mock systems of record, served over HTTP so the platform's clients exercise real HTTP semantics.

- Case system (Salesforce-like): camelCase JSON, optimistic locking via `version`, honours Idempotency-Key.
- Entitlement system (NetSuite-like): loaded from a CSV export.
- Usage warehouse (Snowflake-like): rows of daily usage metrics.

Fault injection (`POST /_admin/faults`) lets tests, Postman and demos make a system go down, slow or flaky.
The shapes deliberately differ from the platform's API: mapping happens in src/clients (anti-corruption layer).
"""
import asyncio
import copy
import csv
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

SYSTEMS = ("case_system", "entitlement_system", "usage_warehouse")
FaultMode = Literal["ok", "down", "slow", "flaky"]


class MockState:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.reset()

    def reset(self) -> None:
        cases = json.loads((self.data_dir / "cases.json").read_text(encoding="utf-8"))
        self.cases: dict[str, dict] = {c["id"]: c for c in copy.deepcopy(cases)}
        with (self.data_dir / "entitlements.csv").open(newline="", encoding="utf-8") as f:
            self.accounts = {row["account_id"]: row for row in csv.DictReader(f)}
        rows = json.loads((self.data_dir / "usage.json").read_text(encoding="utf-8"))
        self.usage = {r["account_id"]: r for r in rows}
        self.comment_keys: dict[str, dict] = {}
        self.faults: dict[str, str] = {s: "ok" for s in SYSTEMS}
        self.calls: dict[str, int] = {s: 0 for s in SYSTEMS}


class FaultRequest(BaseModel):
    system: Literal["case_system", "entitlement_system", "usage_warehouse"]
    mode: FaultMode


class CommentIn(BaseModel):
    body: str
    authorId: str


class CasePatch(BaseModel):
    expectedVersion: int
    isEscalated: bool
    escalationReason: str


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _public_case(c: dict) -> dict:
    out = {k: v for k, v in c.items() if k != "comments"}
    out["latestComment"] = c["comments"][-1] if c.get("comments") else None
    out["commentCount"] = len(c.get("comments", []))
    return out


def create_mock_upstream_app(data_dir: str = "mock_data") -> FastAPI:
    # Dev-only mock: Swagger UI is on so the standalone `upstreams` service (:9000) can be explored at /docs.
    app = FastAPI(title="Mock upstream systems", version="1.0.0", redoc_url=None, description=(
        "Stand-ins for the systems of record behind the platform API. Payload shapes deliberately differ from the "
        "platform's own API. Use **admin** to inject faults (down / slow / flaky) or reset the mock data."))
    state = MockState(data_dir)
    app.state.mock = state

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/docs")

    @app.middleware("http")
    async def inject_faults(request: Request, call_next):
        system = request.url.path.strip("/").split("/", 1)[0].replace("-", "_")
        if system in state.faults:
            state.calls[system] += 1
            mode = state.faults[system]
            if mode == "down":
                return JSONResponse({"error": "service unavailable"}, status_code=503)
            if mode == "slow":
                await asyncio.sleep(30)
            if mode == "flaky" and state.calls[system] % 2 == 1:
                return JSONResponse({"error": "bad gateway"}, status_code=502)
        return await call_next(request)

    # ---- admin -------------------------------------------------------------------------------
    @app.get("/_admin/health", tags=["admin"])
    async def health():
        return {"status": "ok", "faults": state.faults}

    @app.post("/_admin/faults", tags=["admin"])
    async def set_fault(req: FaultRequest):
        state.faults[req.system] = req.mode
        return {"faults": state.faults}

    @app.post("/_admin/reset", tags=["admin"])
    async def reset():
        state.reset()
        return {"reset": True}

    # ---- case system (Salesforce-like) -------------------------------------------------------
    @app.get("/case-system/cases", tags=["case system (Salesforce-like)"])
    async def list_cases(priority: str | None = None, status: str | None = None,
                         accountId: str | None = None, openedBefore: str | None = None):
        records = list(state.cases.values())
        if priority:
            records = [c for c in records if c["priority"] in priority.split(",")]
        if status:
            records = [c for c in records if c["status"] in status.split(",")]
        if accountId:
            records = [c for c in records if c["accountId"] == accountId]
        if openedBefore:
            records = [c for c in records if c["openedAt"] < openedBefore]
        return {"totalSize": len(records), "records": [_public_case(c) for c in records]}

    @app.get("/case-system/cases/{case_id}", tags=["case system (Salesforce-like)"])
    async def get_case(case_id: str):
        c = state.cases.get(case_id)
        if not c:
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
        return _public_case(c)

    @app.post("/case-system/cases/{case_id}/comments", status_code=201, tags=["case system (Salesforce-like)"])
    async def add_comment(case_id: str, body: CommentIn, idempotency_key: str | None = Header(None)):
        c = state.cases.get(case_id)
        if not c:
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
        if idempotency_key and idempotency_key in state.comment_keys:
            return state.comment_keys[idempotency_key]
        comment = {"id": f"CM-{uuid.uuid4().hex[:10]}", "authorId": body.authorId, "body": body.body,
                   "createdAt": _now_iso()}
        c.setdefault("comments", []).append(comment)
        c["lastModifiedAt"] = comment["createdAt"]
        c["version"] += 1
        result = {"caseId": case_id, **comment}
        if idempotency_key:
            state.comment_keys[idempotency_key] = result
        return result

    @app.patch("/case-system/cases/{case_id}", tags=["case system (Salesforce-like)"])
    async def patch_case(case_id: str, patch: CasePatch):
        c = state.cases.get(case_id)
        if not c:
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
        if c["version"] != patch.expectedVersion:
            return JSONResponse({"error": "VERSION_CONFLICT", "currentVersion": c["version"]}, status_code=409)
        c.update(isEscalated=patch.isEscalated, escalationReason=patch.escalationReason,
                 escalatedAt=_now_iso(), lastModifiedAt=_now_iso(), version=c["version"] + 1)
        return _public_case(c)

    # ---- entitlement system (NetSuite-like) ---------------------------------------------------
    @app.get("/entitlement-system/accounts", tags=["entitlement system (NetSuite-like)"])
    async def list_accounts(ids: str | None = None):
        wanted = set(ids.split(",")) if ids else None
        return {"items": [a for k, a in state.accounts.items() if wanted is None or k in wanted]}

    @app.get("/entitlement-system/accounts/{account_id}", tags=["entitlement system (NetSuite-like)"])
    async def get_account(account_id: str):
        a = state.accounts.get(account_id)
        return a if a else JSONResponse({"error": "RECORD_NOT_FOUND"}, status_code=404)

    # ---- usage warehouse (Snowflake-like) -----------------------------------------------------
    @app.get("/usage-warehouse/usage/{account_id}", tags=["usage warehouse (Snowflake-like)"])
    async def get_usage(account_id: str):
        row = state.usage.get(account_id)
        return {"rowCount": 1 if row else 0, "data": [row] if row else []}

    return app
