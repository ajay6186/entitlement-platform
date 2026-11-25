"""Audit trail. Rows are added to the caller's session so they commit atomically with the action."""
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import correlation_id_var
from src.core.security import Principal
from src.models.tables import AuditLog


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; everything the platform stores is UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def record_audit(session: AsyncSession, principal: Principal, *, action: str, resource_type: str,
                 resource_id: str | None, outcome: str, detail: dict | None = None) -> AuditLog:
    row = AuditLog(correlation_id=correlation_id_var.get(), actor_id=principal.sub, agent_id=principal.agent_id,
                   action=action, resource_type=resource_type, resource_id=resource_id, outcome=outcome,
                   detail=detail)
    session.add(row)
    return row
