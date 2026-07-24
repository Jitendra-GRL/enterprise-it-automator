"""OpenTelemetry instrumentation for the agent's LLM/tool-call/graph-node
lifecycle — GenAI semantic conventions where they apply, so the exporter
backend stays swappable (Langfuse, LangSmith, or any OTLP collector) without
touching instrumentation call sites.

Deliberately not vendor-specific: spans use OTel's generic API
(get_tracer().start_as_current_span) rather than a Langfuse/LangSmith SDK
directly, so swapping the exporter later (configure_observability's job) is
a config change, not a call-site rewrite. If OTEL_EXPORTER_OTLP_ENDPOINT is
unset, tracing degrades to a no-op tracer provider — instrumentation is
always safe to leave in place even with no collector running.
"""

import functools
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from app import metrics
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

from app.config import get_settings

logger = logging.getLogger(__name__)

_configured = False


def configure_observability() -> None:
    """Sets up the OTel tracer provider once per process. Safe to call
    multiple times (idempotent) — a second call is a no-op. If
    OTEL_EXPORTER_OTLP_ENDPOINT is unset, the default global tracer
    provider (a no-op) is left in place, so start_as_current_span calls
    throughout the app remain cheap and harmless with no collector
    configured — this is the correct default for local dev.
    """
    global _configured
    if _configured:
        return
    _configured = True

    settings = get_settings()
    if not settings.otel_exporter_endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set — tracing spans are no-ops.")
        return

    resource = Resource(attributes={SERVICE_NAME: "enterprise-it-automator"})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.info("OpenTelemetry tracing configured, exporting to %s", settings.otel_exporter_endpoint)


def get_tracer():
    return trace.get_tracer("enterprise-it-automator")


F = TypeVar("F", bound=Callable[..., Any])


def trace_graph_node(node_name: str) -> Callable[[F], F]:
    """Decorator for a LangGraph node function: wraps it in a span named
    agent.node.<node_name>, recording wall-clock duration and success/error
    status — this is the "latency per graph node" half of Stage 3.4's
    observability goal. Most nodes in this codebase are async (they call an
    LLM or an MCP tool), but a few pure routing/aggregation nodes (e.g.
    join_batch_node) are plain sync functions — inspect.iscoroutinefunction
    picks the right wrapper so both keep their original sync/async calling
    convention instead of every node silently becoming a coroutine.
    """

    def decorator(fn: F) -> F:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                tracer = get_tracer()
                start = time.monotonic()
                with tracer.start_as_current_span(f"agent.node.{node_name}") as span:
                    try:
                        result = await fn(*args, **kwargs)
                        span.set_status(Status(StatusCode.OK))
                        return result
                    except Exception as exc:
                        span.set_status(Status(StatusCode.ERROR, str(exc)))
                        span.record_exception(exc)
                        raise
                    finally:
                        span.set_attribute("duration_ms", (time.monotonic() - start) * 1000)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            tracer = get_tracer()
            start = time.monotonic()
            with tracer.start_as_current_span(f"agent.node.{node_name}") as span:
                try:
                    result = fn(*args, **kwargs)
                    span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    span.record_exception(exc)
                    raise
                finally:
                    span.set_attribute("duration_ms", (time.monotonic() - start) * 1000)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _litellm_model_candidates(model: str) -> list[str]:
    """litellm's bundled pricing table keys most non-OpenAI/Anthropic
    models by '<provider>/<model>' (confirmed by inspecting
    model_prices_and_context_window.json directly: 'llama-3.1-8b-instant'
    is NOT a key, but 'groq/llama-3.1-8b-instant' is; likewise
    'watsonx/ibm/granite-3-8b-instruct' vs the bare 'ibm/granite-3-8b-instruct').
    record_llm_call only receives the bare model string (see
    _served_model_name in app/agent/graph.py — provider identity isn't
    plumbed through), so this tries the bare name first (correct for
    Anthropic, e.g. 'claude-sonnet-4-5'), then each of this project's own
    configured provider prefixes — cheap to try all of them in order
    since a mismatch is just a dict miss, not an error, until
    litellm.cost_per_token is actually called with the first one that's a
    real hit.
    """
    return [model, f"groq/{model}", f"watsonx/{model}", f"openrouter/{model}"]


