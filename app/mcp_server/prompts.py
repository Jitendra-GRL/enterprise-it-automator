"""MCP prompts: reusable templates any MCP client can surface to a user for
drafting a well-formed IT ticket (onboarding / offboarding / access-change),
matching the three categories app/agent/graph.py's classify_ticket_category()
routes on.

These are NOT the internal LLM planner prompts (app/agent/prompts/*.py) —
those are rendered server-side with a live {tool_reference} substitution and
prompt-injection guardrails specific to this app's own LangGraph agent, and
would be meaningless (or actively confusing) handed to an arbitrary MCP
client. Per the MCP spec, a *prompt* is a user-controlled template exposed
for a human (or client UI) to select and fill in before sending — these
produce a ticket `subject`/`body` shaped so that, once POSTed to this app's
own /tickets endpoint, the internal planner prompts above have exactly the
structured information (username, full name, department, resource, etc.)
they expect from a ticket body.
"""

from mcp.server.fastmcp.prompts.base import Message, UserMessage


def register_prompts(mcp) -> None:
    """Registers every prompt on the given FastMCP gateway instance. Called
    once from server.py's module body — same imperative-registration
    approach as resources.py's register_resources(), for the same reason:
    these aren't domain-specific enough to warrant their own FastMCP
    instance the way identity/access/ticketing's tools do.
    """

    @mcp.prompt(
        name="draft_onboarding_ticket",
        title="Draft an onboarding ticket",
        description=(
            "Produces a ticket subject/body for bringing a new employee into "
            "the system — account creation plus any extra access beyond "
            "their department's default bundle."
        ),
    )
    def draft_onboarding_ticket(
        full_name: str,
        username: str,
        email: str,
        department: str = "",
        extra_access: str = "",
    ) -> list[Message]:
        lines = [
            f"Please onboard new employee {full_name} (username: {username}, email: {email}).",
        ]
        if department:
            lines.append(f"Department / role: {department}.")
        if extra_access:
            lines.append(f"They additionally need access to: {extra_access}.")
        body = " ".join(lines)
        return [
            UserMessage(
                f"Draft an IT onboarding ticket with subject "
                f'"Onboard {full_name}" and body:\n\n{body}'
            )
        ]

    @mcp.prompt(
        name="draft_offboarding_ticket",
        title="Draft an offboarding ticket",
        description=(
            "Produces a ticket subject/body for disabling a departing "
            "employee's account."
        ),
    )
    def draft_offboarding_ticket(username: str, reason: str = "") -> list[Message]:
        body = f"Please offboard employee {username} — disable their account."
        if reason:
            body += f" Reason: {reason}."
        return [
            UserMessage(
                f'Draft an IT offboarding ticket with subject "Offboard {username}" '
                f"and body:\n\n{body}"
            )
        ]

    @mcp.prompt(
        name="draft_access_change_ticket",
        title="Draft an access-change ticket",
        description=(
            "Produces a ticket subject/body for granting or revoking a "
            "specific resource for an existing, still-employed employee."
        ),
    )
    def draft_access_change_ticket(
        username: str, resource: str, action: str = "grant"
    ) -> list[Message]:
        verb = "grant" if action.strip().lower() != "revoke" else "revoke"
        body = f"Please {verb} {resource} access for employee {username}."
        return [
            UserMessage(
                f'Draft an IT access-change ticket with subject '
                f'"{verb.capitalize()} {resource} for {username}" and body:\n\n{body}'
            )
        ]
