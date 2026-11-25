"""Case business rules: search (with cross-system tier filter + keyset pagination), comments, escalation."""
import base64
import hashlib
import json
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.clients.base import UpstreamConflict, UpstreamUnavailable
from src.clients.case_client import CaseClient
from src.clients.entitlement_client import EntitlementClient
from src.core.clock import wall_now
from src.core.errors import ProblemException, conflict, forbidden, not_found, upstream_unavailable
from src.core.security import Principal
from src.domain import Case, Comment
from src.models.tables import DomainEvent
from src.services.audit_service import record_audit
from src.services.webhook_service import enqueue_event


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def _unb64(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def invalid_cursor(detail: str) -> ProblemException:
    return ProblemException(400, "invalid-cursor", "Invalid pagination cursor", detail)


class CaseService:
    def __init__(self, cases: CaseClient, entitlements: EntitlementClient):
        self.cases, self.entitlements = cases, entitlements

    # ---- search -------------------------------------------------------------------------------
    async def search(self, *, priority: list[str], status: list[str], customer_id: str | None,
                     support_tier: list[str], opened_before: datetime | None, sort: str, limit: int,
                     cursor: str | None) -> tuple[list[Case], str | None]:
        filters = {"priority": sorted(priority), "status": sorted(status), "customer_id": customer_id,
                   "support_tier": sorted(support_tier),
                   "opened_before": opened_before.isoformat() if opened_before else None, "sort": sort}
        # The cursor is bound to the query, so it cannot be replayed against different filters.
        query_hash = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:16]

        after = None
        if cursor:
            try:
                decoded = _unb64(cursor)
                after = (datetime.fromisoformat(decoded["o"]), decoded["i"])
            except Exception:
                raise invalid_cursor("The cursor is malformed.")
            if decoded.get("q") != query_hash:
                raise invalid_cursor("The cursor belongs to a different query; restart without a cursor.")

        try:
            cases = await self.cases.search(priority=priority, status=status, customer_id=customer_id,
                                            opened_before=opened_before)
            if support_tier:  # cross-system filter: tier lives in the entitlement system
                ents = await self.entitlements.get_many([c.customer_id for c in cases])
                cases = [c for c in cases if c.customer_id in ents and ents[c.customer_id].support_tier in support_tier]
        except UpstreamUnavailable as exc:
            raise upstream_unavailable(exc.system.replace("_", " "))

        descending = sort == "-opened_at"
        cases.sort(key=lambda c: (c.opened_at, c.id), reverse=descending)  # deterministic, id tie-break
        if after:
            cases = [c for c in cases if ((c.opened_at, c.id) < after if descending else (c.opened_at, c.id) > after)]
        page = cases[:limit]
        next_cursor = None
        if len(cases) > limit:
            last = page[-1]
            next_cursor = _b64({"o": last.opened_at.isoformat(), "i": last.id, "q": query_hash})
        return page, next_cursor

    async def get(self, case_id: str) -> Case:
        try:
            case = await self.cases.get(case_id)
        except UpstreamUnavailable:
            raise upstream_unavailable("case system")
        if case is None:
            raise not_found("Case", case_id)
        return case

    # ---- comments -----------------------------------------------------------------------------
    async def add_comment(self, session: AsyncSession, principal: Principal, case_id: str, body: str,
                          idempotency_key: str) -> tuple[Comment, DomainEvent]:
        try:
            comment = await self.cases.add_comment(case_id, body, principal.sub,
                                                   idempotency_key=f"{principal.sub}:{idempotency_key}")
        except UpstreamUnavailable:
            raise upstream_unavailable("case system")
        if comment is None:
            raise not_found("Case", case_id)
        record_audit(session, principal, action="case.comment.create", resource_type="case", resource_id=case_id,
                     outcome="success", detail={"comment_id": comment.id, "length": len(body)})
        # The comment text is customer-facing content: the event carries ids only; receivers fetch details.
        event = await enqueue_event(session, "case.comment_added", case_id, {
            "case_id": case_id, "comment_id": comment.id, "author_id": comment.author_id,
            "timestamp": wall_now().strftime("%Y-%m-%dT%H:%M:%SZ")})
        return comment, event  # the caller commits audit + outbox event with its idempotency record

    # ---- escalation ---------------------------------------------------------------------------
    async def escalate(self, session: AsyncSession, principal: Principal, case_id: str, reason: str,
                       if_match: str | None) -> tuple[Case, DomainEvent]:
        async def deny(outcome: str, exc: ProblemException, why: str):
            record_audit(session, principal, action="case.escalate", resource_type="case", resource_id=case_id,
                         outcome=outcome, detail={"reason": why})
            await session.commit()
            raise exc

        if not principal.has("cases:escalate"):
            await deny("denied", forbidden("Escalation requires the 'cases:escalate' scope."), "missing_scope")

        case = await self.get(case_id)
        if if_match and if_match.strip() not in (case.etag, "*"):
            await deny("denied", ProblemException(
                412, "precondition-failed", "Precondition failed",
                f"The case has changed (current ETag {case.etag}). Re-read it and retry.",
                current_etag=case.etag), "stale_etag")
        if case.status == "closed":
            await deny("denied", conflict("case-closed", "Case is closed", "A closed case cannot be escalated."),
                       "case_closed")
        if case.escalated:
            await deny("denied", conflict("case-already-escalated", "Case already escalated",
                                          "This case has already been escalated."), "already_escalated")

        try:  # optimistic concurrency against the system of record
            updated = await self.cases.escalate(case_id, case.version, reason)
        except UpstreamConflict:
            await deny("failed", conflict("concurrent-modification", "Concurrent modification",
                                          "The case was modified concurrently. Re-read it and retry."),
                       "version_conflict")
        except UpstreamUnavailable:
            await deny("failed", upstream_unavailable("case system"), "upstream_unavailable")

        record_audit(session, principal, action="case.escalate", resource_type="case", resource_id=case_id,
                     outcome="success", detail={"reason": reason, "from_version": case.version,
                                                "to_version": updated.version})
        event = await enqueue_event(session, "case.escalated", case_id, {
            "case_id": case_id, "customer_id": updated.customer_id, "priority": updated.priority,
            "reason": reason, "timestamp": wall_now().strftime("%Y-%m-%dT%H:%M:%SZ")})
        await session.commit()  # audit + outbox event + deliveries in one transaction
        return updated, event
