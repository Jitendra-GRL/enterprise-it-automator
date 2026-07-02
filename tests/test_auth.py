import pytest
from fastapi import HTTPException

from app.api.auth import require_api_key
from app.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_require_api_key_allows_when_unset(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    await require_api_key(x_api_key=None)  # no exception


async def test_require_api_key_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret123")
    with pytest.raises(HTTPException) as exc:
        await require_api_key(x_api_key=None)
    assert exc.value.status_code == 401


async def test_require_api_key_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret123")
    with pytest.raises(HTTPException) as exc:
        await require_api_key(x_api_key="wrong")
    assert exc.value.status_code == 401


async def test_require_api_key_accepts_correct_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret123")
    await require_api_key(x_api_key="secret123")  # no exception
