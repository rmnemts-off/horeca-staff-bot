"""Schema freeze: the table list and the conventions that the whole project relies on.

Needs no database — it only inspects `Base.metadata`.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, DateTime
from src.db.models import Base
from src.db.repositories import CHILD_PARENTS
from src.db.repositories import LIBRARY_TABLES as REPOSITORY_LIBRARY_TABLES

# TZ section 4 verbatim: 4.1 organisation and people, 4.2 schedule, 4.3 checklists,
# 4.4 nomenclature, 4.5 write-offs, 4.6 recipes and knowledge base, 4.7 service tables.
EXPECTED_TABLES = frozenset(
    {
        # 4.1
        "venues",
        "venue_settings",
        "users",
        "venue_members",
        "invite_codes",
        # 4.2
        "shifts",
        # 4.3
        "checklist_templates",
        "checklist_items",
        "checklist_runs",
        "checklist_run_items",
        # 4.4
        "product_categories",
        "products",
        "suppliers",
        "supplier_products",
        "orders",
        "order_items",
        # 4.5
        "writeoff_reasons",
        "writeoffs",
        # 4.6
        "recipes",
        "recipe_ingredients",
        "preps",
        "prep_ingredients",
        "knowledge_articles",
        # 4.7
        "notifications",
        "audit_log",
        "import_jobs",
    }
)

#: Tables that legitimately have no `venue_id` (TZ 3.3 says "almost every table"):
#: `venues` is the scope itself, `users` is global, the rest are child rows reachable
#: only through a venue-scoped parent (decision D9).
TABLES_WITHOUT_VENUE_ID = frozenset(
    {
        "venues",
        "users",
        "checklist_items",
        "checklist_run_items",
        "supplier_products",
        "order_items",
        "recipe_ingredients",
        "prep_ingredients",
    }
)

#: `venue_id` may be NULL only for the shared BarPoint library (TZ 3.3, decision D9).
#: `preps` keeps a nullable column per TZ 4.6, but is not readable through `library()`.
NULLABLE_VENUE_TABLES = frozenset({"recipes", "knowledge_articles", "preps"})


def test_metadata_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_every_table_is_venue_scoped_or_explicitly_listed() -> None:
    scoped = {name for name, table in Base.metadata.tables.items() if "venue_id" in table.c}
    assert set(Base.metadata.tables) - scoped == TABLES_WITHOUT_VENUE_ID


def test_venue_id_is_nullable_only_for_the_library() -> None:
    nullable = {
        name
        for name, table in Base.metadata.tables.items()
        if "venue_id" in table.c and table.c.venue_id.nullable
    }
    assert nullable == NULLABLE_VENUE_TABLES
    # The repository whitelist is narrower than the schema: preps stay venue scoped (D9).
    assert REPOSITORY_LIBRARY_TABLES < NULLABLE_VENUE_TABLES


def test_every_child_table_reaches_a_venue_through_its_parent() -> None:
    """Decision D9: a row without `venue_id` is addressed only through its parent.

    `CHILD_PARENTS` is the map the repository layer enforces at import time; here it is
    checked against the schema itself, so the two cannot drift. `venues` and `users` are
    the two genuinely global tables and are excluded by name.
    """
    children = TABLES_WITHOUT_VENUE_ID - {"venues", "users"}
    assert set(CHILD_PARENTS) == children, (
        "every table without venue_id needs a declared parent (decision D9); "
        f"unmapped: {sorted(children - set(CHILD_PARENTS))}, "
        f"stale: {sorted(set(CHILD_PARENTS) - children)}"
    )

    for child, (parent, foreign_key) in CHILD_PARENTS.items():
        child_table = Base.metadata.tables[child]
        parent_table = Base.metadata.tables[parent]
        assert "venue_id" in parent_table.c, f"{parent} carries no venue_id"
        assert foreign_key in child_table.c, f"{child} has no column {foreign_key}"
        targets = {reference.column for reference in child_table.c[foreign_key].foreign_keys}
        assert parent_table.c.id in targets, (
            f"{child}.{foreign_key} does not point at {parent}.id, so the join in "
            "ChildRepository would not scope anything"
        )


def test_primary_keys_are_bigint_identity() -> None:
    # Decision D14: BIGINT identity, never UUID — two ids must fit in 64-byte callback_data.
    for name, table in Base.metadata.tables.items():
        pk_columns = list(table.primary_key.columns)
        assert len(pk_columns) == 1, name
        column = pk_columns[0]
        assert isinstance(column.type, BigInteger), name
        if name == "venue_settings":
            # PK is the venue itself (TZ 4.1: venue_settings.venue_id PK/FK).
            assert column.name == "venue_id"
            continue
        assert column.name == "id", name
        assert column.identity is not None, name


def test_all_timestamps_are_timezone_aware() -> None:
    # TZ 3.4: everything is stored in UTC, so every timestamp column is timestamptz.
    for name, table in Base.metadata.tables.items():
        for column in table.c:
            if isinstance(column.type, DateTime):
                assert column.type.timezone is True, f"{name}.{column.name}"
