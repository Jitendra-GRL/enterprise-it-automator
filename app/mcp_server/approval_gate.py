"""Server-side enforcement of human-in-the-loop approval for sensitive tools.

The agent cannot simply decide an action is "approved" client-side — the MCP
server itself refuses to execute a sensitive tool unless the caller presents
the id of an Approval row that a human has actually marked APPROVED in the
database. This is what makes HITL a real security boundary rather than a
prompt-level suggestion.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, ApprovalStatus
from app.mcp_server.tools import ToolError


async def require_approval(
    session: AsyncSession, approval_id: int, tool_name: str, tool_args: dict
) -> Approval:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise ToolError(f"Unknown approval_id: {approval_id}")
    if approval.status != ApprovalStatus.APPROVED:
        raise ToolError(
            f"Approval {approval_id} is {approval.status.value}, not approved — "
            "sensitive action refused."
        )
    if approval.tool_name != tool_name:
        raise ToolError(
            f"Approval {approval_id} was granted for tool {approval.tool_name!r}, "
            f"not {tool_name!r} — refusing to reuse it for a different action."
        )
    if approval.tool_args != tool_args:
        raise ToolError(
            f"Approval {approval_id} arguments do not match the requested call — "
            "refusing to reuse it for different arguments."
        )
    return approval


async def find_approved(
    session: AsyncSession, ticket_id: int, tool_name: str, tool_args: dict
) -> Approval | None:
    """Convenience lookup used by the agent to find an already-approved gate."""
    result = await session.scalars(
        select(Approval).where(
            Approval.ticket_id == ticket_id,
            Approval.tool_name == tool_name,
            Approval.status == ApprovalStatus.APPROVED,
        )
    )
    for approval in result:
        if approval.tool_args == tool_args:
            return approval
    return None
