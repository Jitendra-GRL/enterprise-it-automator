import pytest

from app.db.models import Approval, ApprovalStatus
from app.mcp_server.approval_gate import find_approved, require_approval
from app.mcp_server.tools import ToolError


async def _make_approval(session, status, tool_name="disable_user", tool_args=None, ticket_id=1):
    approval = Approval(
        ticket_id=ticket_id,
        tool_name=tool_name,
        tool_args=tool_args or {"username": "asmith"},
        status=status,
    )
    session.add(approval)
    await session.flush()
    return approval


async def test_require_approval_passes_when_approved(session):
    approval = await _make_approval(session, ApprovalStatus.APPROVED)
    result = await require_approval(session, approval.id, "disable_user", {"username": "asmith"})
    assert result.id == approval.id


async def test_require_approval_rejects_when_pending(session):
    approval = await _make_approval(session, ApprovalStatus.PENDING)
    with pytest.raises(ToolError, match="pending, not approved"):
        await require_approval(session, approval.id, "disable_user", {"username": "asmith"})


async def test_require_approval_rejects_when_rejected(session):
    approval = await _make_approval(session, ApprovalStatus.REJECTED)
    with pytest.raises(ToolError, match="rejected, not approved"):
        await require_approval(session, approval.id, "disable_user", {"username": "asmith"})


async def test_require_approval_rejects_unknown_id(session):
    with pytest.raises(ToolError, match="Unknown approval_id"):
        await require_approval(session, 999, "disable_user", {"username": "asmith"})


async def test_require_approval_rejects_tool_mismatch(session):
    """An approval minted for disable_user must not authorize revoke_access —
    the LLM cannot repurpose one human sign-off for a different sensitive action."""
    approval = await _make_approval(session, ApprovalStatus.APPROVED, tool_name="disable_user")
    with pytest.raises(ToolError, match="was granted for tool"):
        await require_approval(session, approval.id, "revoke_access", {"username": "asmith"})


async def test_require_approval_rejects_args_mismatch(session):
    """An approval for one username must not authorize the same action on a different user."""
    approval = await _make_approval(
        session, ApprovalStatus.APPROVED, tool_args={"username": "asmith"}
    )
    with pytest.raises(ToolError, match="arguments do not match"):
        await require_approval(session, approval.id, "disable_user", {"username": "bwayne"})


async def test_require_approval_marks_executed_on_success(session):
    approval = await _make_approval(session, ApprovalStatus.APPROVED)
    assert approval.executed_at is None

    await require_approval(session, approval.id, "disable_user", {"username": "asmith"})

    assert approval.executed_at is not None


async def test_require_approval_rejects_second_use_of_same_approval(session):
    """One human sign-off must authorize exactly one execution — replaying
    the same approval_id for a second call (e.g. an attacker who can call
    the MCP tool directly, bypassing the FastAPI layer entirely) must be
    refused, not silently re-executed."""
    approval = await _make_approval(session, ApprovalStatus.APPROVED)

    await require_approval(session, approval.id, "disable_user", {"username": "asmith"})

    with pytest.raises(ToolError, match="already used"):
        await require_approval(session, approval.id, "disable_user", {"username": "asmith"})


async def test_find_approved_returns_matching_approval(session):
    approval = await _make_approval(
        session, ApprovalStatus.APPROVED, tool_args={"username": "asmith", "resource": "vpn"},
        tool_name="revoke_access", ticket_id=7,
    )
    found = await find_approved(session, 7, "revoke_access", {"username": "asmith", "resource": "vpn"})
    assert found is not None
    assert found.id == approval.id


async def test_find_approved_returns_none_when_not_approved(session):
    await _make_approval(session, ApprovalStatus.PENDING, ticket_id=7)
    found = await find_approved(session, 7, "disable_user", {"username": "asmith"})
    assert found is None


async def test_find_approved_returns_none_for_different_ticket(session):
    await _make_approval(session, ApprovalStatus.APPROVED, ticket_id=7)
    found = await find_approved(session, 8, "disable_user", {"username": "asmith"})
    assert found is None
