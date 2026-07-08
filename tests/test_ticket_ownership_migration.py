"""Regression test for a real bug found live on the public demo deployment:
Ticket.submitted_by_client_id is a NEW column added after the tickets table
already existed on a live database — SQLAlchemy's create_all only creates
MISSING TABLES, never alters an existing table to add a newly-modeled
column, so without a self-healing migration step every read/write touching
this column would crash with "column does not exist" against any database
that already had a tickets table before this column was introduced.

Also covers the actual security fix this column enables: ticket/audit/
approval read-scoping (app/api/main.py) previously compared
`Ticket.requester == client.name` — free-text the caller controls in the
request body, unrelated to which credential authenticated the call. On the
live public demo, the "public-demo-guest" ApiClient submitted a ticket with
requester="scoping-check@example.com" and then could NOT see its own
just-submitted ticket (name mismatch), while the underlying design also
meant nothing stopped a DIFFERENT client from seeing that ticket by simply
submitting with a matching requester string. submitted_by_client_id is the
real ownership link; requester stays purely descriptive.
"""

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db import session as db_session_module
from app.db.models import Ticket, TicketStatus


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "ticket_ownership_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    yield db_path
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


async def test_init_db_on_fresh_database_creates_column_via_create_all(isolated_db):
    """The common case — a brand-new database has never had a tickets table
    at all, so create_all alone creates it correctly, WITH the column, and
    the self-healing step is a no-op."""
    await db_session_module.init_db()

    engine = db_session_module.get_engine()

    def _columns(sync_conn):
        return {c["name"] for c in sa_inspect(sync_conn).get_columns("tickets")}

    async with engine.connect() as conn:
        columns = await conn.run_sync(_columns)

    assert "submitted_by_client_id" in columns


async def test_init_db_self_heals_an_existing_table_missing_the_column(isolated_db):
    """The actual bug scenario: a tickets table that predates this column
    (simulated here by creating the OLD schema shape directly, bypassing
    the current model — exactly what a live database that already had
    tickets before this column was introduced looks like)."""
    db_path = isolated_db
    old_schema_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    async with old_schema_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE tickets ("
                "id INTEGER PRIMARY KEY, requester VARCHAR(128), subject VARCHAR(256), "
                "body TEXT, status VARCHAR(32), result_summary TEXT, "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO tickets (id, requester, subject, body, status, result_summary) "
                "VALUES (1, 'pre-existing@example.com', 'old ticket', 'body', 'COMPLETED', '')"
            )
        )
    await old_schema_engine.dispose()

    await db_session_module.init_db()  # must self-heal, not crash

    engine = db_session_module.get_engine()

    def _columns(sync_conn):
        return {c["name"] for c in sa_inspect(sync_conn).get_columns("tickets")}

    async with engine.connect() as conn:
        columns = await conn.run_sync(_columns)
    assert "submitted_by_client_id" in columns

    # The pre-existing row must survive the migration, with the new column
    # nullable/None rather than the whole row being lost or defaulted wrong.
    async with db_session_module.session_scope() as session:
        ticket = await session.get(Ticket, 1)
        assert ticket is not None
        assert ticket.requester == "pre-existing@example.com"
        assert ticket.submitted_by_client_id is None


async def test_init_db_is_idempotent_when_column_already_present(isolated_db):
    """Calling init_db() twice (e.g. across restarts, or this app's 2
    gunicorn workers both booting) must not error the second time now that
    the column already exists."""
    await db_session_module.init_db()
    await db_session_module.init_db()  # must not raise


async def test_new_ticket_records_which_client_submitted_it(isolated_db):
    from app.db.models import ApiClient, ApiClientRole

    await db_session_module.init_db()

    async with db_session_module.session_scope() as session:
        client = ApiClient(name="public-demo-guest", role=ApiClientRole.STANDARD, key="demo-key")
        session.add(client)
        await session.flush()
        client_id = client.id

        ticket = Ticket(
            requester="scoping-check@example.com",  # deliberately NOT equal to client.name
            subject="s", body="b", status=TicketStatus.PLANNING,
            submitted_by_client_id=client_id,
        )
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id

    async with db_session_module.session_scope() as session:
        reloaded = await session.get(Ticket, ticket_id)
        assert reloaded.submitted_by_client_id == client_id
        assert reloaded.requester == "scoping-check@example.com"
        # The whole point: ownership is NOT the same as the requester string.
        assert reloaded.requester != "public-demo-guest"
