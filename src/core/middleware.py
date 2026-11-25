"""Cross-cutting HTTP concerns: correlation id, access log, rate-limit headers, agent audit trail."""
import logging
import re
import time
import uuid

from fastapi import FastAPI, Request

from src.core.logging import correlation_id_var
from src.services.audit_service import record_audit

log = logging.getLogger("access")
_CID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def install_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("x-correlation-id", "")
        cid = incoming if _CID.match(incoming) else uuid.uuid4().hex  # never trust arbitrary header content
        token = correlation_id_var.set(cid)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Correlation-ID"] = cid
            for name, value in getattr(request.state, "rate_limit", {}).items():
                response.headers.setdefault(name, value)
            return response
        finally:
            principal = getattr(request.state, "principal", None)
            log.info("request", extra={
                "method": request.method, "path": request.url.path, "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "actor_id": principal.sub if principal else None,
                "agent_id": principal.agent_id if principal else None})
            # Every request made by an AI agent is audited (the MCP layer also audits each tool call).
            if principal is not None and principal.agent_id:
                try:
                    async with request.app.state.sessionmaker() as session:
                        outcome = "success" if status < 400 else ("denied" if status in (401, 403) else "failed")
                        record_audit(session, principal, action="agent.api_request", resource_type="http",
                                     resource_id=request.url.path, outcome=outcome,
                                     detail={"method": request.method, "status": status})
                        await session.commit()
                except Exception:  # auditing must not break the response path, but must be visible
                    log.exception("failed to write agent audit record")
            correlation_id_var.reset(token)
