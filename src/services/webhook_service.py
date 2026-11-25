"""Webhooks: transactional outbox, HMAC signing/verification, delivery with bounded retries.

Signature scheme (Standard-Webhooks style):
  Webhook-Id:        evt_...            (unique event id; receivers de-duplicate on it)
  Webhook-Timestamp: unix seconds       (receivers reject outside a tolerance window -> replay protection)
  Webhook-Signature: v1=<hex HMAC-SHA256(secret, "{id}.{timestamp}.{raw body}")>

Every HTTP attempt is recorded in webhook_attempts (headers sent, status, error, duration), so delivery history
can be inspected and replayed from the dashboard.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings, get_settings
from src.core.errors import ProblemException
from src.models.tables import DomainEvent, WebhookAttempt, WebhookDelivery, WebhookSubscription

log = logging.getLogger(__name__)
DEFAULT_SUBSCRIPTION_ID = "whs_default_local"
EVENT_TYPES = ("case.escalated", "case.comment_added", "webhook.test")
USER_AGENT = "entitlement-platform-webhooks/1.0"


def resolve_secret(secret_ref: str) -> str:
    """Secrets are referenced, never stored. Production: a vault/KMS lookup."""
    if secret_ref == "env:PLATFORM_WEBHOOK_SIGNING_SECRET":
        return get_settings().webhook_signing_secret
    if secret_ref.startswith("env:") and (value := os.environ.get(secret_ref[4:])):
        return value
    raise LookupError(f"cannot resolve webhook secret reference {secret_ref!r}")


def sign(secret: str, event_id: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{event_id}.{timestamp}.".encode() + body, hashlib.sha256)
    return "v1=" + mac.hexdigest()


def verify_signature(secret: str, headers, body: bytes, tolerance_seconds: int, now: float | None = None) -> str:
    """Returns the event id, or raises 401 problem. Constant-time comparison."""
    event_id, ts, sig = headers.get("webhook-id"), headers.get("webhook-timestamp"), headers.get("webhook-signature")
    if not (event_id and ts and sig):
        raise ProblemException(401, "invalid-webhook-signature", "Invalid webhook signature",
                               "Missing Webhook-Id, Webhook-Timestamp or Webhook-Signature header.")
    try:
        age = abs((now or time.time()) - int(ts))
    except ValueError:
        age = float("inf")
    if age > tolerance_seconds:
        raise ProblemException(401, "invalid-webhook-signature", "Invalid webhook signature",
                               "Webhook timestamp is outside the allowed tolerance window.")
    expected = sign(secret, event_id, ts, body)
    if not any(hmac.compare_digest(expected, candidate.strip()) for candidate in sig.split(" ")):
        raise ProblemException(401, "invalid-webhook-signature", "Invalid webhook signature",
                               "Webhook signature does not match the payload.")
    return event_id


async def ensure_default_subscription(session: AsyncSession, settings: Settings) -> None:
    """Create the default subscription, or bring its URL/event types in line with the current configuration."""
    if not settings.webhook_default_subscriber_url:
        return
    sub = await session.get(WebhookSubscription, DEFAULT_SUBSCRIPTION_ID)
    if sub is None:
        session.add(WebhookSubscription(id=DEFAULT_SUBSCRIPTION_ID, endpoint_url=settings.webhook_default_subscriber_url,
                                        event_types=list(settings.webhook_default_event_types),
                                        secret_ref="env:PLATFORM_WEBHOOK_SIGNING_SECRET"))
    else:
        sub.endpoint_url = settings.webhook_default_subscriber_url
        sub.event_types = list(settings.webhook_default_event_types)
    await session.commit()


async def enqueue_event(session: AsyncSession, event_type: str, resource_id: str, payload: dict,
                        subscription_ids: list[str] | None = None) -> DomainEvent:
    """Write event + one pending delivery per matching subscription (caller commits atomically).

    `subscription_ids` targets specific subscriptions regardless of their event types (used for test events).
    """
    event_id = f"evt_{uuid.uuid4().hex}"
    event = DomainEvent(id=event_id, event_type=event_type, resource_id=resource_id,
                        payload={"id": event_id, "event": event_type, **payload})
    session.add(event)
    subs = (await session.execute(
        select(WebhookSubscription).where(WebhookSubscription.enabled.is_(True)))).scalars().all()
    await session.flush()
    for sub in subs:
        wanted = sub.id in subscription_ids if subscription_ids is not None else event_type in (sub.event_types or [])
        if wanted:
            session.add(WebhookDelivery(event_id=event_id, subscription_id=sub.id, status="pending"))
    return event


class WebhookDispatcher:
    def __init__(self, sessionmaker: async_sessionmaker, http: httpx.AsyncClient, settings: Settings):
        self.sessionmaker, self.http, self.settings = sessionmaker, http, settings

    async def dispatch_event(self, event_id: str, trigger: str = "auto") -> None:
        async with self.sessionmaker() as session:
            event = await session.get(DomainEvent, event_id)
            deliveries = (await session.execute(select(WebhookDelivery).where(
                WebhookDelivery.event_id == event_id, WebhookDelivery.status == "pending"))).scalars().all()
            for delivery in deliveries:
                sub = await session.get(WebhookSubscription, delivery.subscription_id)
                await self._deliver(session, event, sub, delivery, trigger)

    async def redeliver_pending(self) -> int:
        """Resume deliveries left `pending` (e.g. the process stopped between commit and send)."""
        async with self.sessionmaker() as session:
            event_ids = (await session.execute(select(WebhookDelivery.event_id).where(
                WebhookDelivery.status == "pending").distinct())).scalars().all()
        for event_id in event_ids:
            try:
                await self.dispatch_event(event_id, trigger="startup")
            except Exception:  # one bad event must not stop the others
                log.exception("startup redelivery failed", extra={"event_id": event_id})
        return len(event_ids)

    async def _deliver(self, session: AsyncSession, event: DomainEvent, sub: WebhookSubscription,
                       delivery: WebhookDelivery, trigger: str) -> None:
        body = json.dumps(event.payload, separators=(",", ":"), sort_keys=True).encode()
        try:
            secret = resolve_secret(sub.secret_ref)
        except LookupError as exc:
            delivery.status, delivery.last_error = "failed", str(exc)
            await session.commit()
            return
        attempt_no = (await session.execute(select(func.count()).select_from(WebhookAttempt).where(
            WebhookAttempt.delivery_id == delivery.id))).scalar_one()
        while delivery.attempts < self.settings.webhook_max_attempts:
            ts = str(int(time.time()))
            headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT,
                       "Webhook-Id": event.id, "Webhook-Timestamp": ts,
                       "Webhook-Signature": sign(secret, event.id, ts, body)}
            delivery.attempts += 1
            attempt_no += 1
            attempt = WebhookAttempt(delivery_id=delivery.id, attempt_no=attempt_no, trigger=trigger,
                                     request_headers={k: v for k, v in headers.items() if k != "Content-Type"})
            permanent = False
            started = time.perf_counter()
            try:
                resp = await self.http.post(sub.endpoint_url, content=body, headers=headers,
                                            timeout=self.settings.webhook_timeout_seconds)
                attempt.status_code = delivery.last_status_code = resp.status_code
                attempt.response_body = resp.text[:500]
                if 200 <= resp.status_code < 300:
                    attempt.duration_ms = round((time.perf_counter() - started) * 1000)
                    session.add(attempt)
                    delivery.status, delivery.last_error = "delivered", None
                    await session.commit()
                    log.info("webhook delivered", extra={"event_id": event.id, "subscription_id": sub.id,
                                                         "attempts": delivery.attempts})
                    return
                delivery.last_error = f"HTTP {resp.status_code}"
                permanent = 400 <= resp.status_code < 500 and resp.status_code not in (408, 429)
            except httpx.HTTPError as exc:
                delivery.last_error = type(exc).__name__
            attempt.error = delivery.last_error
            attempt.duration_ms = round((time.perf_counter() - started) * 1000)
            session.add(attempt)
            await session.commit()
            if permanent:
                break
            if delivery.attempts < self.settings.webhook_max_attempts:
                await asyncio.sleep(self.settings.webhook_backoff_initial * 2 ** (delivery.attempts - 1))
        delivery.status = "failed"
        await session.commit()
        log.warning("webhook delivery failed", extra={"event_id": event.id, "subscription_id": sub.id,
                                                      "attempts": delivery.attempts, "error": delivery.last_error})
