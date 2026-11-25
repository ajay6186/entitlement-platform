"""Case system (Salesforce-like) client: maps camelCase records to platform domain models."""
from datetime import datetime

from src.clients.base import BaseUpstreamClient, UpstreamConflict, UpstreamUnavailable
from src.domain import Case, Comment


def _comment(raw: dict, case_id: str | None = None) -> Comment:
    return Comment(id=raw["id"], case_id=raw.get("caseId", case_id), author_id=raw["authorId"],
                   body=raw["body"], created_at=raw["createdAt"])


def _case(raw: dict) -> Case:
    latest = raw.get("latestComment")
    return Case(id=raw["id"], customer_id=raw["accountId"], priority=raw["priority"], status=raw["status"],
                subject=raw["subject"], opened_at=raw["openedAt"], updated_at=raw["lastModifiedAt"],
                escalated=raw.get("isEscalated", False), escalation_reason=raw.get("escalationReason"),
                escalated_at=raw.get("escalatedAt"), version=raw["version"],
                comment_count=raw.get("commentCount", 0),
                latest_comment=_comment(latest, raw["id"]) if latest else None)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class CaseClient(BaseUpstreamClient):
    system = "case_system"
    display_name = "case system"

    async def search(self, *, priority: list[str] | None = None, status: list[str] | None = None,
                     customer_id: str | None = None, opened_before: datetime | None = None) -> list[Case]:
        params = {}
        if priority:
            params["priority"] = ",".join(priority)
        if status:
            params["status"] = ",".join(status)
        if customer_id:
            params["accountId"] = customer_id
        if opened_before:
            params["openedBefore"] = _iso(opened_before)
        resp = await self.request("GET", "/case-system/cases", params=params)
        resp.raise_for_status()
        return [_case(r) for r in resp.json()["records"]]

    async def get(self, case_id: str) -> Case | None:
        resp = await self.request("GET", f"/case-system/cases/{case_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _case(resp.json())

    async def add_comment(self, case_id: str, body: str, author_id: str, idempotency_key: str) -> Comment | None:
        # Safe to retry because the upstream de-duplicates on Idempotency-Key.
        resp = await self.request("POST", f"/case-system/cases/{case_id}/comments",
                                  json={"body": body, "authorId": author_id},
                                  headers={"Idempotency-Key": idempotency_key})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _comment(resp.json(), case_id)

    async def escalate(self, case_id: str, expected_version: int, reason: str) -> Case:
        # Not retried: a retry after an applied-but-unacknowledged write would surface as a false conflict.
        resp = await self.request("PATCH", f"/case-system/cases/{case_id}", retry=False,
                                  json={"expectedVersion": expected_version, "isEscalated": True,
                                        "escalationReason": reason})
        if resp.status_code == 409:
            raise UpstreamConflict(self.system, "version conflict")
        if resp.status_code >= 400:
            raise UpstreamUnavailable(self.system, f"HTTP {resp.status_code}")
        return _case(resp.json())
