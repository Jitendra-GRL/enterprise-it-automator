"""Tests for app/agent/transcription.py — voice-to-ticket transcription via
Groq's Whisper endpoint. The `groq.Groq` client is monkeypatched at the
module's own reference (not the real API) — these tests verify OUR
credential-checking and thread-offloading logic, not Groq's transcription
quality.
"""

import pytest

from app.agent.transcription import TranscriptionUnavailableError, transcribe_audio
from app.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_transcribe_audio_raises_when_no_groq_key(monkeypatch):
    # Explicit empty string, not delenv — pydantic-settings would otherwise
    # still read a real key from this dev machine's .env file (same
    # pattern as tests/test_llm_provider.py's test_groq_without_key_raises).
    monkeypatch.setenv("GROQ_API_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(TranscriptionUnavailableError, match="GROQ_API_KEY"):
        await transcribe_audio(b"fake-audio-bytes", "clip.webm")


async def test_transcribe_audio_returns_stripped_text(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-key")
    get_settings.cache_clear()

    captured = {}

    class _FakeTranscriptions:
        def create(self, *, model, file, response_format):
            captured["model"] = model
            captured["file"] = file
            captured["response_format"] = response_format
            return "  disable jsmith's account  \n"

    class _FakeAudio:
        transcriptions = _FakeTranscriptions()

    class _FakeGroqClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key
            self.audio = _FakeAudio()

    monkeypatch.setattr("app.agent.transcription.Groq", _FakeGroqClient)

    text = await transcribe_audio(b"fake-audio-bytes", "clip.webm")

    assert text == "disable jsmith's account"
    assert captured["api_key"] == "fake-key"
    assert captured["file"] == ("clip.webm", b"fake-audio-bytes")
    assert captured["response_format"] == "text"
    assert captured["model"] == get_settings().stt_model


async def test_transcribe_audio_propagates_sdk_errors(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-key")
    get_settings.cache_clear()

    class _FakeTranscriptions:
        def create(self, **kwargs):
            raise RuntimeError("groq API error: invalid audio format")

    class _FakeAudio:
        transcriptions = _FakeTranscriptions()

    class _FakeGroqClient:
        def __init__(self, api_key):
            self.audio = _FakeAudio()

    monkeypatch.setattr("app.agent.transcription.Groq", _FakeGroqClient)

    with pytest.raises(RuntimeError, match="invalid audio format"):
        await transcribe_audio(b"fake-audio-bytes", "clip.webm")
