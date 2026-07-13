"""Tests for the gateway's MCP prompts (app/mcp_server/prompts.py) —
reusable ticket-drafting templates, distinct from the internal LLM planner
prompts in app/agent/prompts/*.py (see prompts.py's module docstring)."""

from app.mcp_server.server import mcp


async def test_gateway_lists_expected_prompts():
    prompts = await mcp.list_prompts()
    names = {p.name for p in prompts}
    assert {
        "draft_onboarding_ticket",
        "draft_offboarding_ticket",
        "draft_access_change_ticket",
    } <= names


async def test_draft_onboarding_ticket_includes_department_and_extra_access():
    result = await mcp.get_prompt(
        "draft_onboarding_ticket",
        {
            "full_name": "Alice Smith",
            "username": "asmith",
            "email": "asmith@example.com",
            "department": "Engineering",
            "extra_access": "figma",
        },
    )
    text = result.messages[0].content.text
    assert "Alice Smith" in text
    assert "asmith" in text
    assert "Engineering" in text
    assert "figma" in text


async def test_draft_offboarding_ticket_omits_reason_when_not_given():
    result = await mcp.get_prompt("draft_offboarding_ticket", {"username": "bjones"})
    text = result.messages[0].content.text
    assert "bjones" in text
    assert "Reason" not in text


async def test_draft_access_change_ticket_defaults_to_grant():
    result = await mcp.get_prompt(
        "draft_access_change_ticket", {"username": "cwu", "resource": "github:engineering"}
    )
    text = result.messages[0].content.text
    assert "grant" in text.lower()
    assert "cwu" in text
    assert "github:engineering" in text


async def test_draft_access_change_ticket_revoke_action():
    result = await mcp.get_prompt(
        "draft_access_change_ticket",
        {"username": "cwu", "resource": "admin-panel", "action": "revoke"},
    )
    text = result.messages[0].content.text
    assert "revoke" in text.lower()
