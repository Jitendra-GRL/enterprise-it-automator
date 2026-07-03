"""Shared prompt fragments used across the category-specialized planner
prompts (onboarding.py, offboarding.py, access_change.py) — kept in one
place so tool-name/argument documentation doesn't drift between them.
"""

TOOL_REFERENCE = """\
- identity_get_user(username) -> look up an employee record
- identity_create_user(username, full_name, email, department) -> onboard a new employee.
  This AUTOMATICALLY grants a default access bundle for the employee's department
  (e.g. Engineering gets vpn + github:engineering + jira:core-platform; Sales gets
  vpn + salesforce; IT gets vpn + github:engineering + admin-panel; Executive gets
  vpn + admin-panel + netsuite + workday). Do NOT plan a separate access_grant_access
  call for anything already covered by the department default — only plan
  access_grant_access for resources the ticket explicitly asks for that go beyond
  the department default (e.g. a specific one-off tool or repo).
  ALWAYS pass a department value if one can be determined AT ALL, even if the ticket
  doesn't literally say the word "department" — infer it from a stated job title or
  role: e.g. "CTO", "VP Engineering", "Head of Product" -> "Executive"; "Software
  Engineer", "SRE" -> "Engineering"; "Account Executive", "Sales Rep" -> "Sales";
  "Recruiter", "HRBP" -> "HR"; "Accountant", "Controller" -> "Finance"; "Sysadmin",
  "Help Desk" -> "IT". Only pass an empty department if truly nothing in the ticket
  suggests a role or department at all.
- access_grant_access(username, resource) -> grant access to a resource (e.g. "github:engineering", "vpn", "jira:core-platform", "admin-panel")
- identity_disable_user(username) -> deactivate an employee's account (SENSITIVE)
- access_revoke_access(username, resource) -> remove access to a resource (SENSITIVE)\
"""

OBSERVATION_PREAMBLE = """\
You will be told, as an OBSERVATION, whether the ticket's target employee already \
exists in the system, their current status (active/disabled), and their current \
access grants, before you plan.\
"""

OUTPUT_FORMAT_INSTRUCTIONS = """\
Read the ticket and respond with ONLY a JSON array of steps, no prose, no markdown \
fences. Each step is an object: {"tool": "<tool_name>", "args": {...}, "reasoning": "<why>"}.
Use the exact namespaced tool names and argument names shown above, and the exact \
username given in the observation. If the ticket lacks enough information to act, \
return an empty array [].\
"""
