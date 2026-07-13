"""Tests for cross-replica mutual exclusion on the SLA sweep
(app/db/session.py's try_advisory_lock + app/agent/sla_sweep.py's guarded
run_sla_sweep).

The Postgres advisory-lock protocol itself is exercised against a FAKE
engine/connection that records the SQL — a real multi-replica Postgres race
can't run in CI's SQLite world, but the protocol (try-lock, guarded body,
unconditional unlock on the SAME connection, no unlock when never acquired)
is exactly the part worth pinning down; the live behavior rides on
Postgres's own advisory-lock semantics, which don't need re-testing here.
"""

import datetime as dt
from contextlib import asynccontextmanager

import pytest

from app.config import get_settings
from app.db import session as db_session_module
from app.db.models import Approval, ApprovalStatus, Ticket, TicketStatus
from app.db.session import try_advisory_lock


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "replica_safety_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await db_session_module.init_db()
    yield db_session_module
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_sqlite_lock_is_noop_true(isolated_db):
    """On SQLite (single-process topology) the lock always grants — the
    sweep must behave byte-for-byte as it did before locking existed.
    """
    async with try_advisory_lock(12345) as acquired:
        assert acquired is True


async def test_sweep_runs_normally_on_sqlite(isolated_db):
    from app.agent.sla_sweep import run_sla_sweep

    now = dt.datetime.now(dt.timezone.utc)
    async with isolated_db.session_scope() as session:
        session.add(
            Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL)
        )
        session.add(
            Approval(
                id=1, ticket_id=1, tool_name="disable_user", tool_args={"username": "jsmith"},
                status=ApprovalStatus.PENDING, sla_deadline=now - dt.timedelta(minutes=1),
            )
        )

    result = await run_sla_sweep()
    assert result["skipped"] is False
    assert result["escalated_approvals"] == [1]


async def test_sweep_skips_when_lock_held_elsewhere(isolated_db, monkeypatch):
    """When another replica holds the lock, the pass is skipped whole: no
    escalations, no audit rows, and the result says so.
    """
    import app.agent.sla_sweep as sweep_module

    @asynccontextmanager
    async def lock_held_elsewhere(lock_id: int):
        yield False

    monkeypatch.setattr(sweep_module, "try_advisory_lock", lock_held_elsewhere)

    now = dt.datetime.now(dt.timezone.utc)
    async with isolated_db.session_scope() as session:
        session.add(
            Ticket(id=1, requester="hr@x.com", subject="s", body="b", status=TicketStatus.AWAITING_APPROVAL)
        )
        session.add(
            Approval(
                id=1, ticket_id=1, tool_name="disable_user", tool_args={"username": "jsmith"},
                status=ApprovalStatus.PENDING, sla_deadline=now - dt.timedelta(minutes=1),
            )
        )

    result = await sweep_module.run_sla_sweep()
    assert result == {"escalated_approvals": [], "stuck_tickets": [], "skipped": True}

    async with isolated_db.session_scope() as session:
        approval = await session.get(Approval, 1)
        # Untouched — the OTHER replica's sweep owns this pass.
        assert approval.status == ApprovalStatus.PENDING


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    """Records advisory-lock SQL statements issued on this 'connection'."""

    def __init__(self, grant: bool, log: list):
        self._grant = grant
        self.log = log

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.log.append((sql, dict(params or {})))
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(self._grant)
        return _FakeResult(None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, grant: bool):
        self.log: list = []
        self._grant = grant

        class _Dialect:
            name = "postgresql"

        self.dialect = _Dialect()

    def connect(self):
        return _FakeConn(self._grant, self.log)


async def test_postgres_protocol_unlocks_on_same_connection(monkeypatch):
    engine = _FakeEngine(grant=True)
    monkeypatch.setattr(db_session_module, "get_engine", lambda: engine)

    async with try_advisory_lock(74301) as acquired:
        assert acquired is True

    sqls = [sql for sql, _ in engine.log]
    assert any("pg_try_advisory_lock" in s for s in sqls)
    assert any("pg_advisory_unlock" in s for s in sqls)
    # Both statements used the same lock id.
    assert all(params.get("id") == 74301 for _, params in engine.log)


async def test_postgres_protocol_unlocks_even_when_body_raises(monkeypatch):
    engine = _FakeEngine(grant=True)
    monkeypatch.setattr(db_session_module, "get_engine", lambda: engine)

    with pytest.raises(RuntimeError):
        async with try_advisory_lock(74301):
            raise RuntimeError("sweep pass blew up")

    assert any("pg_advisory_unlock" in sql for sql, _ in engine.log)


async def test_postgres_protocol_never_unlocks_a_lock_it_did_not_get(monkeypatch):
    """Unlocking a lock held by ANOTHER session would release their lock
    out from under them (Postgres advisory unlock is by key, not owner-
    checked within a session) — the not-acquired path must not emit it.
    """
    engine = _FakeEngine(grant=False)
    monkeypatch.setattr(db_session_module, "get_engine", lambda: engine)

    async with try_advisory_lock(74301) as acquired:
        assert acquired is False

    sqls = [sql for sql, _ in engine.log]
    assert any("pg_try_advisory_lock" in s for s in sqls)
    assert not any("pg_advisory_unlock" in s for s in sqls)
