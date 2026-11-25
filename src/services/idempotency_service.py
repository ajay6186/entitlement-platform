"""Idempotency-Key handling for retriable writes.

Semantics (aligned with the IETF Idempotency-Key header draft):
- same key + same request          -> replay the stored response (Idempotent-Replayed: true), no new write
- same key + different request     -> 422 idempotency-key-reused
- same key while first is running  -> 409 idempotency-request-in-progress
- first attempt fails with 5xx      -> record removed so the client may retry with the same key
Keys are scoped per client (token subject) and expire after PLATFORM_IDEMPOTENCY_TTL_HOURS.
"""
import hashlib
import json
import re
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.core.clock import wall_now
from src.core.errors import ProblemException
from src.models.tables import IdempotencyRecord
from src.services.audit_service import as_utc

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-:.]{8,255}$")


def validate_key(key: str | None) -> str:
    if not key:
        raise ProblemException(400, "idempotency-key-required", "Idempotency-Key header required",
                               "Retriable writes must send a unique Idempotency-Key header (e.g. a UUID).")
    if not KEY_PATTERN.match(key):
        raise ProblemException(400, "idempotency-key-invalid", "Invalid Idempotency-Key",
                               "Idempotency-Key must be 8-255 characters of [A-Za-z0-9_-:.].")
    return key


def fingerprint(method: str, path: str, body: dict) -> str:
    canonical = json.dumps({"method": method, "path": path, "body": body}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def begin(session: AsyncSession, *, client_id: str, key: str, method: str, path: str,
                request_hash: str) -> tuple[str, IdempotencyRecord]:
    """Returns ("new", record) when the caller should execute, or ("replay", record) to return the stored result."""
    now = wall_now()
    existing = (await session.execute(select(IdempotencyRecord).where(
        IdempotencyRecord.client_id == client_id, IdempotencyRecord.idempotency_key == key))).scalar_one_or_none()
    if existing and as_utc(existing.expires_at) <= now:
        await session.delete(existing)
        await session.commit()
        existing = None
    if existing:
        if existing.request_hash != request_hash:
            raise ProblemException(422, "idempotency-key-reused", "Idempotency-Key reused with a different request",
                                   "This Idempotency-Key was already used for a different request payload.")
        if existing.state != "completed":
            raise ProblemException(409, "idempotency-request-in-progress", "Request already in progress",
                                   "A request with this Idempotency-Key is still being processed. Retry shortly.",
                                   headers={"Retry-After": "1"})
        return "replay", existing

    record = IdempotencyRecord(client_id=client_id, idempotency_key=key, method=method, path=path,
                               request_hash=request_hash, state="in_progress",
                               expires_at=now + timedelta(hours=get_settings().idempotency_ttl_hours))
    session.add(record)
    try:
        await session.commit()  # the unique constraint is the lock against concurrent duplicates
    except IntegrityError:
        await session.rollback()
        raise ProblemException(409, "idempotency-request-in-progress", "Request already in progress",
                               "A concurrent request with this Idempotency-Key is being processed.",
                               headers={"Retry-After": "1"})
    return "new", record


def complete(record: IdempotencyRecord, status: int, body: dict, location: str | None = None) -> None:
    record.state, record.response_status, record.response_body = "completed", status, body
    record.response_location = location


async def abandon(session: AsyncSession, record: IdempotencyRecord) -> None:
    record_id = record.id  # read before rollback expires the instance
    await session.rollback()
    await session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.id == record_id))
    await session.commit()
