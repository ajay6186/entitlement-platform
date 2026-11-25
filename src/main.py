"""Application factory. Run: uvicorn src.main:app  (or: docker compose up)"""
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from src.api import admin, cases, customers, webhooks
from src.config import Settings, get_settings
from src.core.errors import install_error_handlers
from src.core.logging import configure_logging
from src.core.middleware import install_middleware
from src.core.rate_limit import RateLimiter
from src.dependencies import build_container
from src.models.database import init_db, make_engine, make_sessionmaker
from src.services.webhook_service import WebhookDispatcher, ensure_default_subscription
from src.upstreams.mock_app import create_mock_upstream_app

ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = ROOT / "openapi.yaml"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        await init_db(engine)
        app.state.engine, app.state.sessionmaker = engine, make_sessionmaker(engine)
        if settings.upstream_base_url:
            upstream_http = httpx.AsyncClient(base_url=settings.upstream_base_url, timeout=httpx.Timeout(
                settings.upstream_read_timeout, connect=settings.upstream_connect_timeout))
        else:
            mock = create_mock_upstream_app(str(ROOT / settings.mock_data_dir))
            app.state.mock_upstream = mock
            upstream_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock), base_url="http://mock-upstreams")
        app.state.container = build_container(settings, upstream_http)
        webhook_http = httpx.AsyncClient()
        app.state.dispatcher = WebhookDispatcher(app.state.sessionmaker, webhook_http, settings)
        async with app.state.sessionmaker() as session:
            await ensure_default_subscription(session, settings)
        # Resume deliveries left pending by a previous run (outbox recovery), without delaying start-up.
        redelivery = asyncio.create_task(app.state.dispatcher.redeliver_pending())
        yield
        if not redelivery.done():
            redelivery.cancel()
            with suppress(asyncio.CancelledError):
                await redelivery
        await upstream_http.aclose()
        await app.state.dispatcher.http.aclose()
        await engine.dispose()

    app = FastAPI(title="Customer Entitlement & Support Case Platform", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.rate_limiter = RateLimiter(settings)

    # The hand-written contract (openapi.yaml) is the source of truth; /docs renders it, not a generated one.
    spec = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    app.openapi = lambda: spec

    @app.get("/openapi.yaml", include_in_schema=False)
    async def openapi_yaml():
        return FileResponse(OPENAPI_PATH, media_type="application/yaml")

    install_error_handlers(app)
    install_middleware(app)
    # Outermost: lets the React webhook dashboard call the API from the browser (bearer token, no cookies).
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
                       allow_methods=["GET", "POST", "PATCH"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match", "X-Correlation-ID"],
                       expose_headers=["ETag", "X-Correlation-ID", "Idempotent-Replayed"])
    for router in (customers.router, cases.router, webhooks.router, admin.audit_router, admin.health_router):
        app.include_router(router)
    if settings.enable_dev_endpoints:
        app.include_router(admin.dev_router)
    return app


app = create_app()
