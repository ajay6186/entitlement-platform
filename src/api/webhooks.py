"""Webhook receiver (reference), subscriptions, and the delivery dashboard API (events, attempts, retry, test)."""
import json
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.core.clock import wall_now
from src.core.errors import ProblemException, conflict, not_found
from src.core.security import Principal, get_principal, require
from src.models.database import get_session
from src.models.tables import DomainEvent, WebhookAttempt, WebhookDelivery, WebhookInbox, WebhookSubscription
from src.schemas.api import (
    AttemptOut, DeliveryDetailOut, DeliveryOut, EventDetailOut, EventOut, SubscriptionCreate, SubscriptionOut,
    SubscriptionUpdate, WebhookStatsOut, WebhookTestRequest,
)
from src.services.webhook_service import enqueue_event, verify_signature

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post("/receiver")
async def receive_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    """Reference receiver: verifies HMAC signature and timestamp, de-duplicates on event id."""
    raw = await request.body()
    settings = get_settings()
    event_id = verify_signature(settings.webhook_signing_secret, request.headers, raw,
                                settings.webhook_tolerance_seconds)
    try:
        event = json.loads(raw)
        if event.get("id") != event_id:
            raise ValueError
    except ValueError:
        raise ProblemException(400, "invalid-webhook-payload", "Invalid webhook payload",
                               "Body must be JSON whose id matches the Webhook-Id header.")
    session.add(WebhookInbox(event_id=event_id, event_type=str(event.get("event"))))
    try:
        await session.commit()
    except IntegrityError:  # already processed: acknowledge so the sender stops retrying, do nothing else
        return {"status": "duplicate", "event_id": event_id}
    return {"status": "accepted", "event_id": event_id}


@router.post("/subscriptions", status_code=201, response_model=SubscriptionOut)
async def create_subscription(payload: SubscriptionCreate, principal: Principal = Depends(get_principal),
                              session: AsyncSession = Depends(get_session)):
    require(principal, "webhooks:manage")
    if not payload.secret_ref.startswith(("env:", "vault:")):
        raise ProblemException(422, "validation-error", "Request validation failed",
                               "secret_ref must reference a secret (env:NAME or vault:PATH), not contain one.")
    sub = WebhookSubscription(id=f"whs_{uuid.uuid4().hex[:16]}", endpoint_url=payload.endpoint_url,
                              event_types=payload.event_types, secret_ref=payload.secret_ref, enabled=True)
    session.add(sub)
    await session.commit()
    return sub


@router.get("/subscriptions", response_model=list[SubscriptionOut])
async def list_subscriptions(principal: Principal = Depends(get_principal),
                             session: AsyncSession = Depends(get_session)):
    require(principal, "webhooks:manage")
    return (await session.execute(select(WebhookSubscription).order_by(WebhookSubscription.created_at))).scalars().all()


@router.get("/deliveries", response_model=list[DeliveryOut])
async def list_deliveries(event_id: str | None = Query(None, max_length=40),
                          status: str | None = Query(None, pattern="^(pending|delivered|failed)$"),
                          limit: int = Query(50, ge=1, le=200),
                          principal: Principal = Depends(get_principal),
                          session: AsyncSession = Depends(get_session)):
    require(principal, "webhooks:manage")
    q = select(WebhookDelivery).order_by(WebhookDelivery.id.desc()).limit(limit)
    if event_id:
        q = q.where(WebhookDelivery.event_id == event_id)
    if status:
        q = q.where(WebhookDelivery.status == status)
    return (await session.execute(q)).scalars().all()


