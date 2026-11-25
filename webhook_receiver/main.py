"""Demo webhook receiver: a separate system that consumes the platform's signed webhooks.

    POST /webhooks/receive   verify HMAC signature + timestamp, de-duplicate on Webhook-Id, record the request
    GET  /events             what arrived (accepted, duplicate, rejected, simulated failures), newest first
    GET  /events/stream      Server-Sent Events: every received request is pushed to listening browsers at once
                             (the dashboard's pop-up notifications)
    GET|PUT /control         failure simulation, to show the sender's retries live:
                               ok         -> 200
                               fail       -> 500 every time        (sender retries, then marks failed)
                               fail_next  -> 500 for the next N    (sender succeeds on a later attempt)
                               slow       -> answer after N seconds (each try times out at the sender, which marks
                                                                     the delivery failed, but every request is still
                                                                     processed here: first accepted, rest duplicate)
                               reject     -> 400                    (permanent error: sender does not retry)

Run:  uvicorn webhook_receiver.main:app --port 8200
Env:  WEBHOOK_SECRET (must equal the platform's PLATFORM_WEBHOOK_SIGNING_SECRET), WEBHOOK_TOLERANCE_SECONDS,
      RECEIVER_DB (SQLite path), CORS_ORIGINS.
"""
import asyncio
import json
import os
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from webhook_receiver.security import verify
from webhook_receiver.store import EventStore

SHOWN_HEADERS = ("webhook-id", "webhook-timestamp", "webhook-signature", "user-agent", "content-type")
KEEPALIVE_SECONDS = 15


class Hub:
    """Fan-out of received webhooks to live listeners (one queue per open /events/stream connection)."""

    def __init__(self):
        self.listeners: set = set()

    def publish(self, item: dict) -> None:
        for queue in list(self.listeners):
            try:
                queue.put_nowait(item)
            except Exception:  # a slow listener with a full queue misses this one; it can reload /events
                pass


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["ok", "fail", "fail_next", "slow", "reject"] = "ok"
    fail_count: int = Field(2, ge=1, le=10, description="For fail_next: how many requests fail before recovering.")
    delay_seconds: float = Field(8.0, ge=0, le=30, description="For slow: how long to wait before answering.")


def create_app(secret: str | None = None, db_path: str | None = None, tolerance_seconds: int | None = None) -> FastAPI:
    secret = secret or os.getenv("WEBHOOK_SECRET", "whsec_dev_only_change_me_0123456789")
    tolerance = tolerance_seconds if tolerance_seconds is not None else int(os.getenv("WEBHOOK_TOLERANCE_SECONDS", "300"))
    store = EventStore(db_path or os.getenv("RECEIVER_DB", "data/receiver.db"))

    app = FastAPI(title="Demo webhook receiver", version="1.0.0", description=(
        "An independent system receiving the platform's webhooks. It verifies signatures, rejects replays, "
        "de-duplicates on `Webhook-Id`, and can simulate failures (`/control`) so you can watch the sender retry."))
    app.add_middleware(CORSMiddleware,
                       allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(","),
                       allow_methods=["GET", "PUT", "DELETE"], allow_headers=["Content-Type"])
    app.state.store = store
    app.state.hub = Hub()
    app.state.control = Control()
    app.state.failures_left = 0

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/docs")

    @app.get("/health", tags=["receiver"])
    async def health():
        return {"status": "ok", "mode": app.state.control.mode}

    @app.post("/webhooks/receive", tags=["receiver"])
    async def receive(request: Request):
        raw = await request.body()
        headers = {k: v for k, v in request.headers.items() if k in SHOWN_HEADERS}
        event_id = request.headers.get("webhook-id")
        text = raw.decode("utf-8", errors="replace")
        event_type = None
        try:
            event_type = json.loads(raw).get("event")
        except (ValueError, AttributeError):
            pass

        def record(outcome: str, status: int, valid: bool | None, reason: str | None):
            row = store.add(event_id=event_id, event_type=event_type, outcome=outcome, http_status=status,
                            signature_valid=valid, reason=reason, headers=headers, body=text)
            app.state.hub.publish(row)  # push to open browsers (notifications)

        # 1. Simulated outages happen before any processing, like a crashed or overloaded service.
        control: Control = app.state.control
        if control.mode == "fail" or (control.mode == "fail_next" and app.state.failures_left > 0):
            if control.mode == "fail_next":
                app.state.failures_left -= 1
                if app.state.failures_left == 0:
                    app.state.control = Control(mode="ok")  # recovered
            record("simulated_failure", 500, None, f"simulated outage (mode {control.mode})")
            return JSONResponse({"error": "simulated receiver failure"}, status_code=500)
        if control.mode == "reject":
            record("rejected", 400, None, "simulated permanent rejection (mode reject)")
            return JSONResponse({"error": "simulated rejection"}, status_code=400)
        if control.mode == "slow":
            await asyncio.sleep(control.delay_seconds)

        # 2. Authenticity: signature + timestamp window.
        check = verify(secret, event_id, request.headers.get("webhook-timestamp"),
                       request.headers.get("webhook-signature"), raw, tolerance)
        if not check.valid:
            record("rejected", 401, False, check.reason)
            return JSONResponse({"error": "invalid signature", "reason": check.reason}, status_code=401)

        # 3. Idempotency: the sender retries, so the same event can arrive more than once.
        if store.already_processed(event_id):
            record("duplicate", 200, True, "already processed; acknowledged without processing again")
            return {"status": "duplicate", "event_id": event_id}

        record("accepted", 200, True, None)
        return {"status": "accepted", "event_id": event_id}

    @app.get("/events", tags=["history"])
    async def list_events(limit: int = Query(100, ge=1, le=500)):
        return {"counts": store.counts(), "events": store.list(limit)}

    @app.get("/events/stream", tags=["history"])
    async def stream(request: Request):
        """Server-Sent Events: `event: webhook` with the stored record, the moment a request arrives."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        app.state.hub.listeners.add(queue)

        async def events():
            try:
                yield "retry: 3000\n\n"  # browsers reconnect after 3 s if the connection drops
                while not await request.is_disconnected():
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"id: {item['id']}\nevent: webhook\ndata: {json.dumps(item)}\n\n"
            finally:
                app.state.hub.listeners.discard(queue)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.delete("/events", tags=["history"])
    async def clear_events():
        return {"deleted": store.clear()}

    @app.get("/control", tags=["failure simulation"])
    async def get_control():
        return {**app.state.control.model_dump(), "failures_left": app.state.failures_left}

    @app.put("/control", tags=["failure simulation"])
    async def set_control(control: Control):
        app.state.control = control
        app.state.failures_left = control.fail_count if control.mode == "fail_next" else 0
        return {**control.model_dump(), "failures_left": app.state.failures_left}

    return app


app = create_app()
