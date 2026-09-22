"""Alembic's environment: how migrations reach the database.

Alembic's own async template, with two changes. The URL comes from
DATABASE_URL, the variable the app reads. And migrations run under a
PostgreSQL advisory lock, so two replicas that migrate as they start do not
race: the second waits, then finds the schema already at head and does
nothing.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# Migrations here are written by hand with `op`, so there is no model metadata
# to compare against. Set this to your models' MetaData for autogenerate.
target_metadata = None

# Any constant works, as long as nothing else in the database uses it.
LOCK = 0x0B_B0_0C


def database_url() -> str:
    url = os.environ.get(
        "DATABASE_URL", "postgresql://oxbrook:oxbrook@127.0.0.1:5499/oxbrook"
    )
    # The app speaks to asyncpg directly and SQLAlchemy needs to be told which
    # driver to use for the same database.
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        # Held until this transaction ends. PostgreSQL runs DDL inside a
        # transaction, so that is after the last migration and the version
        # row are committed, and a runner that waited reads both.
        connection.execute(text("select pg_advisory_xact_lock(:key)"), {"key": LOCK})
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    # asyncio.run needs a thread with no running loop. The CLI is one; code
    # that migrates from inside a server's lifespan calls command.upgrade
    # through asyncio.to_thread for this reason.
    asyncio.run(run_async_migrations())
