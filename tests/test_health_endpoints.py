"""Tests for GET/HEAD on /health and /ready.

Regression coverage for a real bug found live: shields.io's status badge
(and Better Stack, DEPLOYMENT.md's documented uptime monitor) issue HEAD
requests against /health by default. FastAPI's bare `@app.get(...)`
shorthand does NOT auto-add HEAD support the way Starlette's own Route
claims to on this pinned FastAPI version (reproduced with a minimal
two-line FastAPI app, unrelated to anything specific to this route) — every
HEAD request got a 405, making the app look permanently "down" on
monitoring dashboards despite GET /health responding 200 the entire time.
Fixed by registering both endpoints via `@app.api_route(path, methods=["GET", "HEAD"])`
instead of `@app.get(path)`.
"""

import httpx
import pytest

from app.config import get_settings
from app.db import session as db_session_module


@pytest.fixture
async def client(monkeypatch, tmp_path):
    db_path = tmp_path / "health_endpoints_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module

    await db_session_module.init_db()

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_get_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_head_health_is_not_405(client):
    """The actual bug: a HEAD request (what uptime monitors send) must not
    be rejected with 405 Method Not Allowed."""
    resp = await client.head("/health")
    assert resp.status_code == 200


async def test_get_ready(client):
    resp = await client.get("/ready")
    assert resp.status_code in (200, 503)  # readiness depends on checkpointer init in this fixture
    assert "ready" in resp.json()


async def test_head_ready_is_not_405(client):
    resp = await client.head("/ready")
    assert resp.status_code != 405
