"""Platform domain models: the shape the platform reasons about, independent of upstream formats."""
from datetime import date, datetime

from pydantic import BaseModel


class Comment(BaseModel):
    id: str
    case_id: str | None = None
    author_id: str
    body: str
    created_at: datetime


class Case(BaseModel):
    id: str
    customer_id: str
    priority: str
    status: str
    subject: str
    opened_at: datetime
    updated_at: datetime
    escalated: bool
    escalation_reason: str | None = None
    escalated_at: datetime | None = None
    version: int
    comment_count: int = 0
    latest_comment: Comment | None = None

    @property
    def etag(self) -> str:
        return f'"{self.version}"'


class Entitlement(BaseModel):
    account_id: str
    name: str
    support_tier: str
    contract_start: date
    contract_end: date
    arr_usd: int


class Usage(BaseModel):
    protected_tb: float
    api_calls: int
    active_users: int
    as_of: date
