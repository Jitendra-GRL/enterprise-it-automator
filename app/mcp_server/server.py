"""Custom MCP server exposing enterprise IT-provisioning tools over stdio.

Standardizes tool exposure for the agent: every sensitive mutation (disable_user,
revoke_access) requires a pre-approved `approval_id` minted by the FastAPI HITL
flow, enforced server-side in approval_gate.require_approval — the LLM cannot
talk its way past this by claiming an action is authorized.

Run directly for local stdio testing:
    python -m app.mcp_server.server
Or launch it as a subprocess via an MCP client (see app/agent/mcp_client.py).
"""

from mcp.server.fastmcp import FastMCP

from app.db.session import init_db, session_scope
from app.mcp_server import tools as t
from app.mcp_server.approval_gate import require_approval
from app.mcp_server.tools import ToolError, is_sensitive

mcp = FastMCP("enterprise-it-automator")


@mcp.tool()
async def get_user(username: str) -> dict:
    """Look up an employee's identity record: status, department, and access grants."""
    async with session_scope() as session:
        return await t.get_user(session, username)


@mcp.tool()
async def create_user(
    username: str, full_name: str, email: str, department: str = "", ticket_id: int | None = None
) -> dict:
    """Provision a new employee identity (onboarding). Not a sensitive action."""
    async with session_scope() as session:
        return await t.create_user(
            session, username, full_name, email, department, actor="mcp-client", ticket_id=ticket_id
        )


@mcp.tool()
async def grant_access(
    username: str, resource: str, ticket_id: int | None = None
) -> dict:
    """Grant an employee access to a resource (e.g. 'github:engineering'). Not sensitive."""
    async with session_scope() as session:
        return await t.grant_access(
            session, username, resource, actor="mcp-client", ticket_id=ticket_id
        )


@mcp.tool()
async def disable_user(
    username: str, approval_id: int, ticket_id: int | None = None
) -> dict:
    """Disable an employee's account (offboarding). SENSITIVE: requires a prior
    human-approved `approval_id` (see request_approval / the FastAPI /approvals
    endpoints) matching this exact tool call, or the server refuses the action.
    """
    async with session_scope() as session:
        await require_approval(
            session, approval_id, "disable_user", {"username": username}
        )
        return await t.disable_user(
            session, username, actor="mcp-client", ticket_id=ticket_id
        )


@mcp.tool()
async def revoke_access(
    username: str, resource: str, approval_id: int, ticket_id: int | None = None
) -> dict:
    """Revoke an employee's access to a resource. SENSITIVE: requires a prior
    human-approved `approval_id` matching this exact tool call, or the server
    refuses the action.
    """
    async with session_scope() as session:
        await require_approval(
            session, approval_id, "revoke_access", {"username": username, "resource": resource}
        )
        return await t.revoke_access(
            session, username, resource, actor="mcp-client", ticket_id=ticket_id
        )


@mcp.tool()
def is_sensitive_action(tool_name: str) -> bool:
    """Report whether a tool name requires human approval before execution."""
    return is_sensitive(tool_name)


async def _bootstrap() -> None:
    await init_db()


def main() -> None:
    import asyncio

    asyncio.run(_bootstrap())
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
