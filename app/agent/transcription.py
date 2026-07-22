"""Voice-to-ticket transcription — POST /tickets/transcribe (app/api/main.py)
sends an uploaded audio clip through Groq's Whisper endpoint and returns
plain text, which the caller then submits through the existing POST
/tickets unchanged. Deliberately NOT a second way to create a ticket: this
module only ever returns text, never touches the DB or the agent graph, so
the exact same prompt-injection framing/validation ticket text already
gets (app/agent/graph.py's _wrap_untrusted_ticket_text) applies to a
voice-submitted ticket automatically, with no separate code path to keep
in sync or a second surface an attacker could target for a weaker
guardrail.

Uses the `groq` SDK directly (not langchain_groq's ChatGroq, which only
wraps the chat-completions endpoint) — a already-installed dependency for
LLM_PROVIDER=groq, so no new package. Reuses the SAME groq_api_key that
provider already requires, rather than a separate credential to configure.
"""

import asyncio
from typing import cast

from groq import Groq

from app.config import get_settings


class TranscriptionUnavailableError(RuntimeError):
    """Raised when GROQ_API_KEY isn't set — transcription always needs a
    real Groq credential regardless of the configured LLM_PROVIDER, since
    it calls Groq's Whisper endpoint directly, not through the pluggable
    chat-model adapter in app/agent/llm.py."""


def _transcribe_sync(audio_bytes: bytes, filename: str) -> str:
    """Synchronous Groq SDK call — run off the event loop by the async
    wrapper below, since the groq SDK has no async client and blocking the
    loop for a real network call would stall every other in-flight request.
    """
    settings = get_settings()
    client = Groq(api_key=settings.groq_api_key)
    # response_format="text" makes the SDK return a plain str directly
    # (json/verbose_json return a Transcription object with a .text
    # attribute instead) — verified live against the real API, not just
    # documentation. The SDK's own type stubs claim Transcription
    # unconditionally regardless of response_format (not a discriminated
    # literal at the type level), so the cast here corrects a real gap
    # between the SDK's static types and its actual runtime behavior.
    response = client.audio.transcriptions.create(
        model=settings.stt_model,
        file=(filename, audio_bytes),
        response_format="text",
    )
    return cast(str, response).strip()


async def transcribe_audio(audio_bytes: bytes, filename: str) -> str:
    """Transcribes an audio clip to text via Groq's Whisper endpoint.
    Raises TranscriptionUnavailableError if GROQ_API_KEY isn't configured;
    propagates the groq SDK's own exception on a real API failure (bad
    audio format, oversized file past Groq's own limit, rate limit,
    transient network error) — the caller (the /tickets/transcribe route)
    is responsible for turning that into the right HTTP status, not this
    module, which stays a plain "text in, text out, or raise" function.
    """
    settings = get_settings()
    if not settings.groq_api_key:
        raise TranscriptionUnavailableError(
            "Voice-to-ticket transcription requires GROQ_API_KEY to be set "
            "(used directly for Groq's Whisper endpoint, regardless of the "
            "configured LLM_PROVIDER)."
        )
    return await asyncio.to_thread(_transcribe_sync, audio_bytes, filename)
