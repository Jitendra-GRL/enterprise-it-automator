"""Tests for app/notifications/email.py — the optional plain-email
notification channel for pending sensitive-action approvals.

app.notifications.email._send is monkeypatched rather than hitting a real
SMTP server — these tests verify OUR reviewer-selection and best-effort
error-swallowing logic, not smtplib's own behavior.
"""

from app.config import get_settings
from app.db.models import ApprovalStatus


class _FakeReviewer:
    def __init__(self, username, email):
        self.username = username
        self.email = email


class _FakeApproval:
    id = 1
    ticket_id = 7
    tool_name = "disable_user"
    tool_args = {"username": "jsmith"}
    reasoning = "test"
    status = ApprovalStatus.PENDING


async def test_notify_reviewers_is_noop_without_smtp_host(monkeypatch):
    from app.notifications.email import notify_reviewers_of_pending_approval

    monkeypatch.delenv("SMTP_HOST", raising=False)
    get_settings.cache_clear()

    calls = []
    monkeypatch.setattr("app.notifications.email._send", lambda **k: calls.append(k))

    await notify_reviewers_of_pending_approval(_FakeApproval(), [_FakeReviewer("r", "r@example.com")])
    assert calls == []
    get_settings.cache_clear()


async def test_notify_reviewers_skips_reviewers_without_email(monkeypatch):
    from app.notifications.email import notify_reviewers_of_pending_approval

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    get_settings.cache_clear()

    calls = []
    monkeypatch.setattr("app.notifications.email._send", lambda **k: calls.append(k))

    reviewers = [
        _FakeReviewer("no-email-reviewer", None),
        _FakeReviewer("emailed-reviewer", "reviewer@example.com"),
    ]

    await notify_reviewers_of_pending_approval(_FakeApproval(), reviewers)

    assert len(calls) == 1
    assert calls[0]["to_address"] == "reviewer@example.com"
    assert "disable_user" in calls[0]["body"]
    assert "7" in calls[0]["subject"]
    get_settings.cache_clear()


async def test_notify_reviewers_swallows_send_failures(monkeypatch):
    """An SMTP failure must never propagate — it's a best-effort
    convenience layer on top of the real (dashboard/Telegram) approval flow."""
    from app.notifications.email import notify_reviewers_of_pending_approval

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    get_settings.cache_clear()

    def _boom(**k):
        raise ConnectionError("network blip")

    monkeypatch.setattr("app.notifications.email._send", _boom)

    await notify_reviewers_of_pending_approval(
        _FakeApproval(), [_FakeReviewer("linked-reviewer", "reviewer@example.com")]
    )  # must not raise
    get_settings.cache_clear()
