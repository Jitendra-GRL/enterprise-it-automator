"""MCP resources: read-only, app-controlled data the gateway exposes
alongside its tools (identity_*, access_*, ticketing_*).

Per the MCP spec, resources and tools serve different purposes even when
backed by the same data: a tool is model-controlled (the LLM decides when to
invoke get_user), while a resource is app/user-controlled (a human operator
attaches it to context via their client's UI, or a client enumerates it to
build a picker) — see /docs/learn/server-concepts. Before this module, every
piece of data this gateway exposed was only reachable by calling a tool;
these resources let any MCP client (not just this project's own LangGraph
agent, which never needed resources since it drives everything via planned
tool calls) browse the audit trail or employee directory directly.

Registered on the gateway `mcp` instance directly in server.py (not
namespaced per-domain like tools) — these are cross-cutting read views over
the same database the three domain servers share, not something that would
ever run as its own separate process the way a domain's tools might (see
registry.py's docstring for that distinction).
"""

import json

from sqlalchemy import select

from app.db.models import AuditLog, EmployeeUser
from app.db.session import session_scope

# Caps how much a single resource read returns — these are meant for a human
# operator or client UI to browse, not a bulk export; an unbounded SELECT *
# against a real company's employee_users/audit_log table would be a
# multi-thousand-row response with no pagination support (FastMCP resources
# don't paginate the way tools/list can with a cursor).
_RECENT_AUDIT_LIMIT = 50


def register_resources(mcp) -> None:
    """Registers every resource/resource-template on the given FastMCP
    gateway instance. Called once from server.py's module body, mirroring
    how identity_server.py etc. register their tools via decorators at
    import time — resources use the same @mcp.resource() decorator pattern,
    just called imperatively here so this module doesn't need its own
    FastMCP instance (unlike the domain tool servers, there's nothing to
    compose across processes for these).
    """

    @mcp.resource(
        "directory://employees",
        name="employee_directory",
        title="Employee directory",
        description=(
            "Every employee identity record known to this system: username, "
            "full name, department, status (active/disabled), and current "
            "access grants. The same underlying data identity_get_user "
            "returns per-employee, browsable here as a full listing."
        ),
        mime_type="application/json",
    )
    async def employee_directory() -> str:
        async with session_scope() as session:
            users = (await session.scalars(select(EmployeeUser))).all()
            return json.dumps(
                [
                    {
                        "username": u.username,
                        "full_name": u.full_name,
                        "email": u.email,
                        "department": u.department,
                        "status": u.status.value,
                        "access_grants": u.access_grants,
                    }
                    for u in users
                ],
                indent=2,
            )

    @mcp.resource(
        "audit://log/recent",
        name="recent_audit_log",
        title="Recent audit log entries",
        description=(
            f"The {_RECENT_AUDIT_LIMIT} most recent entries from the "
            "immutable audit trail of every tool invocation this gateway "
            "has executed (successful or rejected) — actor, tool name, "
            "arguments, and outcome. For a single ticket's full trail, use "
            "the audit://ticket/{ticket_id} resource template instead."
        ),
        mime_type="application/json",
    )
    async def recent_audit_log() -> str:
        async with session_scope() as session:
            # DESC + limit to get the N most recent rows, then reversed back
            # to chronological order for a human reading the JSON top-to-bottom.
            entries = (
                await session.scalars(
                    select(AuditLog).order_by(AuditLog.id.desc()).limit(_RECENT_AUDIT_LIMIT)
                )
            ).all()
            return json.dumps(
                [_audit_entry_dict(e) for e in reversed(list(entries))],
                indent=2,
            )

    @mcp.resource(
        "audit://ticket/{ticket_id}",
        name="ticket_audit_trail",
        title="Audit trail for a specific ticket",
        description=(
            "Every audited tool-call attempt (successful or rejected) "
            "recorded against a specific ticket_id, in chronological order — "
            "the complete record of what the agent tried to do while "
            "processing that ticket."
        ),
        mime_type="application/json",
    )
    async def ticket_audit_trail(ticket_id: str) -> str:
        async with session_scope() as session:
            entries = (
                await session.scalars(
                    select(AuditLog)
                    .where(AuditLog.ticket_id == int(ticket_id))
                    .order_by(AuditLog.id.asc())
                )
            ).all()
            return json.dumps([_audit_entry_dict(e) for e in entries], indent=2)


def _audit_entry_dict(entry: AuditLog) -> dict:
    return {
        "id": entry.id,
        "ticket_id": entry.ticket_id,
        "actor": entry.actor,
        "tool_name": entry.tool_name,
        "tool_args": entry.tool_args,
        "result": entry.result,
        "success": entry.success,
        "created_at": entry.created_at.isoformat(),
    }
