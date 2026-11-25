from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ProblemException
from src.core.security import Principal, get_principal, require
from src.dependencies import Container, get_container
from src.domain import Case
from src.models.database import get_session
from src.schemas.api import (CaseList, CaseOut, CaseSort, CaseStatus, CommentCreate, CommentOut, EscalationCreate,
                             Priority, SupportTier)
from src.services import idempotency_service as idem

router = APIRouter(prefix="/v1/cases", tags=["cases"])
CASE_ID = Path(..., pattern=r"^CS-[0-9]{3,12}$")
SEARCH_PARAMS = {"priority", "status", "customer_id", "support_tier", "opened_before", "sort", "limit", "cursor"}


def _case_json(case: Case) -> dict:
    return CaseOut.model_validate(case.model_dump()).model_dump(mode="json")


@router.get("", response_model=CaseList)
async def search_cases(request: Request,
                       priority: list[Priority] = Query([]),
                       status: list[CaseStatus] = Query([]),
                       customer_id: str | None = Query(None, pattern=r"^ACC-[0-9]{3,10}$"),
                       support_tier: list[SupportTier] = Query([]),
                       opened_before: datetime | None = Query(None),
                       sort: CaseSort = Query(CaseSort.opened_at_asc),
                       limit: int = Query(20, ge=1, le=100),
                       cursor: str | None = Query(None, max_length=512),
                       principal: Principal = Depends(get_principal),
                       c: Container = Depends(get_container)):
    """Search cases. Repeat a filter to OR values (?priority=P1&priority=P2); different filters AND together."""
    require(principal, "cases:read")
    # Unknown parameters are rejected rather than silently ignored (a typo would otherwise widen the result).
    if unknown := sorted(set(request.query_params) - SEARCH_PARAMS):
        raise ProblemException(422, "validation-error", "Request validation failed",
                               f"Unknown query parameter(s): {', '.join(unknown)}.",
                               errors=[{"location": f"query.{u}", "message": "unknown parameter"} for u in unknown])
    if opened_before and opened_before.tzinfo is None:
        opened_before = opened_before.replace(tzinfo=UTC)
    page, next_cursor = await c.case_service.search(
        priority=[p.value for p in priority], status=[s.value for s in status], customer_id=customer_id,
        support_tier=[t.value for t in support_tier], opened_before=opened_before, sort=sort.value,
        limit=limit, cursor=cursor)
    return {"data": [_case_json(x) for x in page],
            "page": {"limit": limit, "next_cursor": next_cursor, "has_more": next_cursor is not None}}


@router.get("/{case_id}", response_model=CaseOut)
async def get_case(case_id: str = CASE_ID, principal: Principal = Depends(get_principal),
                   c: Container = Depends(get_container)):
    require(principal, "cases:read")
    case = await c.case_service.get(case_id)
    return JSONResponse(_case_json(case), headers={"ETag": case.etag})


@router.post("/{case_id}/comments", status_code=201, response_model=CommentOut)
async def add_comment(payload: CommentCreate, request: Request, background: BackgroundTasks,
                      case_id: str = CASE_ID,
                      idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                      principal: Principal = Depends(get_principal),
                      session: AsyncSession = Depends(get_session),
                      c: Container = Depends(get_container)):
    require(principal, "cases:comment")
    key = idem.validate_key(idempotency_key)
    path = f"/v1/cases/{case_id}/comments"
    kind, record = await idem.begin(session, client_id=principal.sub, key=key, method="POST", path=path,
                                    request_hash=idem.fingerprint("POST", path, payload.model_dump()))
    if kind == "replay":
        headers = {"Idempotent-Replayed": "true"}
        if record.response_location:
            headers["Location"] = record.response_location
        return JSONResponse(record.response_body, status_code=record.response_status, headers=headers)
    try:
        comment, event = await c.case_service.add_comment(session, principal, case_id, payload.body, key)
    except Exception:
        await idem.abandon(session, record)  # nothing was created; let the client retry with the same key
        raise
    body = CommentOut.model_validate(comment.model_dump() | {"case_id": case_id}).model_dump(mode="json")
    location = f"{path}/{comment.id}"
    idem.complete(record, 201, body, location)
    await session.commit()  # idempotency result + audit row + outbox event together
    background.add_task(request.app.state.dispatcher.dispatch_event, event.id)
    return JSONResponse(body, status_code=201, headers={"Location": location, "Idempotent-Replayed": "false"},
                        background=background)


@router.post("/{case_id}/escalate", response_model=CaseOut)
async def escalate_case(payload: EscalationCreate, request: Request, background: BackgroundTasks,
                        case_id: str = CASE_ID,
                        if_match: str | None = Header(None, alias="If-Match"),
                        principal: Principal = Depends(get_principal),
                        session: AsyncSession = Depends(get_session),
                        c: Container = Depends(get_container)):
    """Authorization, state rules and optimistic concurrency are enforced in CaseService.escalate."""
    case, event = await c.case_service.escalate(session, principal, case_id, payload.reason, if_match)
    background.add_task(request.app.state.dispatcher.dispatch_event, event.id)
    return JSONResponse(_case_json(case), headers={"ETag": case.etag}, background=background)
