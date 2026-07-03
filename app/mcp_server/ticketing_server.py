"""Ticketing domain: posting status updates back to an external
ticketing/ITSM system (Jira, ServiceNow, etc.).

This is explicitly a SIMULATION — a real deployment would have this server
call out to Jira's/ServiceNow's actual REST API. The tool contract (post a
comment, look up current status) is representative of what a real
ticket-sync integration looks like, and lives in its own domain server on
purpose: an orchestration agent legitimately needs to update the *system of
record for the ticket itself*, not just the identity/access backends,
mirroring how real IT environments separate "the ticketing tool" from "the
directory service" as different systems entirely.

Standalone FastMCP instance so this domain could run as its own process
with its own deploy/scale profile — see server.py, which composes this
alongside identity_server and access_server under one gateway process for
local/dev use via add_tool() namespacing.
"""

from mcp.server.fastmcp import FastMCP

from app.db.session import session_scope
from app.mcp_server import tools as t

ticketing_mcp = FastMCP("ticketing")


@ticketing_mcp.tool()
async def add_ticket_comment(ticket_id: int, comment: str) -> dict:
    """Post a status comment back to the external ticketing system for this
    ticket. Not sensitive — informational only, doesn't mutate identity or
    access state."""
    async with session_scope() as session:
        return await t.add_ticket_comment(session, ticket_id, comment)


@ticketing_mcp.tool()
async def get_ticket_status(ticket_id: int) -> dict:
    """Look up the current status of a ticket in the external ticketing
    system (simulated: reflects this app's own Ticket record, which is
    where a real sync job would have last written it)."""
    async with session_scope() as session:
        return await t.get_ticket_status(session, ticket_id)