@router.patch("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
async def update_subscription(payload: SubscriptionUpdate,
                              subscription_id: str = Path(..., max_length=40),
                              principal: Principal = Depends(get_principal),
                              session: AsyncSession = Depends(get_session)):
    """Enable/disable a subscription or change the events it receives."""
    require(principal, "webhooks:manage")
    sub = await session.get(WebhookSubscription, subscription_id)
    if sub is None:
        raise not_found("Subscription", subscription_id)
    if payload.enabled is not None:
        sub.enabled = payload.enabled
    if payload.event_types is not None:
        sub.event_types = list(payload.event_types)
    await session.commit()
    return sub


# ---- dashboard ---------------------------------------------------------------------------------

def _event_status(deliveries: list[WebhookDelivery]) -> str:
    states = {d.status for d in deliveries}
    if not states:
        return "no_subscribers"
    if "pending" in states:
        return "pending"
    return "failed" if "failed" in states else "delivered"


@router.get("/stats", response_model=WebhookStatsOut)
async def webhook_stats(principal: Principal = Depends(get_principal),
                        session: AsyncSession = Depends(get_session)):
    """Totals for the dashboard's overview cards."""
    require(principal, "webhooks:manage")
    by_type = dict((await session.execute(
        select(DomainEvent.event_type, func.count()).group_by(DomainEvent.event_type))).all())
    by_status = dict((await session.execute(
        select(WebhookDelivery.status, func.count()).group_by(WebhookDelivery.status))).all())
    attempts_total = (await session.execute(select(func.count()).select_from(WebhookAttempt))).scalar_one()
    failed_attempts = (await session.execute(select(func.count()).select_from(WebhookAttempt).where(
        (WebhookAttempt.status_code.is_(None)) | (WebhookAttempt.status_code >= 300)))).scalar_one()
    subs = (await session.execute(select(WebhookSubscription.enabled))).scalars().all()
    last = (await session.execute(select(func.max(DomainEvent.created_at)))).scalar_one()
    return WebhookStatsOut(
        events_total=sum(by_type.values()), events_by_type=by_type,
        deliveries={k: by_status.get(k, 0) for k in ("pending", "delivered", "failed")},
        attempts_total=attempts_total, failed_attempts=failed_attempts,
        subscriptions_enabled=sum(1 for e in subs if e), subscriptions_total=len(subs), last_event_at=last)


@router.get("/events", response_model=list[EventOut])
async def list_events(event_type: str | None = Query(None, max_length=64),
                      limit: int = Query(50, ge=1, le=200),
                      principal: Principal = Depends(get_principal),
                      session: AsyncSession = Depends(get_session)):
    """Published events, newest first, each with the state of its deliveries."""
    require(principal, "webhooks:manage")
    q = select(DomainEvent).order_by(DomainEvent.created_at.desc(), DomainEvent.id.desc()).limit(limit)
    if event_type:
        q = q.where(DomainEvent.event_type == event_type)
    events = (await session.execute(q)).scalars().all()
    deliveries = (await session.execute(select(WebhookDelivery).where(
        WebhookDelivery.event_id.in_([e.id for e in events])))).scalars().all() if events else []
    per_event: dict[str, list[WebhookDelivery]] = {}
    for d in deliveries:
        per_event.setdefault(d.event_id, []).append(d)
    return [EventOut(id=e.id, event_type=e.event_type, resource_id=e.resource_id, created_at=e.created_at,
                     status=_event_status(per_event.get(e.id, [])),
                     deliveries=[DeliveryOut.model_validate(d, from_attributes=True) for d in per_event.get(e.id, [])])
            for e in events]


@router.get("/events/{event_id}", response_model=EventDetailOut)
async def get_event(event_id: str = Path(..., max_length=40),
                    principal: Principal = Depends(get_principal),
                    session: AsyncSession = Depends(get_session)):
    """One event: its signed payload and every delivery attempt (headers sent, response, timing)."""
    require(principal, "webhooks:manage")
    event = await session.get(DomainEvent, event_id)
    if event is None:
        raise not_found("Event", event_id)
    deliveries = (await session.execute(select(WebhookDelivery).where(
        WebhookDelivery.event_id == event_id).order_by(WebhookDelivery.id))).scalars().all()
    detail = []
    for d in deliveries:
        sub = await session.get(WebhookSubscription, d.subscription_id)
        attempts = (await session.execute(select(WebhookAttempt).where(
            WebhookAttempt.delivery_id == d.id).order_by(WebhookAttempt.attempt_no))).scalars().all()
        detail.append(DeliveryDetailOut(
            **DeliveryOut.model_validate(d, from_attributes=True).model_dump(),
            endpoint_url=sub.endpoint_url if sub else None,
            attempt_log=[AttemptOut.model_validate(a, from_attributes=True) for a in attempts]))
    return EventDetailOut(id=event.id, event_type=event.event_type, resource_id=event.resource_id,
                          created_at=event.created_at, status=_event_status(list(deliveries)),
                          payload=event.payload, deliveries=detail)


@router.post("/deliveries/{delivery_id}/retry", status_code=202, response_model=DeliveryOut)
async def retry_delivery(request: Request, background: BackgroundTasks,
                         delivery_id: int = Path(..., ge=1),
                         principal: Principal = Depends(get_principal),
                         session: AsyncSession = Depends(get_session)):
    """Re-send a failed delivery (a fresh round of attempts, same event id so receivers can de-duplicate)."""
    require(principal, "webhooks:manage")
    delivery = await session.get(WebhookDelivery, delivery_id)
    if delivery is None:
        raise not_found("Delivery", str(delivery_id))
    if delivery.status != "failed":
        raise conflict("delivery-not-failed", "Delivery is not failed",
                       f"Only failed deliveries can be retried; this one is {delivery.status}.")
    delivery.status, delivery.attempts = "pending", 0
    await session.commit()
    background.add_task(request.app.state.dispatcher.dispatch_event, delivery.event_id, "manual_retry")
    body = DeliveryOut.model_validate(delivery, from_attributes=True).model_dump(mode="json")
    return JSONResponse(body, status_code=202, background=background)


@router.post("/test", status_code=202)
async def send_test_event(request: Request, background: BackgroundTasks,
                          payload: WebhookTestRequest | None = None,
                          principal: Principal = Depends(get_principal),
                          session: AsyncSession = Depends(get_session)):
    """Publish a `webhook.test` event to one subscription (or every enabled one) to check a receiver end to end."""
    require(principal, "webhooks:manage")
    sub_id = payload.subscription_id if payload else None
    if sub_id:
        sub = await session.get(WebhookSubscription, sub_id)
        if sub is None:
            raise not_found("Subscription", sub_id)
        if not sub.enabled:
            raise conflict("subscription-disabled", "Subscription is disabled", "Enable it before sending a test.")
        targets = [sub_id]
    else:
        targets = list((await session.execute(select(WebhookSubscription.id).where(
            WebhookSubscription.enabled.is_(True)))).scalars().all())
        if not targets:
            raise conflict("no-enabled-subscriptions", "No enabled subscriptions",
                           "Create or enable a subscription first.")
    event = await enqueue_event(session, "webhook.test", sub_id or "all", {
        "message": "Test event from the webhook dashboard.", "sent_by": principal.sub,
        "timestamp": wall_now().strftime("%Y-%m-%dT%H:%M:%SZ")}, subscription_ids=targets)
    await session.commit()
    background.add_task(request.app.state.dispatcher.dispatch_event, event.id)
    return JSONResponse({"event_id": event.id, "deliveries": len(targets)}, status_code=202, background=background)
