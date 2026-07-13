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
