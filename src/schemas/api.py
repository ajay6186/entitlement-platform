"""Request/response schemas of the public REST API (mirrors openapi.yaml)."""
from datetime import UTC, date, datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

# SQLite hands back naive datetimes; everything stored is UTC, so make that explicit on the wire.
UtcDatetime = Annotated[datetime, AfterValidator(lambda d: d if d.tzinfo else d.replace(tzinfo=UTC))]


class Priority(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class CaseStatus(str, Enum):
    new = "new"
    open = "open"
    pending = "pending"
    closed = "closed"


class SupportTier(str, Enum):
    Platinum = "Platinum"
    Gold = "Gold"
    Silver = "Silver"


class CaseSort(str, Enum):
    opened_at_asc = "opened_at"
    opened_at_desc = "-opened_at"


# ---- customers ------------------------------------------------------------------------------
class UsageOut(BaseModel):
    protected_tb: float
    api_calls: int
    active_users: int
    as_of: date


class CustomerOut(BaseModel):
    customer_id: str
    name: str
    support_tier: SupportTier
    contract_start: date
    renewal_date: date
    days_until_renewal: int
    arr_usd: int | None = Field(None, description="Present only with the customers:financials scope.")
    usage: UsageOut | None
    degraded: bool
    degraded_sources: list[str] = []
    redacted_fields: list[str] = []


# ---- cases ----------------------------------------------------------------------------------
class CommentOut(BaseModel):
    id: str
    case_id: str
    author_id: str
    body: str
    created_at: datetime


class CaseOut(BaseModel):
    id: str
    customer_id: str
    priority: Priority
    status: CaseStatus
    subject: str
    opened_at: datetime
    updated_at: datetime
    escalated: bool
    escalation_reason: str | None = None
    escalated_at: datetime | None = None
    version: int
    comment_count: int
    latest_comment: CommentOut | None = None


class PageInfo(BaseModel):
    limit: int
    next_cursor: str | None
    has_more: bool


class CaseList(BaseModel):
    data: list[CaseOut]
    page: PageInfo


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(..., min_length=1, max_length=5000)

    @field_validator("body")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must not be blank")
        return v.strip()


class EscalationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=3, max_length=500)


# ---- webhooks / audit -----------------------------------------------------------------------
SubscribableEvent = Literal["case.escalated", "case.comment_added"]


class SubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint_url: str = Field(..., pattern=r"^https?://", max_length=1024)
    event_types: list[SubscribableEvent] = Field(default_factory=lambda: ["case.escalated"], min_length=1)
    secret_ref: str = Field("env:PLATFORM_WEBHOOK_SIGNING_SECRET", max_length=256)


class SubscriptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    event_types: list[SubscribableEvent] | None = Field(None, min_length=1)


class SubscriptionOut(BaseModel):
    id: str
    endpoint_url: str
    event_types: list[str]
    secret_ref: str
    enabled: bool
    created_at: UtcDatetime


class DeliveryOut(BaseModel):
    id: int
    event_id: str
    subscription_id: str
    status: str
    attempts: int
    last_status_code: int | None
    last_error: str | None
    updated_at: UtcDatetime


class AttemptOut(BaseModel):
    attempt_no: int
    trigger: str
    attempted_at: UtcDatetime
    request_headers: dict[str, str]
    status_code: int | None
    error: str | None
    response_body: str | None
    duration_ms: int


class DeliveryDetailOut(DeliveryOut):
    endpoint_url: str | None
    attempt_log: list[AttemptOut]


class EventOut(BaseModel):
    id: str
    event_type: str
    resource_id: str
    created_at: UtcDatetime
    status: Literal["pending", "delivered", "failed", "no_subscribers"]
    deliveries: list[DeliveryOut]


class EventDetailOut(EventOut):
    payload: dict
    deliveries: list[DeliveryDetailOut]


class WebhookStatsOut(BaseModel):
    events_total: int
    events_by_type: dict[str, int]
    deliveries: dict[str, int]  # pending / delivered / failed
    attempts_total: int
    failed_attempts: int
    subscriptions_enabled: int
    subscriptions_total: int
    last_event_at: UtcDatetime | None


class WebhookTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subscription_id: str | None = Field(None, max_length=40, description="Omit to send to every enabled subscription.")


class AuditOut(BaseModel):
    id: int
    occurred_at: UtcDatetime
    correlation_id: str | None
    actor_id: str
    agent_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    detail: dict | None
