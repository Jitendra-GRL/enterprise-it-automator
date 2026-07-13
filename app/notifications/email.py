"""Optional plain-email notifications for pending sensitive-action approvals
(Settings.smtp_host — see app/config.py's smtp_* settings).

Deliberately the simplest possible notification channel: no linking flow (a
reviewer's email is just set directly on their Reviewer row, out-of-band),
no reply-to-decide mechanism (replying to an email isn't a safe way to
authenticate a decision — deciding an approval always still requires the
real X-Reviewer-Token via the dashboard or Telegram). This module only ever
tells a reviewer "go look," never lets them act from the email itself.

Fully additive and opt-in: with smtp_host unset, notify_reviewers_of_pending_approval
is a no-op and every existing approval flow (dashboard, Telegram) is
completely unaffected.
"""

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings
from app.db.models import Approval, Reviewer

logger = logging.getLogger(__name__)


def _approval_email_body(approval: Approval) -> str:
    username = approval.tool_args.get("username", "")
    lines = [
        f"A sensitive action needs your approval — ticket #{approval.ticket_id}.",
        "",
        f"Action: {approval.tool_name}",
    ]
    if username:
        lines.append(f"Target: {username}")
    if approval.reasoning:
        lines.append(f"Reasoning: {approval.reasoning}")
    lines.append("")
    lines.append("Decide it from the dashboard (or Telegram, if linked).")
    return "\n".join(lines)


def _send(*, to_address: str, subject: str, body: str) -> None:
    """Synchronous send via smtplib — run off the event loop by callers
    (notify_reviewers_of_pending_approval below), since smtplib has no async
    API and blocking the loop for a real network call would stall the
    approval flow it's just a best-effort notification for.
    """
    settings = get_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from_address_or_default
    msg["To"] = to_address
    msg.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)


async def notify_reviewers_of_pending_approval(approval: Approval, reviewers: list[Reviewer]) -> None:
    """Emails every reviewer in `reviewers` who has an email set — silently
    skips anyone who doesn't. Caller (app/agent/graph.py's
    _notify_reviewers_if_configured) is responsible for resolving
    `reviewers` via app.api.rbac.find_entitled_reviewers first.

    Best-effort: an SMTP failure (bad credentials, network blip) is logged
    and swallowed, never allowed to fail the ticket run itself — same
    contract as app/notifications/telegram.py's equivalent function.
    """
    settings = get_settings()
    if not settings.smtp_host:
        return

    addressed = [r for r in reviewers if r.email]
    if not addressed:
        return

    import asyncio

    subject = f"Approval needed — ticket #{approval.ticket_id}"
    body = _approval_email_body(approval)

    for reviewer in addressed:
        if not reviewer.email:  # addressed already filters, but narrows the Optional for typing
            continue
        try:
            await asyncio.to_thread(_send, to_address=reviewer.email, subject=subject, body=body)
        except Exception:
            logger.exception(
                "Failed to send email approval notification to reviewer %s (approval %d)",
                reviewer.username, approval.id,
            )
