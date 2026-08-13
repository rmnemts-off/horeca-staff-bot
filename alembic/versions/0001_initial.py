"""initial schema

Revision ID: 0001
Revises:
Create Date: frozen with the model layer (plan, block A, tasks 2-3)

Creates the whole schema of TZ section 4 and NOTHING ELSE — structure only, not one row
of content. The system ships empty (TZ 1.4#6, acceptance criterion 11.5), so a migration
may never write data; `tests/test_no_seed_data.py` scans this directory as plain text and
fails the build if any row-writing statement ever appears here.

Order of operations matters:

1. `pg_trgm` — the GIN indexes on `recipes.name` and `knowledge_articles.title` need the
   `gin_trgm_ops` operator class (TZ 4.6: search survives typos).
2. Enum types — created explicitly, once each, because several tables share a type
   (`checklist_type` is used by templates and runs, `unit` by five tables). Letting
   SQLAlchemy create them as a side effect of `create_table` would emit `CREATE TYPE`
   twice and fail.
3. Tables and indexes, in dependency order.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Named PostgreSQL enum types, mirroring src/db/models/enums.py. Values are part of the
#: schema, not of the data: they are fixed by the code, never entered by a venue.
#: The `bp_` prefix keeps generic names (`unit`, `order_status`, `member_role`) out of the
#: shared `public` namespace of a database this schema may not own; the product is meant to
#: be replicated across installations (TZ 1.4#4).
ENUM_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bp_checklist_type", ("opening", "closing", "stock")),
    ("bp_member_role", ("staff", "manager", "owner")),
    ("bp_notification_status", ("scheduled", "sending", "sent", "failed", "cancelled")),
    ("bp_order_status", ("draft", "submitted", "sent", "confirmed", "cancelled")),
    ("bp_product_kind", ("alcohol", "non_alcohol", "ingredient", "other")),
    ("bp_run_status", ("sent", "in_progress", "completed", "overdue", "skipped")),
    ("bp_shift_source", ("manual", "import")),
    ("bp_shift_status", ("planned", "confirmed", "worked", "missed", "cancelled")),
    ("bp_supplier_channel", ("supplier", "marketplace", "market", "household")),
    ("bp_unit", ("ml", "l", "g", "kg", "pcs", "bottle")),
)


def upgrade() -> None:
    # The schema uses two PostgreSQL 15 features: NULLS NOT DISTINCT on the recipe
    # uniqueness index, and the column list form of ON DELETE SET NULL in the venue
    # composite keys. Fail with a readable message instead of a syntax error halfway in.
    op.execute(
        "DO $$ BEGIN IF current_setting('server_version_num')::int < 150000 THEN "
        "RAISE EXCEPTION 'PostgreSQL 15 or newer is required by this schema'; "
        "END IF; END $$"
    )

    # TZ 4.6: a misspelled query must still find the recipe, which needs similarity().
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # `CREATE TYPE` has no IF NOT EXISTS. Without the guard an upgrade that failed halfway
    # cannot be retried: the types created before the failure already exist, and there is
    # no revision to downgrade from. The guard is symmetric with `DROP TYPE IF EXISTS` in
    # `downgrade()`.
    for type_name, values in ENUM_TYPES:
        rendered_values = ", ".join(f"'{value}'" for value in values)
        op.execute(
            f"DO $$ BEGIN CREATE TYPE {type_name} AS ENUM ({rendered_values}); "
            f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )

    op.create_table(
        "venues",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_venues")),
    )
    op.create_table(
        "product_categories",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "alcohol",
                "non_alcohol",
                "ingredient",
                "other",
                name="bp_product_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["venue_id", "parent_id"],
            ["product_categories.venue_id", "product_categories.id"],
            name="fk_product_categories_venue_parent",
            ondelete="SET NULL (parent_id)",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"],
            ["venues.id"],
            name=op.f("fk_product_categories_venue_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_categories")),
        sa.UniqueConstraint("venue_id", "id", name="uq_product_categories_venue_id_id"),
    )
    op.create_index(
        op.f("ix_product_categories_venue_id"), "product_categories", ["venue_id"], unique=False
    )
    op.create_index(
        "ix_product_categories_venue_order",
        "product_categories",
        ["venue_id", "order_index"],
        unique=False,
    )
    op.create_table(
        "suppliers",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("contact_person", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("telegram_username", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column(
            "channel",
            postgresql.ENUM(
                "supplier",
                "marketplace",
                "market",
                "household",
                name="bp_supplier_channel",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("order_url", sa.Text(), nullable=True),
        sa.Column("external_app", sa.Text(), nullable=True),
        sa.Column("order_deadline_time", sa.Time(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_suppliers_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suppliers")),
        sa.UniqueConstraint("venue_id", "id", name="uq_suppliers_venue_id_id"),
    )
    op.create_index(op.f("ix_suppliers_venue_id"), "suppliers", ["venue_id"], unique=False)
    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_venue_id", sa.BigInteger(), nullable=True),
        sa.Column("is_bot_blocked", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["active_venue_id"],
            ["venues.id"],
            name=op.f("fk_users_active_venue_id"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_users_telegram_id")),
    )
    op.create_table(
        "venue_settings",
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "opening_checklist_lead_minutes", sa.Integer(), server_default="10", nullable=False
        ),
        sa.Column(
            "closing_checklist_lead_minutes", sa.Integer(), server_default="30", nullable=False
        ),
        sa.Column("shift_reminder_hours", sa.Integer(), server_default="12", nullable=False),
        sa.Column("checklist_overdue_minutes", sa.Integer(), server_default="60", nullable=False),
        sa.Column("escalate_to_manager", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("group_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("order_reminder_time", sa.Time(), nullable=True),
        sa.Column("default_shift_start", sa.Time(), nullable=False),
        sa.Column("default_shift_end", sa.Time(), nullable=False),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_venue_settings_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("venue_id", name=op.f("pk_venue_settings")),
    )
    op.create_table(
        "writeoff_reasons",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("requires_comment", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("requires_photo", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["venue_id"],
            ["venues.id"],
            name=op.f("fk_writeoff_reasons_venue_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_writeoff_reasons")),
        sa.UniqueConstraint("venue_id", "id", name="uq_writeoff_reasons_venue_id_id"),
    )
    op.create_index(
        op.f("ix_writeoff_reasons_venue_id"), "writeoff_reasons", ["venue_id"], unique=False
    )
    op.create_index(
        "ix_writeoff_reasons_venue_order",
        "writeoff_reasons",
        ["venue_id", "order_index"],
        unique=False,
    )
    op.create_table(
        "audit_log",
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_audit_log_user_id"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_audit_log_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity", "entity_id"], unique=False)
    op.create_index(
        "ix_audit_log_venue_created", "audit_log", ["venue_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_audit_log_venue_id"), "audit_log", ["venue_id"], unique=False)
    op.create_table(
        "checklist_templates",
        sa.Column(
            "type",
            postgresql.ENUM(
                "opening", "closing", "stock", name="bp_checklist_type", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_checklist_templates_updated_by"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"],
            ["venues.id"],
            name=op.f("fk_checklist_templates_venue_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checklist_templates")),
        sa.UniqueConstraint(
            "venue_id", "type", "version", name="uq_checklist_templates_venue_type_version"
        ),
        sa.UniqueConstraint("venue_id", "id", name="uq_checklist_templates_venue_id_id"),
    )
    op.create_index(
        op.f("ix_checklist_templates_venue_id"), "checklist_templates", ["venue_id"], unique=False
    )
    op.create_index(
        "uq_checklist_templates_active",
        "checklist_templates",
        ["venue_id", "type"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "import_jobs",
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("file_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rows_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_ok", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("mapping", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_import_jobs_user_id"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_import_jobs_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_jobs")),
    )
    op.create_index(
        "ix_import_jobs_venue_created", "import_jobs", ["venue_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_import_jobs_venue_id"), "import_jobs", ["venue_id"], unique=False)
    op.create_table(
        "invite_codes",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("staff", "manager", "owner", name="bp_member_role", create_type=False),
            nullable=False,
        ),
        sa.Column("position", sa.Text(), nullable=True),
        sa.Column("full_name", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_by", sa.BigInteger(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_invite_codes_created_by"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["used_by"], ["users.id"], name=op.f("fk_invite_codes_used_by"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_invite_codes_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invite_codes")),
        sa.UniqueConstraint("code", name=op.f("uq_invite_codes_code")),
    )
    op.create_index(op.f("ix_invite_codes_venue_id"), "invite_codes", ["venue_id"], unique=False)
    op.create_table(
        "knowledge_articles",
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_knowledge_articles_updated_by"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"],
            ["venues.id"],
            name=op.f("fk_knowledge_articles_venue_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_articles")),
    )
    op.create_index(
        "ix_knowledge_articles_aliases_gin",
        "knowledge_articles",
        ["aliases"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_articles_title_trgm",
        "knowledge_articles",
        ["title"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_index(
        op.f("ix_knowledge_articles_venue_id"), "knowledge_articles", ["venue_id"], unique=False
    )
    op.create_table(
        "notifications",
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "scheduled",
                "sending",
                "sent",
                "failed",
                "cancelled",
                name="bp_notification_status",
                create_type=False,
            ),
            server_default="scheduled",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("entity", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.BigInteger(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_notifications_user_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_notifications_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    # Uniqueness of `dedup_key` covers the live part of the queue only: a `cancelled`,
    # `failed` or `sent` row is history and must not block the same key from being
    # scheduled again (plan, tasks 14, 19 and 33). This partial index is the ON CONFLICT
    # target of the upsert.
    op.create_index(
        "uq_notifications_dedup_key",
        "notifications",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('scheduled', 'sending')"),
    )
    op.create_index(
        "ix_notifications_entity", "notifications", ["entity", "entity_id"], unique=False
    )
    op.create_index(
        "ix_notifications_status_scheduled",
        "notifications",
        ["status", "scheduled_at"],
        unique=False,
    )
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"], unique=False)
    op.create_index(op.f("ix_notifications_venue_id"), "notifications", ["venue_id"], unique=False)
    op.create_table(
        "orders",
        sa.Column("supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "draft",
                "submitted",
                "sent",
                "confirmed",
                "cancelled",
                name="bp_order_status",
                create_type=False,
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("delivery_date", sa.Date(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "supplier_id"],
            ["suppliers.venue_id", "suppliers.id"],
            name="fk_orders_venue_supplier",
            ondelete="SET NULL (supplier_id)",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_orders_user_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_orders_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
    )
    op.create_index(op.f("ix_orders_venue_id"), "orders", ["venue_id"], unique=False)
    op.create_index("ix_orders_venue_status", "orders", ["venue_id", "status"], unique=False)
    op.create_table(
        "preps",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("yield_amount", sa.Text(), nullable=True),
        sa.Column("shelf_life", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_preps_updated_by"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_preps_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preps")),
    )
    op.create_index(op.f("ix_preps_venue_id"), "preps", ["venue_id"], unique=False)
    op.create_table(
        "products",
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column(
            "unit",
            postgresql.ENUM(
                "ml", "l", "g", "kg", "pcs", "bottle", name="bp_unit", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("bottle_volume", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("portion_volume", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("portions_per_bottle", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("menu_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("purchase_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("default_supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("storage_zone", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "category_id"],
            ["product_categories.venue_id", "product_categories.id"],
            name="fk_products_venue_category",
            ondelete="SET NULL (category_id)",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "default_supplier_id"],
            ["suppliers.venue_id", "suppliers.id"],
            name="fk_products_venue_default_supplier",
            ondelete="SET NULL (default_supplier_id)",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_products_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("venue_id", "id", name="uq_products_venue_id_id"),
    )
    op.create_index(
        "ix_products_venue_category", "products", ["venue_id", "category_id"], unique=False
    )
    op.create_index(op.f("ix_products_venue_id"), "products", ["venue_id"], unique=False)
    op.create_table(
        "recipes",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("glassware", sa.Text(), nullable=True),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("ice", sa.Text(), nullable=True),
        sa.Column("garnish", sa.Text(), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("season_date", sa.Date(), nullable=True),
        sa.Column("yield_variants", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("photo_file_id", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_recipes_updated_by"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_recipes_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipes")),
    )
    op.create_index(
        "ix_recipes_aliases_gin",
        "recipes",
        ["aliases"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_recipes_name_trgm",
        "recipes",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(op.f("ix_recipes_venue_id"), "recipes", ["venue_id"], unique=False)
    op.create_index(
        "uq_recipes_venue_category_name",
        "recipes",
        ["venue_id", "category", sa.literal_column("lower(btrim(name))")],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_table(
        "shifts",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("shift_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("hours", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("is_opener", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_closer", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "planned",
                "confirmed",
                "worked",
                "missed",
                "cancelled",
                name="bp_shift_status",
                create_type=False,
            ),
            server_default="planned",
            nullable=False,
        ),
        sa.Column(
            "source",
            postgresql.ENUM("manual", "import", name="bp_shift_source", create_type=False),
            server_default="manual",
            nullable=False,
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_shifts_created_by"), ondelete="SET NULL"
        ),
        # RESTRICT: TZ 5.1 keeps the history of a deactivated employee.
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_shifts_user_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_shifts_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shifts")),
        sa.UniqueConstraint("venue_id", "id", name="uq_shifts_venue_id_id"),
    )
    op.create_index("ix_shifts_user_date", "shifts", ["user_id", "shift_date"], unique=False)
    op.create_index("ix_shifts_venue_date", "shifts", ["venue_id", "shift_date"], unique=False)
    op.create_index(op.f("ix_shifts_venue_id"), "shifts", ["venue_id"], unique=False)
    op.create_table(
        "venue_members",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("staff", "manager", "owner", name="bp_member_role", create_type=False),
            nullable=False,
        ),
        sa.Column("position", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_venue_members_user_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_venue_members_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_venue_members")),
        sa.UniqueConstraint("user_id", "venue_id", name="uq_venue_members_user_venue"),
    )
    op.create_index(op.f("ix_venue_members_user_id"), "venue_members", ["user_id"], unique=False)
    op.create_index(op.f("ix_venue_members_venue_id"), "venue_members", ["venue_id"], unique=False)
    op.create_table(
        "checklist_items",
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        sa.Column("group_name", sa.Text(), nullable=True),
        sa.Column("group_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("requires_photo", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("requires_comment", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_critical", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["checklist_templates.id"],
            name=op.f("fk_checklist_items_template_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checklist_items")),
    )
    op.create_index(
        op.f("ix_checklist_items_template_id"), "checklist_items", ["template_id"], unique=False
    )
    op.create_index(
        "ix_checklist_items_template_order",
        "checklist_items",
        ["template_id", "group_index", "order_index"],
        unique=False,
    )
    op.create_table(
        "checklist_runs",
        sa.Column("shift_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM(
                "opening", "closing", "stock", name="bp_checklist_type", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "sent",
                "in_progress",
                "completed",
                "overdue",
                "skipped",
                name="bp_run_status",
                create_type=False,
            ),
            server_default="sent",
            nullable=False,
        ),
        sa.Column("done_items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skip_comment", sa.Text(), nullable=True),
        sa.Column("completed_by", sa.BigInteger(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["completed_by"],
            ["users.id"],
            name=op.f("fk_checklist_runs_completed_by"),
            ondelete="SET NULL",
        ),
        # Venue-composite keys: a run cannot point at the shift or the template of another
        # venue. `SET NULL (shift_id)` is the PostgreSQL 15+ column list — deleting a shift
        # is a normal operation (plan, tasks 29 and 33) and must leave the completed
        # checklist in place (TZ 4.3, 5.9); nulling the whole key would null `venue_id`.
        sa.ForeignKeyConstraint(
            ["venue_id", "shift_id"],
            ["shifts.venue_id", "shifts.id"],
            name="fk_checklist_runs_venue_shift",
            ondelete="SET NULL (shift_id)",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "template_id"],
            ["checklist_templates.venue_id", "checklist_templates.id"],
            name="fk_checklist_runs_venue_template",
            ondelete="RESTRICT",
        ),
        # RESTRICT: TZ 5.1 keeps the history of a deactivated employee.
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_checklist_runs_user_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_checklist_runs_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checklist_runs")),
    )
    op.create_index(op.f("ix_checklist_runs_user_id"), "checklist_runs", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_checklist_runs_venue_id"), "checklist_runs", ["venue_id"], unique=False
    )
    op.create_index(
        "ix_checklist_runs_venue_status", "checklist_runs", ["venue_id", "status"], unique=False
    )
    op.create_index(
        "uq_checklist_runs_shift_type",
        "checklist_runs",
        ["shift_id", "type"],
        unique=True,
        postgresql_where=sa.text("shift_id IS NOT NULL"),
    )
    op.create_table(
        "order_items",
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column(
            "unit",
            postgresql.ENUM(
                "ml", "l", "g", "kg", "pcs", "bottle", name="bp_unit", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_order_items_order_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_order_items_product_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
    )
    op.create_index(op.f("ix_order_items_order_id"), "order_items", ["order_id"], unique=False)
    op.create_table(
        "prep_ingredients",
        sa.Column("prep_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column(
            "unit",
            postgresql.ENUM(
                "ml", "l", "g", "kg", "pcs", "bottle", name="bp_unit", create_type=False
            ),
            nullable=True,
        ),
        sa.Column("qty_text", sa.Text(), nullable=True),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["prep_id"], ["preps.id"], name=op.f("fk_prep_ingredients_prep_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_prep_ingredients_product_id"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prep_ingredients")),
    )
    op.create_index(
        op.f("ix_prep_ingredients_prep_id"), "prep_ingredients", ["prep_id"], unique=False
    )
    op.create_table(
        "recipe_ingredients",
        sa.Column("recipe_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=True),
        sa.Column("prep_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column(
            "unit",
            postgresql.ENUM(
                "ml", "l", "g", "kg", "pcs", "bottle", name="bp_unit", create_type=False
            ),
            nullable=True,
        ),
        sa.Column("qty_text", sa.Text(), nullable=True),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["prep_id"],
            ["preps.id"],
            name=op.f("fk_recipe_ingredients_prep_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_recipe_ingredients_product_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipes.id"],
            name=op.f("fk_recipe_ingredients_recipe_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipe_ingredients")),
    )
    op.create_index(
        op.f("ix_recipe_ingredients_recipe_id"), "recipe_ingredients", ["recipe_id"], unique=False
    )
    op.create_table(
        "supplier_products",
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("sku", sa.Text(), nullable=True),
        sa.Column("package_size", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_supplier_products_product_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.id"],
            name=op.f("fk_supplier_products_supplier_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_products")),
    )
    op.create_index(
        op.f("ix_supplier_products_product_id"), "supplier_products", ["product_id"], unique=False
    )
    op.create_index(
        op.f("ix_supplier_products_supplier_id"), "supplier_products", ["supplier_id"], unique=False
    )
    op.create_table(
        "writeoffs",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("shift_id", sa.BigInteger(), nullable=True),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column(
            "unit",
            postgresql.ENUM(
                "ml", "l", "g", "kg", "pcs", "bottle", name="bp_unit", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("reason_id", sa.BigInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("photo_file_id", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("venue_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by"],
            ["users.id"],
            name=op.f("fk_writeoffs_cancelled_by"),
            ondelete="SET NULL",
        ),
        # Venue-composite keys: product, reason and shift all belong to the same venue.
        sa.ForeignKeyConstraint(
            ["venue_id", "product_id"],
            ["products.venue_id", "products.id"],
            name="fk_writeoffs_venue_product",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "reason_id"],
            ["writeoff_reasons.venue_id", "writeoff_reasons.id"],
            name="fk_writeoffs_venue_reason",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id", "shift_id"],
            ["shifts.venue_id", "shifts.id"],
            name="fk_writeoffs_venue_shift",
            ondelete="SET NULL (shift_id)",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_writeoffs_user_id"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"], ["venues.id"], name=op.f("fk_writeoffs_venue_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_writeoffs")),
    )
    op.create_index(op.f("ix_writeoffs_user_id"), "writeoffs", ["user_id"], unique=False)
    op.create_index(
        "ix_writeoffs_venue_created", "writeoffs", ["venue_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_writeoffs_venue_id"), "writeoffs", ["venue_id"], unique=False)
    op.create_table(
        "checklist_run_items",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("is_done", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("photo_file_id", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["checklist_items.id"],
            name=op.f("fk_checklist_run_items_item_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["checklist_runs.id"],
            name=op.f("fk_checklist_run_items_run_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checklist_run_items")),
        sa.UniqueConstraint("run_id", "item_id", name="uq_checklist_run_items_run_item"),
    )
    op.create_index(
        op.f("ix_checklist_run_items_run_id"), "checklist_run_items", ["run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_checklist_run_items_run_id"), table_name="checklist_run_items")
    op.drop_table("checklist_run_items")
    op.drop_index(op.f("ix_writeoffs_venue_id"), table_name="writeoffs")
    op.drop_index("ix_writeoffs_venue_created", table_name="writeoffs")
    op.drop_index(op.f("ix_writeoffs_user_id"), table_name="writeoffs")
    op.drop_table("writeoffs")
    op.drop_index(op.f("ix_supplier_products_supplier_id"), table_name="supplier_products")
    op.drop_index(op.f("ix_supplier_products_product_id"), table_name="supplier_products")
    op.drop_table("supplier_products")
    op.drop_index(op.f("ix_recipe_ingredients_recipe_id"), table_name="recipe_ingredients")
    op.drop_table("recipe_ingredients")
    op.drop_index(op.f("ix_prep_ingredients_prep_id"), table_name="prep_ingredients")
    op.drop_table("prep_ingredients")
    op.drop_index(op.f("ix_order_items_order_id"), table_name="order_items")
    op.drop_table("order_items")
    op.drop_index(
        "uq_checklist_runs_shift_type",
        table_name="checklist_runs",
        postgresql_where=sa.text("shift_id IS NOT NULL"),
    )
    op.drop_index("ix_checklist_runs_venue_status", table_name="checklist_runs")
    op.drop_index(op.f("ix_checklist_runs_venue_id"), table_name="checklist_runs")
    op.drop_index(op.f("ix_checklist_runs_user_id"), table_name="checklist_runs")
    op.drop_table("checklist_runs")
    op.drop_index("ix_checklist_items_template_order", table_name="checklist_items")
    op.drop_index(op.f("ix_checklist_items_template_id"), table_name="checklist_items")
    op.drop_table("checklist_items")
    op.drop_index(op.f("ix_venue_members_venue_id"), table_name="venue_members")
    op.drop_index(op.f("ix_venue_members_user_id"), table_name="venue_members")
    op.drop_table("venue_members")
    op.drop_index(op.f("ix_shifts_venue_id"), table_name="shifts")
    op.drop_index("ix_shifts_venue_date", table_name="shifts")
    op.drop_index("ix_shifts_user_date", table_name="shifts")
    op.drop_table("shifts")
    op.drop_index(
        "uq_recipes_venue_category_name", table_name="recipes", postgresql_nulls_not_distinct=True
    )
    op.drop_index(op.f("ix_recipes_venue_id"), table_name="recipes")
    op.drop_index(
        "ix_recipes_name_trgm",
        table_name="recipes",
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.drop_index("ix_recipes_aliases_gin", table_name="recipes", postgresql_using="gin")
    op.drop_table("recipes")
    op.drop_index(op.f("ix_products_venue_id"), table_name="products")
    op.drop_index("ix_products_venue_category", table_name="products")
    op.drop_table("products")
    op.drop_index(op.f("ix_preps_venue_id"), table_name="preps")
    op.drop_table("preps")
    op.drop_index("ix_orders_venue_status", table_name="orders")
    op.drop_index(op.f("ix_orders_venue_id"), table_name="orders")
    op.drop_table("orders")
    op.drop_index(op.f("ix_notifications_venue_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_index("ix_notifications_status_scheduled", table_name="notifications")
    op.drop_index("ix_notifications_entity", table_name="notifications")
    op.drop_index(
        "uq_notifications_dedup_key",
        table_name="notifications",
        postgresql_where=sa.text("status IN ('scheduled', 'sending')"),
    )
    op.drop_table("notifications")
    op.drop_index(op.f("ix_knowledge_articles_venue_id"), table_name="knowledge_articles")
    op.drop_index(
        "ix_knowledge_articles_title_trgm",
        table_name="knowledge_articles",
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.drop_index(
        "ix_knowledge_articles_aliases_gin",
        table_name="knowledge_articles",
        postgresql_using="gin",
    )
    op.drop_table("knowledge_articles")
    op.drop_index(op.f("ix_invite_codes_venue_id"), table_name="invite_codes")
    op.drop_table("invite_codes")
    op.drop_index(op.f("ix_import_jobs_venue_id"), table_name="import_jobs")
    op.drop_index("ix_import_jobs_venue_created", table_name="import_jobs")
    op.drop_table("import_jobs")
    op.drop_index(
        "uq_checklist_templates_active",
        table_name="checklist_templates",
        postgresql_where=sa.text("is_active"),
    )
    op.drop_index(op.f("ix_checklist_templates_venue_id"), table_name="checklist_templates")
    op.drop_table("checklist_templates")
    op.drop_index(op.f("ix_audit_log_venue_id"), table_name="audit_log")
    op.drop_index("ix_audit_log_venue_created", table_name="audit_log")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_writeoff_reasons_venue_order", table_name="writeoff_reasons")
    op.drop_index(op.f("ix_writeoff_reasons_venue_id"), table_name="writeoff_reasons")
    op.drop_table("writeoff_reasons")
    op.drop_table("venue_settings")
    op.drop_table("users")
    op.drop_index(op.f("ix_suppliers_venue_id"), table_name="suppliers")
    op.drop_table("suppliers")
    op.drop_index("ix_product_categories_venue_order", table_name="product_categories")
    op.drop_index(op.f("ix_product_categories_venue_id"), table_name="product_categories")
    op.drop_table("product_categories")
    op.drop_table("venues")

    for type_name, _values in reversed(ENUM_TYPES):
        op.execute(f"DROP TYPE IF EXISTS {type_name}")

    # `pg_trgm` is deliberately NOT dropped: `CREATE EXTENSION IF NOT EXISTS` above may
    # have found it already installed by the DBA, and an extension is a database-level
    # facility rather than something this schema owns.
