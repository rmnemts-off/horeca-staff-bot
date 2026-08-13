"""Acceptance criterion 11.5: the delivery is empty.

"После `docker compose up` и миграций база пуста" — this test is the machine-checkable
half of it. A fresh database gets `alembic upgrade head` and nothing else; every table is
then discovered from `information_schema` (never a hard-coded list, so a table added later
cannot quietly escape the check) and must contain zero rows.

The static half — no INSERT may exist in a migration in the first place — lives in
`tests/test_no_seed_data.py` and runs without a database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from src.db.models import Base

pytestmark = pytest.mark.db

#: The only table allowed to hold a row: Alembic's own revision bookkeeping.
BOOKKEEPING_TABLES = frozenset({"alembic_version"})

LIST_TABLES = text(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
    "ORDER BY table_name"
)


async def test_migrations_create_every_model_table(
    empty_migrated_database: AsyncEngine,
) -> None:
    """Sanity check on the discovery itself: the migration really built the schema."""
    async with empty_migrated_database.connect() as connection:
        discovered = {row[0] for row in await connection.execute(LIST_TABLES)}

    missing = set(Base.metadata.tables) - discovered
    unexpected = discovered - set(Base.metadata.tables) - BOOKKEEPING_TABLES
    assert not missing, f"tables declared in the models but not created: {sorted(missing)}"
    assert not unexpected, f"tables created but not declared in the models: {sorted(unexpected)}"


async def test_delivery_ships_with_zero_rows(empty_migrated_database: AsyncEngine) -> None:
    """Criterion 11.5: count(*) = 0 in every table except alembic_version."""
    async with empty_migrated_database.connect() as connection:
        tables = [row[0] for row in await connection.execute(LIST_TABLES)]
        assert tables, "upgrade head created no tables at all"
        assert set(tables) >= BOOKKEEPING_TABLES

        populated: dict[str, int] = {}
        for table in tables:
            if table in BOOKKEEPING_TABLES:
                continue
            # Identifier comes from information_schema, not from user input.
            rows = await connection.execute(text(f'SELECT count(*) FROM "{table}"'))
            count = rows.scalar_one()
            if count:
                populated[table] = count

    assert not populated, (
        "the delivery must ship empty (TZ 1.4#6, criterion 11.5), "
        f"but these tables already contain rows: {populated}"
    )
