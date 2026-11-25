"""Platform-owned tables. Business data (cases, entitlements, usage) stays in the systems of record."""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.core.clock import wall_now
from src.models.database import Base


class IdempotencyRecord(Base):
    """One row per (client, Idempotency-Key). Stores the request fingerprint and the first response."""
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_idempotency_client_key"),
        Index("ix_idempotency_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(512))
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="in_progress")  # in_progress | completed
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=wall_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only record of sensitive actions and of every agent-originated request."""
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_resource_time", "resource_type", "resource_id", "occurred_at"),
        Index("ix_audit_actor_time", "actor_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=wall_now)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(128))
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16))  # success | denied | failed
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DomainEvent(Base):
    """Transactional outbox: the event is written in the same transaction as the audit record."""
    __tablename__ = "domain_events"
    __table_args__ = (Index("ix_events_type_time", "event_type", "created_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # evt_<uuid>
    event_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=wall_now)


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # whs_<uuid>
    endpoint_url: Mapped[str] = mapped_column(String(1024))
    event_types: Mapped[list] = mapped_column(JSON, default=lambda: ["case.escalated"])
    # A *reference* to the signing secret (e.g. env:NAME or vault path), never the secret itself.
    secret_ref: Mapped[str] = mapped_column(String(256))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=wall_now)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "subscription_id", name="uq_delivery_event_subscription"),
        Index("ix_delivery_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.id"))
    subscription_id: Mapped[str] = mapped_column(ForeignKey("webhook_subscriptions.id"))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | delivered | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=wall_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=wall_now, onupdate=wall_now)


class WebhookAttempt(Base):
    """One row per HTTP attempt of a delivery: what was sent (signature headers, never the secret) and what came back."""
    __tablename__ = "webhook_attempts"
    __table_args__ = (Index("ix_attempt_delivery", "delivery_id", "attempt_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("webhook_deliveries.id"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(16), default="auto")  # auto | manual_retry | startup
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=wall_now)
    request_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)  # truncated
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class WebhookInbox(Base):
    """Receiver side: event ids already processed, for replay/duplicate protection."""
    __tablename__ = "webhook_inbox"

    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=wall_now)
