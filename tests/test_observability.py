"""Tests for OpenTelemetry instrumentation (Stage 3.4).

Uses a real TracerProvider wired to an in-memory span exporter (not mocks)
so assertions check actual span names/attributes/status as OTel itself
would report them, rather than asserting on our own wrapper's internals.
"""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from prometheus_client import REGISTRY

from app.observability import record_llm_call, record_tool_call, trace_graph_node


def _sample(name: str, labels: dict | None = None) -> float:
    # Same delta-based-assertion rationale as test_metrics.py's helper:
    # prometheus_client's registry is process-global across the whole test
    # session, so absolute values depend on test ordering.
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


# OTel's global API only allows trace.set_tracer_provider() to succeed
# once per process — a second call is silently ignored (with a warning),
# so the in-memory provider/exporter must be installed exactly once for
# this whole test module rather than per-test. Each test clears the
# exporter's buffer instead of swapping providers, to stay isolated.
_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


@pytest.fixture
def span_exporter():
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


async def test_trace_graph_node_wraps_async_node_and_records_success(span_exporter):
    @trace_graph_node("my_node")
    async def some_node(state):
        return {"done": True}

    result = await some_node({"x": 1})

    assert result == {"done": True}
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "agent.node.my_node"
    assert spans[0].status.status_code == StatusCode.OK
    assert spans[0].attributes["duration_ms"] >= 0


def test_trace_graph_node_wraps_sync_node_and_records_success(span_exporter):
    @trace_graph_node("sync_node")
    def some_node(state):
        return {"done": True}

    result = some_node({"x": 1})

    assert result == {"done": True}
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "agent.node.sync_node"
    assert spans[0].status.status_code == StatusCode.OK


async def test_trace_graph_node_records_error_status_and_reraises(span_exporter):
    @trace_graph_node("failing_node")
    async def some_node(state):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await some_node({})

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].events[0].name == "exception"


def test_trace_graph_node_preserves_function_identity():
    @trace_graph_node("named_node")
    async def some_node(state):
        """docstring"""
        return state

    assert some_node.__name__ == "some_node"


async def test_record_llm_call_sets_gen_ai_attributes_on_current_span(span_exporter):
    tracer = trace.get_tracer("test")

    class _FakeResponse:
        usage_metadata = {"input_tokens": 10, "output_tokens": 5}

    with tracer.start_as_current_span("test-span"):
        record_llm_call("plan", "llama-3.1-8b-instant", _FakeResponse())

    spans = span_exporter.get_finished_spans()
    assert spans[0].attributes["gen_ai.request.model"] == "llama-3.1-8b-instant"
    assert spans[0].attributes["gen_ai.usage.input_tokens"] == 10
    assert spans[0].attributes["gen_ai.usage.output_tokens"] == 5


async def test_record_llm_call_tolerates_missing_usage_metadata(span_exporter):
    tracer = trace.get_tracer("test")

    class _FakeResponse:
        pass

    with tracer.start_as_current_span("test-span"):
        record_llm_call("plan", "llama-3.1-8b-instant", _FakeResponse())

    spans = span_exporter.get_finished_spans()
    assert spans[0].attributes["gen_ai.request.model"] == "llama-3.1-8b-instant"
    assert "gen_ai.usage.input_tokens" not in spans[0].attributes


async def test_record_tool_call_sets_mcp_attributes_on_success(span_exporter):
    tracer = trace.get_tracer("test")

    with tracer.start_as_current_span("test-span"):
        record_tool_call("identity_get_user", ok=True, domain="identity")

    spans = span_exporter.get_finished_spans()
    assert spans[0].attributes["mcp.tool.name"] == "identity_get_user"
    assert spans[0].attributes["mcp.tool.success"] is True
    assert spans[0].attributes["mcp.tool.domain"] == "identity"


async def test_record_tool_call_omits_domain_when_not_given(span_exporter):
    tracer = trace.get_tracer("test")

    with tracer.start_as_current_span("test-span"):
        record_tool_call("some_tool", ok=False)

    spans = span_exporter.get_finished_spans()
    assert spans[0].attributes["mcp.tool.success"] is False
    assert "mcp.tool.domain" not in spans[0].attributes


