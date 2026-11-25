import os

# Must be set before src modules read settings.
os.environ.update({
    "PLATFORM_FIXED_NOW": "2026-11-23T10:00:00Z",
    "PLATFORM_JWT_SECRET": "test-jwt-secret-0123456789-abcdefghijkl",
    "PLATFORM_WEBHOOK_SIGNING_SECRET": "whsec_test_0123456789",
    "PLATFORM_LOG_LEVEL": "WARNING",
    "PLATFORM_UPSTREAM_BACKOFF_INITIAL": "0.01",
    "PLATFORM_UPSTREAM_BACKOFF_MAX": "0.02",
    "PLATFORM_UPSTREAM_CONNECT_TIMEOUT": "0.1",
    "PLATFORM_UPSTREAM_READ_TIMEOUT": "0.2",
    "PLATFORM_WEBHOOK_BACKOFF_INITIAL": "0.01",
    "PLATFORM_WEBHOOK_DEFAULT_SUBSCRIBER_URL": "http://testserver/v1/webhooks/receiver",
})

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.config import Settings, get_settings  # noqa: E402
from src.core.security import issue_persona_token  # noqa: E402
from src.main import create_app  # noqa: E402

get_settings.cache_clear()


def make_client(tmp_path, **overrides) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", **overrides)
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()
    # Deliver webhooks back into this same app in-process (no network).
    app.state.dispatcher.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    return client


@pytest.fixture
def client(tmp_path):
    c = make_client(tmp_path)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def mock_state(client):
    return client.app.state.mock_upstream.state.mock


def auth(persona: str, agent_id: str | None = None) -> dict:
    return {"Authorization": f"Bearer {issue_persona_token(persona, agent_id=agent_id)}"}


def set_fault(client: TestClient, system: str, mode: str) -> None:
    assert client.post("/dev/upstream-faults", json={"system": system, "mode": mode}).status_code == 200
