"""Rebinding a card to another Telegram account, and the last-owner backstop.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-15

Two things, both of them answers to something that happened on the live stand.

**Rebinding.** A person who changes their Telegram account had no way back into their own
card: a new code created a second `users` row and a second membership, the roster showed
the same name twice, and the shifts, checklists and write-offs stayed on the first one. The
fix is a code with a `purpose` — it points at a card and moves `users.telegram_id`, so the
history never moves at all. The columns are here; the behaviour is in
`src/services/access.py`.

**The owner guard.** `MemberService` refuses to leave a venue without an active owner, and
this trigger is the second lock on that door. It is deliberately `DEFERRABLE INITIALLY
DEFERRED`: handing ownership over is «promote the new one, demote the old one» inside one
transaction, and an immediate trigger would kill it halfway through. The check for
`venues.is_active` keeps a cascade from deleting a venue from tripping over it.

Everything here is additive and the data is untouched: `purpose` arrives with a server
default, so every code issued before this migration keeps meaning exactly what it meant.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

#: The guard of TZ 2. `OLD` is the row that changed; the venue is read off it, because the
#: row itself may no longer be an owner by the time this runs.
OWNER_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION bp_venue_members_owner_guard() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM venues v WHERE v.id = OLD.venue_id AND v.is_active)
     AND NOT EXISTS (
       SELECT 1 FROM venue_members m
       WHERE m.venue_id = OLD.venue_id AND m.role = 'owner' AND m.is_active
     )
  THEN
    RAISE EXCEPTION 'venue % would be left without an active owner', OLD.venue_id
      USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

#: `DEFERRABLE INITIALLY DEFERRED` is the whole design of this line: handing ownership over
#: is «promote the new one, demote the old one» inside one transaction, and an immediate
#: trigger would kill it halfway through, when the venue momentarily has two owners on the
#: way to having one.
OWNER_GUARD_TRIGGER = (
    "CREATE CONSTRAINT TRIGGER trg_venue_members_owner_guard "
    "AFTER UPDATE OR DELETE ON venue_members "
    "DEFERRABLE INITIALLY DEFERRED "
    "FOR EACH ROW EXECUTE FUNCTION bp_venue_members_owner_guard()"
)


def upgrade() -> None:
    purpose = postgresql.ENUM("join", "rebind", name="bp_invite_purpose", create_type=False)
    purpose.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "invite_codes",
        sa.Column("purpose", purpose, nullable=False, server_default="join"),
    )
    op.add_column(
        "invite_codes",
        sa.Column("issued_to_member_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_invite_codes_issued_to_member_id"),
        "invite_codes",
        "venue_members",
        ["issued_to_member_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "rebind_target",
        "invite_codes",
        "(purpose = 'rebind') = (issued_to_member_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "used_pair",
        "invite_codes",
        "(used_at IS NULL) = (used_by IS NULL)",
    )
    op.create_index(
        "uq_invite_codes_open_rebind",
        "invite_codes",
        ["issued_to_member_id"],
        unique=True,
        postgresql_where=sa.text("purpose = 'rebind' AND used_at IS NULL AND revoked_at IS NULL"),
    )
    op.create_index(
        "ix_invite_codes_venue_pending",
        "invite_codes",
        ["venue_id"],
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )
    op.create_index(
        "ix_venue_members_active_owner",
        "venue_members",
        ["venue_id"],
        postgresql_where=sa.text("role = 'owner' AND is_active"),
    )

    op.execute(OWNER_GUARD_FUNCTION)
    op.execute(OWNER_GUARD_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_venue_members_owner_guard ON venue_members")
    op.execute("DROP FUNCTION IF EXISTS bp_venue_members_owner_guard()")

    op.drop_index("ix_venue_members_active_owner", table_name="venue_members")
    op.drop_index("ix_invite_codes_venue_pending", table_name="invite_codes")
    op.drop_index("uq_invite_codes_open_rebind", table_name="invite_codes")
    # `op.f()` marks the name as final. Without it the naming convention of
    # `src/db/models/base.py` prefixes it a second time and the drop looks for
    # `ck_invite_codes_ck_invite_codes_used_pair`, which does not exist — the whole
    # downgrade then rolls back and the migration cannot be undone at all.
    op.drop_constraint(op.f("ck_invite_codes_used_pair"), "invite_codes", type_="check")
    op.drop_constraint(op.f("ck_invite_codes_rebind_target"), "invite_codes", type_="check")
    op.drop_constraint(
        op.f("fk_invite_codes_issued_to_member_id"), "invite_codes", type_="foreignkey"
    )
    op.drop_column("invite_codes", "issued_to_member_id")
    op.drop_column("invite_codes", "purpose")

    postgresql.ENUM(name="bp_invite_purpose").drop(op.get_bind(), checkfirst=True)