async def test_configure_observability_is_idempotent(monkeypatch):
    """Calling configure_observability() twice must not raise or double-set
    the global tracer provider — main.py calls it at import time, and
    pytest importing app.api.main more than once (or other modules doing
    the same) must stay a safe no-op past the first call.
    """
    import app.observability as observability

    monkeypatch.setattr(observability, "_configured", False)
    observability.configure_observability()
    observability.configure_observability()


async def test_record_llm_call_records_cost_for_a_priced_anthropic_model(span_exporter):
    """claude-sonnet-4-5 is confirmed present verbatim (no provider prefix
    needed) in litellm's bundled model_prices_and_context_window.json —
    checked directly against the file, not assumed."""
    tracer = trace.get_tracer("test")

    class _FakeResponse:
        usage_metadata = {"input_tokens": 1000, "output_tokens": 500}

    before = _sample("llm_cost_usd_total", {"model": "claude-sonnet-4-5"})
    with tracer.start_as_current_span("test-span"):
        record_llm_call("plan", "claude-sonnet-4-5", _FakeResponse())
    after = _sample("llm_cost_usd_total", {"model": "claude-sonnet-4-5"})

    assert after > before


async def test_record_llm_call_records_cost_for_a_groq_model_via_provider_prefix(span_exporter):
    """'llama-3.1-8b-instant' (this project's bare groq_model config value)
    is NOT itself a key in litellm's pricing table — only the prefixed
    'groq/llama-3.1-8b-instant' is (confirmed directly against
    model_prices_and_context_window.json). This is the specific case
    _litellm_model_candidates exists to handle."""
    tracer = trace.get_tracer("test")

    class _FakeResponse:
        usage_metadata = {"input_tokens": 1000, "output_tokens": 500}

    before = _sample("llm_cost_usd_total", {"model": "llama-3.1-8b-instant"})
    with tracer.start_as_current_span("test-span"):
        record_llm_call("plan", "llama-3.1-8b-instant", _FakeResponse())
    after = _sample("llm_cost_usd_total", {"model": "llama-3.1-8b-instant"})

    assert after > before


async def test_record_llm_call_marks_unknown_model_as_unpriced_not_zero_cost(span_exporter):
    """A model litellm's pricing table has no entry for at all (under any
    of this project's known provider prefixes) must be counted as
    unpriced, never silently recorded as $0 cost — those two states look
    identical on a dashboard unless kept as separate metrics."""
    tracer = trace.get_tracer("test")

    class _FakeResponse:
        usage_metadata = {"input_tokens": 1000, "output_tokens": 500}

    cost_before = _sample("llm_cost_usd_total", {"model": "totally-unknown-model-xyz"})
    unpriced_before = _sample("llm_cost_unpriced_calls_total", {"model": "totally-unknown-model-xyz"})
    with tracer.start_as_current_span("test-span"):
        record_llm_call("plan", "totally-unknown-model-xyz", _FakeResponse())
    cost_after = _sample("llm_cost_usd_total", {"model": "totally-unknown-model-xyz"})
    unpriced_after = _sample(
        "llm_cost_unpriced_calls_total", {"model": "totally-unknown-model-xyz"}
    )

    assert cost_after == cost_before
    assert unpriced_after == unpriced_before + 1


async def test_no_op_when_otel_endpoint_unset(monkeypatch):
    """With OTEL_EXPORTER_OTLP_ENDPOINT unset (the default/local-dev case),
    configure_observability() must return before ever calling
    trace.set_tracer_provider() — tracing stays a harmless no-op rather
    than crashing or trying to export anywhere. Asserted via a spy rather
    than "provider didn't change", since OTel's global API silently
    ignores a second set_tracer_provider() call regardless of whether this
    function makes it — that would pass even if the early-return broke.
    """
    import app.observability as observability
    from app.config import get_settings

    monkeypatch.setattr(observability, "_configured", False)
    settings = get_settings()
    monkeypatch.setattr(settings, "otel_exporter_endpoint", "")
    monkeypatch.setattr(observability, "get_settings", lambda: settings)

    called = False
    original_set = trace.set_tracer_provider

    def _spy(provider):
        nonlocal called
        called = True
        original_set(provider)

    monkeypatch.setattr(trace, "set_tracer_provider", _spy)
    observability.configure_observability()
    assert called is False
