"""Tests for POST /tickets/transcribe — the HTTP layer around
app/agent/transcription.py: auth, daily-request-cap sharing with
POST /tickets, upload-size validation, and error-status mapping.
transcribe_audio itself is monkeypatched here (it's unit-tested directly
in tests/test_transcription.py) so these tests exercise the ROUTE's own
logic without a real Groq call.
"""

import httpx
import pytest

from app.config import get_settings
from app.db import session as db_session_module


@pytest.fixture
async def app_client(monkeypatch, tmp_path):
    db_path = tmp_path / "transcribe_endpoint_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "admin-bootstrap-key")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module

    await db_session_module.init_db()
    await main_module._ensure_bootstrap_admin_client()

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, main_module

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_transcribe_rejects_missing_api_key(app_client):
    client, _ = app_client
    resp = await client.post(
        "/tickets/transcribe", files={"audio": ("clip.webm", b"fake-bytes", "audio/webm")}
    )
    assert resp.status_code == 401


async def test_transcribe_rejects_empty_file(app_client, monkeypatch):
    client, main_module = app_client
    monkeypatch.setattr(main_module, "transcribe_audio", None)  # must never be called
    resp = await client.post(
        "/tickets/transcribe",
        files={"audio": ("clip.webm", b"", "audio/webm")},
        headers={"X-API-Key": "admin-bootstrap-key"},
    )
    assert resp.status_code == 400


async def test_transcribe_rejects_oversized_file(app_client, monkeypatch):
    client, main_module = app_client
    # Settings is a cached, frozen-by-convention pydantic instance — patch
    # the module-level get_settings accessor the route actually calls,
    # rather than mutate the cached instance in place.
    small_settings = get_settings().model_copy(update={"stt_max_upload_bytes": 10})
    monkeypatch.setattr(main_module, "get_settings", lambda: small_settings)

    resp = await client.post(
        "/tickets/transcribe",
        files={"audio": ("clip.webm", b"more than ten bytes of fake audio", "audio/webm")},
        headers={"X-API-Key": "admin-bootstrap-key"},
    )
    assert resp.status_code == 413


async def test_transcribe_returns_text_on_success(app_client, monkeypatch):
    client, main_module = app_client

    async def _fake_transcribe(audio_bytes, filename):
        assert audio_bytes == b"fake-bytes"
        assert filename == "clip.webm"
        return "disable jsmith's account"

    monkeypatch.setattr(main_module, "transcribe_audio", _fake_transcribe)

    resp = await client.post(
        "/tickets/transcribe",
        files={"audio": ("clip.webm", b"fake-bytes", "audio/webm")},
        headers={"X-API-Key": "admin-bootstrap-key"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"text": "disable jsmith's account"}


async def test_transcribe_maps_unavailable_error_to_503(app_client, monkeypatch):
    client, main_module = app_client

    async def _fake_transcribe(audio_bytes, filename):
        from app.agent.transcription import TranscriptionUnavailableError

        raise TranscriptionUnavailableError("GROQ_API_KEY not set")

    monkeypatch.setattr(main_module, "transcribe_audio", _fake_transcribe)

    resp = await client.post(
        "/tickets/transcribe",
        files={"audio": ("clip.webm", b"fake-bytes", "audio/webm")},
        headers={"X-API-Key": "admin-bootstrap-key"},
    )
    assert resp.status_code == 503


async def test_transcribe_maps_sdk_error_to_502(app_client, monkeypatch):
    client, main_module = app_client

    async def _fake_transcribe(audio_bytes, filename):
        raise RuntimeError("groq API error: invalid audio format")

    monkeypatch.setattr(main_module, "transcribe_audio", _fake_transcribe)

    resp = await client.post(
        "/tickets/transcribe",
        files={"audio": ("clip.webm", b"fake-bytes", "audio/webm")},
        headers={"X-API-Key": "admin-bootstrap-key"},
    )
    assert resp.status_code == 502


async def test_transcribe_shares_daily_request_cap_with_tickets(app_client, monkeypatch):
    """Transcription must count against the SAME per-client daily request
    cap as POST /tickets — otherwise a caller could route unlimited Groq
    spend through transcription once the ticket cap is hit."""
    client, main_module = app_client

    async def _fake_transcribe(audio_bytes, filename):
        return "some text"

    monkeypatch.setattr(main_module, "transcribe_audio", _fake_transcribe)

    from sqlalchemy import select

    from app.db.models import ApiClient

    async with db_session_module.session_scope() as session:
        row = await session.scalar(select(ApiClient).where(ApiClient.key == "admin-bootstrap-key"))
        row.daily_request_limit = 1
        row.daily_request_count = 1  # already at the cap

    resp = await client.post(
        "/tickets/transcribe",
        files={"audio": ("clip.webm", b"fake-bytes", "audio/webm")},
        headers={"X-API-Key": "admin-bootstrap-key"},
    )
    assert resp.status_code == 429
