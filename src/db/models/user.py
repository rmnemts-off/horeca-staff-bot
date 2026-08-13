"""People: users, venue membership, invite codes (TZ 4.1, 5.1)."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base, CreatedAtMixin, IdMixin, VenueScopedMixin, fk
from src.db.models.enums import MemberRole, member_role_enum

if TYPE_CHECKING:
    from src.db.models.venue import Venue


class User(IdMixin, CreatedAtMixin, Base):
    """TZ 4.1: users(id, telegram_id, full_name, phone, username, is_active, last_seen_at).

    Global entity: one person may work in several venues (TZ 2), so `users` has no venue_id.
    """

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    full_name: Mapped[str]
    phone: Mapped[str | None] = mapped_column(nullable=True)
    username: Mapped[str | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(server_default="true", nullable=False)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    # TZ 5.1: "if the employee belongs to several venues, the choice is remembered".
    # Kept in the database, not in the Redis FSM store, because the FSM store has a TTL.
    active_venue_id: Mapped[int | None] = fk("venues.id", ondelete="SET NULL", nullable=True)
    # TZ 6: a failed delivery marks the person as "bot blocked" in the employees list (TZ 5.8).
    is_bot_blocked: Mapped[bool] = mapped_column(server_default="false", nullable=False)

    memberships: Mapped[list[VenueMember]] = relationship(back_populates="user")


class VenueMember(IdMixin, VenueScopedMixin, Base):
    """TZ 4.1: venue_members(id, user_id, venue_id, role, position, is_active, joined_at)."""

    __tablename__ = "venue_members"
    __table_args__ = (UniqueConstraint("user_id", "venue_id", name="uq_venue_members_user_venue"),)

    user_id: Mapped[int] = fk("users.id", ondelete="CASCADE", index=True)
    role: Mapped[MemberRole] = mapped_column(member_role_enum, nullable=False)
    # Bartender / waiter / cook — free text, the venue names positions itself.
    position: Mapped[str | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(server_default="true", nullable=False)
    joined_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    user: Mapped[User] = relationship(back_populates="memberships")
    venue: Mapped[Venue] = relationship(back_populates="members")


class InviteCode(IdMixin, VenueScopedMixin, CreatedAtMixin, Base):
    """TZ 4.1 and 5.1: single-use code, lives 7 days, bound to venue, role and position."""

    __tablename__ = "invite_codes"

    code: Mapped[str] = mapped_column(unique=True, nullable=False)
    role: Mapped[MemberRole] = mapped_column(member_role_enum, nullable=False)
    position: Mapped[str | None] = mapped_column(nullable=True)
    # Decision B8: the manager types the full name when issuing the code; the employee
    # confirms or corrects it on activation. Nullable, so the manager may leave it blank.
    full_name: Mapped[str | None] = mapped_column(nullable=True)

    created_by: Mapped[int | None] = fk("users.id", ondelete="SET NULL", nullable=True)
    expires_at: Mapped[dt.datetime]
    used_by: Mapped[int | None] = fk("users.id", ondelete="SET NULL", nullable=True)
    used_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    # Plan, task 15: a code can be revoked before it is used.
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
