"""CI half of the golden-ticket eval suite (see evals/__init__.py): every
golden ticket's RECORDED model outputs, replayed through the real graph,
must score a full pass — classifier parsing, username extraction, planner
JSON guardrails, sensitivity gating, and injection-refusal all pinned.

If this fails after a prompt/parser/policy change, either the change broke
the pipeline contract (fix the change) or it deliberately moved the
contract (re-record the affected golden ticket AND run the live eval —
`python -m evals.run_live` — to confirm a real model still meets it).
"""

import pytest

from app.config import get_settings
from app.db import session as db_session_module
from evals.golden import GOLDEN_TICKETS
from evals.runner import ScriptedLLM, evaluate


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    """Gated golden tickets write real Approval rows through
    await_approval_node — keep them off the developer's data/ files."""
    db_path = tmp_path / "golden_tickets_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    # Pin the app's DEFAULT sensitive-action policy: the golden contract
    # tests what ships, not whatever a developer's local .env happens to
    # override (found live: a stale local .env without enable_user made the
    # re-onboarding ticket skip its gate and fail this suite).
    monkeypatch.setenv(
        "SENSITIVE_ACTIONS", "disable_user,enable_user,revoke_access,create_user,grant_access"
    )
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await db_session_module.init_db()
    yield
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_recorded_golden_tickets_all_pass(isolated_db):
    report = await evaluate(lambda ticket: ScriptedLLM(ticket["recorded"]))
    assert report.passed == report.total, "\n" + "\n".join(report.summary_lines())


async def test_golden_set_covers_all_categories():
    """The eval set itself must keep covering every ticket category the
    supervisor can route to — shrinking coverage should be a deliberate,
    visible act, not drift."""
    covered = {t["expected_category"] for t in GOLDEN_TICKETS}
    assert covered == {"ONBOARDING", "OFFBOARDING", "ACCESS_CHANGE"}


async def test_golden_set_pins_injection_resistance():
    names = {t["name"] for t in GOLDEN_TICKETS}
    assert "prompt-injection-resisted" in names
    injection = next(t for t in GOLDEN_TICKETS if t["name"] == "prompt-injection-resisted")
    assert "identity_disable_user" in injection["forbidden_tools"]
