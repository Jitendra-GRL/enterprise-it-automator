"""Tests for the Prometheus metrics layer (app/metrics.py and its wiring
into the HTTP middleware, observability helpers, and SLA sweep).

Counter assertions are DELTA-based (read before, act, read after) rather
than absolute — prometheus_client metrics live in a process-global registry
shared across every test in the session, so absolute values depend on test
ordering and would be flaky by construction.
"""

import datetime as dt

import httpx
import pytest
from prometheus_client import REGISTRY

from app.config import get_settings
from app.db import session as db_session_module
from app.db.models import Approval, ApprovalStatus, Ticket, TicketStatus


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


@pytest.fixture
async def client(monkeypatch, tmp_path):
    db_path = tmp_path / "metrics_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("API_KEY", "test-api-key")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None

    import app.api.main as main_module

    await db_session_module.init_db()
    await main_module._ensure_bootstrap_admin_client()

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": "test-api-key"}
    ) as ac:
        yield ac

    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_metrics_endpoint_serves_prometheus_text(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    # A metric defined in app/metrics.py must be present in the exposition.
    assert "tickets_submitted_total" in resp.text


async def test_metrics_endpoint_requires_no_api_key(client):
    resp = await client.get("/metrics", headers={"X-API-Key": ""})
    assert resp.status_code == 200


async def test_http_requests_counted_by_route_template(client):
    before = _sample(
        "http_requests_total", {"method": "GET", "path": "/health", "status": "200"}
    )
    await client.get("/health")
    after = _sample(
        "http_requests_total", {"method": "GET", "path": "/health", "status": "200"}
    )
    assert after == before + 1


async def test_unmatched_routes_collapse_into_one_label(client):
    before = _sample(
        "http_requests_total", {"method": "GET", "path": "unmatched", "status": "404"}
    )
    await client.get("/no-such-route-abc")
    await client.get("/no-such-route-xyz")
    after = _sample(
        "http_requests_total", {"method": "GET", "path": "unmatched", "status": "404"}
    )
    # Both 404s land on the same "unmatched" series — probing random URLs
    # must not mint new timeseries (cardinality protection).
    assert after == before + 2


async def test_request_duration_histogram_observes(client):
    before = _sample(
        "http_request_duration_seconds_count", {"method": "GET", "path": "/health"}
    )
    await client.get("/health")
    after = _sample(
        "http_request_duration_seconds_count", {"method": "GET", "path": "/health"}
    )
    assert after == before + 1


def test_record_tool_call_increments_counter():
    from app.observability import record_tool_call

    before = _sample("mcp_tool_calls_total", {"tool": "identity_get_user", "outcome": "success"})
    record_tool_call("identity_get_user", ok=True, domain="identity")
    after = _sample("mcp_tool_calls_total", {"tool": "identity_get_user", "outcome": "success"})
    assert after == before + 1

    before_fail = _sample(
        "mcp_tool_calls_total", {"tool": "identity_get_user", "outcome": "failure"}
    )
    record_tool_call("identity_get_user", ok=False, domain="identity")
    after_fail = _sample(
        "mcp_tool_calls_total", {"tool": "identity_get_user", "outcome": "failure"}
    )
    assert after_fail == before_fail + 1


def test_record_llm_call_counts_tokens():
    from app.observability import record_llm_call

    class FakeResponse:
        usage_metadata = {"input_tokens": 120, "output_tokens": 45}

    model = "test-model-metrics"
    calls_before = _sample("llm_calls_total", {"model": model})
    in_before = _sample("llm_tokens_total", {"model": model, "direction": "input"})
    out_before = _sample("llm_tokens_total", {"model": model, "direction": "output"})

    record_llm_call("llm.plan", model, FakeResponse())

    assert _sample("llm_calls_total", {"model": model}) == calls_before + 1
    assert _sample("llm_tokens_total", {"model": model, "direction": "input"}) == in_before + 120
    assert _sample("llm_tokens_total", {"model": model, "direction": "output"}) == out_before + 45


def test_record_llm_call_tolerates_missing_usage():
    from app.observability import record_llm_call

    class NoUsage:
        usage_metadata = None

    model = "test-model-no-usage"
    calls_before = _sample("llm_calls_total", {"model": model})
    record_llm_call("llm.plan", model, NoUsage())
    assert _sample("llm_calls_total", {"model": model}) == calls_before + 1
    # No token samples minted for a provider that reports no usage.
    assert _sample("llm_tokens_total", {"model": model, "direction": "input"}) == 0.0


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "metrics_sweep_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await db_session_module.init_db()
    yield db_session_module
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_sla_sweep_updates_escalation_counter_and_pending_gauge(isolated_db):
    from app.agent.sla_sweep import run_sla_sweep

    now = dt.datetime.now(dt.timezone.utc)
    async with isolated_db.session_scope() as session:
        session.add(
            Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL)
        )
        # One overdue (escalates) and one comfortably within SLA (stays pending).
        session.add(
            Approval(
                id=1, ticket_id=1, tool_name="disable_user", tool_args={"username": "jsmith"},
                status=ApprovalStatus.PENDING, sla_deadline=now - dt.timedelta(minutes=1),
            )
        )
        session.add(
            Approval(
                id=2, ticket_id=1, tool_name="revoke_access", tool_args={"username": "jsmith"},
                status=ApprovalStatus.PENDING, sla_deadline=now + dt.timedelta(hours=2),
            )
        )

    escalated_before = _sample("approvals_escalated_total")
    await run_sla_sweep()

    assert _sample("approvals_escalated_total") == escalated_before + 1
    # Gauge reflects the post-sweep level: exactly one approval still PENDING.
    assert _sample("approvals_pending") == 1
