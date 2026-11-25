"""Audit read API, health, and dev-only helpers (token minting, upstream fault injection)."""
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ProblemException
from src.core.security import PERSONAS, Principal, get_principal, issue_persona_token, require
from src.models.database import get_session
from src.models.tables import AuditLog
from src.schemas.api import AuditOut

audit_router = APIRouter(prefix="/v1/audit-logs", tags=["audit"])
health_router = APIRouter(tags=["health"])
dev_router = APIRouter(prefix="/dev", tags=["dev"])


@audit_router.get("", response_model=list[AuditOut])
async def list_audit(resource_id: str | None = Query(None, max_length=128),
                     actor_id: str | None = Query(None, max_length=128),
                     action: str | None = Query(None, max_length=64),
                     limit: int = Query(50, ge=1, le=500),
                     principal: Principal = Depends(get_principal),
                     session: AsyncSession = Depends(get_session)):
    require(principal, "audit:read")
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if resource_id:
        q = q.where(AuditLog.resource_id == resource_id)
    if actor_id:
        q = q.where(AuditLog.actor_id == actor_id)
    if action:
        q = q.where(AuditLog.action == action)
    return (await session.execute(q)).scalars().all()


@health_router.get("/healthz")
async def healthz(request: Request):
    breakers = request.app.state.container.breakers
    return {"status": "ok", "upstreams": {name: b.state for name, b in breakers.items()}}


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona: Literal["support_agent", "support_lead", "finance_analyst", "platform_admin"]
    agent_id: str | None = Field(None, pattern=r"^[a-z0-9-]{3,64}$")


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system: Literal["case_system", "entitlement_system", "usage_warehouse"]
    mode: Literal["ok", "down", "slow", "flaky"]


@dev_router.post("/token")
async def mint_token(req: TokenRequest):
    """DEV ONLY: stands in for the IdP's OAuth token endpoint."""
    p = PERSONAS[req.persona]
    return {"access_token": issue_persona_token(req.persona, agent_id=req.agent_id), "token_type": "Bearer",
            "expires_in": 3600, "sub": p["sub"], "scope": " ".join(p["scopes"])}


def _mock(request: Request):
    """The in-process mock's state, or None when the mock upstreams run as a separate service."""
    mock = getattr(request.app.state, "mock_upstream", None)
    return mock.state.mock if mock is not None else None


async def _remote_mock_admin(request: Request, path: str, body: dict | None = None) -> dict:
    """Forward a dev command to a standalone mock upstream service (e.g. the `upstreams` container)."""
    http: httpx.AsyncClient = request.app.state.container.upstream_http
    try:
        r = await http.post(path, json=body)
    except httpx.HTTPError as exc:
        raise ProblemException(502, "mock-admin-unreachable", "Mock upstream admin unreachable",
                               f"Could not reach the mock upstream service: {type(exc).__name__}.")
    if r.status_code == 404:
        raise ProblemException(409, "mock-not-in-use", "Mock upstreams not in use",
                               "Fault injection only works with the mock upstreams (in-process or standalone).")
    if r.status_code >= 400:
        raise ProblemException(502, "mock-admin-failed", "Mock upstream admin call failed",
                               f"The mock upstream service answered {r.status_code}.")
    return r.json()


@dev_router.post("/upstream-faults")
async def set_fault(req: FaultRequest, request: Request):
    """DEV ONLY: make an upstream go down/slow/flaky to demonstrate degraded responses."""
    state = _mock(request)
    if state is not None:
        state.faults[req.system] = req.mode
        faults = state.faults
    else:
        faults = (await _remote_mock_admin(request, "/_admin/faults", req.model_dump()))["faults"]
    if req.mode == "ok":
        request.app.state.container.breakers[req.system].record_success()
    return {"faults": faults}


@dev_router.post("/reset")
async def reset(request: Request):
    """DEV ONLY: restore mock upstream data and faults, close breakers, clear rate limits."""
    state = _mock(request)
    if state is not None:
        state.reset()
    else:
        await _remote_mock_admin(request, "/_admin/reset")
    for b in request.app.state.container.breakers.values():
        b.record_success()
    request.app.state.rate_limiter.reset()
    return {"reset": True}
