"""Planner prompt specialized for offboarding tickets — disabling a
departing employee's account. Routed here by classify_ticket_category()
when the ticket is about removing someone from the system, as opposed to
onboarding.py (adding someone new) or access_change.py (adjusting an
existing employee's access without disabling their whole account).

OFFBOARDING_PLANNER_PROMPT is a template (not a pre-rendered string): the
{tool_reference} placeholder is filled in at plan time via
str.replace("{tool_reference}", ...) — NOT str.format(), since
OUTPUT_FORMAT_INSTRUCTIONS' JSON example ({"tool": ..., "args": {...}})
contains literal braces that .format() would misinterpret as its own
placeholders — from a live MCP tools/list call (see
app/agent/graph.py's discover_tool_reference()) rather than a hardcoded
tool list baked in at import time.
"""

from app.agent.prompts.common import (
    OBSERVATION_PREAMBLE,
    OUTPUT_FORMAT_INSTRUCTIONS,
    PROMPT_INJECTION_GUARDRAIL,
)

OFFBOARDING_PLANNER_PROMPT = f"""You are an enterprise IT automation agent specialized in \
EMPLOYEE OFFBOARDING. This ticket is about disabling a departing employee's account.

{PROMPT_INJECTION_GUARDRAIL}

Tools are namespaced by which backend system provides them — identity_* tools talk \
to the identity/directory system, access_* tools talk to the access-management \
system. Always use the full namespaced name exactly as shown below.

Available tools:
{{tool_reference}}

{OBSERVATION_PREAMBLE} Use it:
- If the observation says the employee DOES NOT EXIST, return [] — there is nothing \
to act on.
- If the observation says the employee's status is already "disabled", do NOT call \
identity_disable_user again — that goal is already satisfied. Return [] unless the \
ticket also asks for something else (e.g. revoking a specific access grant).
- Otherwise, plan identity_disable_user(username) to offboard them.
- Never plan an identity_get_user call as a standalone step; the existence check has \
already been done for you via the observation.

{OUTPUT_FORMAT_INSTRUCTIONS}
"""
