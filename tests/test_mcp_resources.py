"""Tests for the gateway's MCP resources (app/mcp_server/resources.py) —
audit://log/recent, audit://ticket/{ticket_id}, and directory://employees.

Follows the same DB-reset pattern as test_domain_server_composition.py:
resources.py reads through app.db.session's module-level engine/session
factory singleton (not the isolated `session` fixture in conftest.py), so
each test points that singleton at a fresh in-memory SQLite DB via
DATABASE_URL before bootstrapping.
"""

import json

from app.mcp_server.server import _bootstrap, mcp


def _reset_db(monkeypatch):
    from app.config import get_settings
    from app.db import session as db_session_module

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None


async def test_gateway_lists_expected_resources(monkeypatch):
    _reset_db(monkeypatch)
    await _bootstrap()

    resources = await mcp.list_resources()
    uris = {str(r.uri) for r in resources}
    assert "directory://employees" in uris
    assert "audit://log/recent" in uris

    templates = await mcp.list_resource_templates()
    template_uris = {t.uriTemplate for t in templates}
    assert "audit://ticket/{ticket_id}" in template_uris


async def test_employee_directory_resource_reflects_created_users(monkeypatch):
    _reset_db(monkeypatch)
    await _bootstrap()

    from app.mcp_server import tools as t
    from app.db.session import session_scope

    async with session_scope() as session:
        await t.create_user(session, "dresource", "Dana Resource", "d@example.com", "Engineering")

    [result] = await mcp.read_resource("directory://employees")
    payload = json.loads(result.content)
    usernames = {row["username"] for row in payload}
    assert "dresource" in usernames


async def test_recent_audit_log_resource_returns_chronological_entries(monkeypatch):
    _reset_db(monkeypatch)
    await _bootstrap()

    from app.mcp_server import tools as t
    from app.db.session import session_scope

    async with session_scope() as session:
        await t.create_user(session, "auser1", "A One", "a1@example.com")
    async with session_scope() as session:
        await t.create_user(session, "auser2", "A Two", "a2@example.com")

    [result] = await mcp.read_resource("audit://log/recent")
    payload = json.loads(result.content)
    assert len(payload) >= 2
    # Chronological (oldest first): auser1's create_user entry precedes auser2's.
    tool_args_in_order = [e["tool_args"].get("username") for e in payload if e["tool_name"] == "create_user"]
    assert tool_args_in_order.index("auser1") < tool_args_in_order.index("auser2")


async def test_ticket_audit_trail_resource_template_scopes_to_one_ticket(monkeypatch):
    _reset_db(monkeypatch)
    await _bootstrap()

    from app.mcp_server import tools as t
    from app.db.session import session_scope
    from app.db.models import Ticket

    async with session_scope() as session:
        ticket = Ticket(requester="r@example.com", subject="Onboard", body="...")
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id
        await t.create_user(
            session, "tuser", "T User", "t@example.com", ticket_id=ticket_id
        )

    async with session_scope() as session:
        # A second, unrelated ticket's audit entry must NOT leak into the first's trail.
        other_ticket = Ticket(requester="r2@example.com", subject="Offboard", body="...")
        session.add(other_ticket)
        await session.flush()
        await t.create_user(
            session, "otheruser", "Other User", "o@example.com", ticket_id=other_ticket.id
        )

    [result] = await mcp.read_resource(f"audit://ticket/{ticket_id}")
    payload = json.loads(result.content)
    assert len(payload) == 1
    assert payload[0]["tool_args"]["username"] == "tuser"
