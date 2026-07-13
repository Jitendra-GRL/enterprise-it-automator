"""Alembic environment — async-engine aware, single-source-of-truth config.

How this coexists with app/db/session.py's init_db() (create_all +
_ensure_column): init_db() remains the zero-ceremony path for local dev,
tests, and the current Render deployment — it self-heals a fresh or
slightly-behind database at startup and predates this directory. Alembic is
the CONTROLLED path for environments where schema changes must be reviewed,
versioned, applied deliberately (and rolled back): run `alembic upgrade
head` as a release step and init_db()'s create_all/_ensure_column calls
become no-ops (everything already exists). The two agree by construction —
tests/test_migrations.py fails CI if the migration chain and the live
models ever describe different schemas.

The database URL comes from the app's own Settings (DATABASE_URL env/.env),
NOT from alembic.ini — alembic can never migrate a different database than
the app would run against. The async driver URL (sqlite+aiosqlite /
postgresql+asyncpg) is used as-is via an async engine, matching how the app
itself connects.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit the SQL that WOULD run, without a DB connection — supports the
    review-the-DDL-before-it-touches-prod workflow (`alembic upgrade head
    --sql`).
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_render_as_batch(),
    )

    with context.begin_transaction():
        context.run_migrations()


def _render_as_batch() -> bool:
    """SQLite can't ALTER TABLE most things in place; alembic's batch mode
    recreates the table instead. Enabled only on SQLite — Postgres gets
    plain ALTERs.
    """
    return _database_url().startswith("sqlite")


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=_render_as_batch(),
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": _database_url()},
        prefix="sqlalchemy.",
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