def _record_estimated_cost(model: str, input_tokens: int, output_tokens: int) -> None:
    """Estimates this call's USD cost via litellm's bundled static pricing
    table (litellm.cost_per_token) — pure local computation against a
    dictionary shipped in the litellm package, no network call and no
    dependency on litellm's proxy/router (this project uses its OWN
    provider-fallback logic in app/agent/llm.py; litellm is used here
    ONLY as a cost-calculation utility, not as a request gateway — see
    that module's docstring for why its circuit-breaker-integrated
    fallback isn't being replaced).

    litellm.cost_per_token raises when a model string isn't in its
    pricing map — expected and handled for OpenRouter's free-tier model
    specifically (genuinely $0 real cost, correctly unpriced rather than
    estimated as zero, which would look identical to "not measured" in
    a dashboard) and for any future model litellm's table hasn't caught
    up to yet. Recorded as LLM_COST_UNPRICED_TOTAL rather than silently
    guessing or raising into the LLM call path — a broken cost estimate
    must never be able to break an actual ticket run.
    """
    import litellm

    for candidate in _litellm_model_candidates(model):
        try:
            input_cost, output_cost = litellm.cost_per_token(
                model=candidate, prompt_tokens=input_tokens, completion_tokens=output_tokens
            )
        except Exception:
            continue
        metrics.LLM_COST_USD.labels(model=model).inc(input_cost + output_cost)
        return
    metrics.LLM_COST_UNPRICED_TOTAL.labels(model=model).inc()


def record_llm_call(span_name: str, model: str, response) -> None:
    """Records token usage for an LLM call on the current span, using
    OTel's GenAI semantic convention attribute names (gen_ai.*) so this
    stays meaningful to any GenAI-aware backend, not just a custom
    dashboard. response is a langchain_core AIMessage-like object;
    usage_metadata is populated by LangChain's chat model wrappers when the
    underlying provider reports token counts (not all providers always do).

    Also increments the Prometheus counters (app/metrics.py) — same call
    site for both signals, so the metrics can never drift out of sync with
    what the traces say happened.
    """
    span = trace.get_current_span()
    span.set_attribute("gen_ai.request.model", model)
    metrics.LLM_CALLS.labels(model=model).inc()
    usage = getattr(response, "usage_metadata", None)
    if usage:
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        if input_tokens:
            metrics.LLM_TOKENS.labels(model=model, direction="input").inc(input_tokens)
        if output_tokens:
            metrics.LLM_TOKENS.labels(model=model, direction="output").inc(output_tokens)
        if input_tokens or output_tokens:
            _record_estimated_cost(model, input_tokens, output_tokens)
        # Imported here, not at module top: token_budget imports app.config,
        # and keeping observability import-light avoids ordering surprises
        # for the several modules that import it first thing.
        from app.agent.token_budget import add_tokens

        add_tokens(input_tokens + output_tokens)


def record_tool_call(tool_name: str, ok: bool, domain: str | None = None) -> None:
    """Records a tool-call outcome on the current span — the "tool-call
    success/failure rate" half of Stage 3.4's observability goal — and on
    the mcp_tool_calls_total Prometheus counter.
    """
    span = trace.get_current_span()
    span.set_attribute("mcp.tool.name", tool_name)
    span.set_attribute("mcp.tool.success", ok)
    if domain:
        span.set_attribute("mcp.tool.domain", domain)
    metrics.MCP_TOOL_CALLS.labels(tool=tool_name, outcome="success" if ok else "failure").inc()
