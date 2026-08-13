"""Alembic environment.

Three ways to point Alembic at a database, in this order of priority:

1. ``config.attributes["database_url"]`` — programmatic, used by the test harness
   (``tests/conftest.py``) so that a throwaway database can be migrated without
   touching the process environment. Passed through ``Config.attributes``, not
   ``sqlalchemy.url``, because the latter goes through ConfigParser interpolation
   and would choke on a ``%`` inside a password.
2. ``DATABASE_URL`` in the environment — how the Docker entrypoint runs.
3. ``settings.database_url`` — the ``.env`` file, for a local `alembic` invocation.

The engine is async (TZ 3.1: SQLAlchemy 2.0 asyncio). Offline mode (``--sql``) needs no
DBAPI at all and is what the migration is verified with while no PostgreSQL is available.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from src.config import MissingSettingError, settings
from src.db.models import Base

config = context.config

# The test harness calls Alembic in-process and sets `configure_logging` to False, so that
# reading alembic.ini does not tear down the loggers pytest has already installed.
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Autogenerate compares against the frozen model layer (plan, block A, tasks 2-3).
target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the connection URL; never read it from `sqlalchemy.url`."""
    from_attributes = config.attributes.get("database_url")
    if from_attributes:
        return str(from_attributes)
    from_environment = os.environ.get("DATABASE_URL", "").strip()
    if from_environment:
        return from_environment
    if not settings.database_url:
        raise MissingSettingError("DATABASE_URL")
    return settings.database_url


def _context_options() -> dict[str, object]:
    """Options shared by offline and online runs."""
    return {
        "target_metadata": target_metadata,
        # Column type changes must show up in autogenerate diffs, not be silently ignored.
        "compare_type": True,
        "compare_server_default": True,
        # Named constraints (see NAMING_CONVENTION in src/db/models/base.py) let Alembic
        # render DROP CONSTRAINT for objects it did not create itself.
        "include_schemas": False,
        "render_as_batch": False,
    }


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (`alembic upgrade head --sql`)."""
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_context_options(),  # type: ignore[arg-type]
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_context_options())  # type: ignore[arg-type]

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
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
