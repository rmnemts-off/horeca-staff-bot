"""Venues and their settings (TZ 4.1)."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Integer, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base, CreatedAtMixin, IdMixin

if TYPE_CHECKING:
    from src.db.models.user import VenueMember


class Venue(IdMixin, CreatedAtMixin, Base):
    """TZ 4.1: venues(id, name, city, timezone, is_active, created_at)."""

    __tablename__ = "venues"

    name: Mapped[str]
    city: Mapped[str]
    # IANA name, e.g. Europe/Moscow (TZ 3.4).
    timezone: Mapped[str]
    is_active: Mapped[bool] = mapped_column(server_default="true", nullable=False)

    settings: Mapped[VenueSettings | None] = relationship(
        back_populates="venue",
        cascade="all, delete-orphan",
        uselist=False,
    )
    members: Mapped[list[VenueMember]] = relationship(back_populates="venue")


class VenueSettings(Base):
    """TZ 4.1 plus the default shift window (TZ 4.2 refers to it, section 4 lacks it)."""

    __tablename__ = "venue_settings"

    venue_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("venues.id", ondelete="CASCADE"),
        primary_key=True,
    )

    opening_checklist_lead_minutes: Mapped[int] = mapped_column(
        Integer, server_default="10", nullable=False
    )
    closing_checklist_lead_minutes: Mapped[int] = mapped_column(
        Integer, server_default="30", nullable=False
    )
    shift_reminder_hours: Mapped[int] = mapped_column(Integer, server_default="12", nullable=False)
    checklist_overdue_minutes: Mapped[int] = mapped_column(
        Integer, server_default="60", nullable=False
    )
    escalate_to_manager: Mapped[bool] = mapped_column(server_default="true", nullable=False)
    # Working group of the venue (TZ 5.10); NULL until the manager runs /bind.
    group_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Venue-local time (D12), NULL while the venue does not remind about orders.
    order_reminder_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)

    # Default shift window, venue-local time. Required by TZ 4.2 ("if the schedule file has
    # no time column, take the venue default shift time") and by the create-venue wizard.
    default_shift_start: Mapped[dt.time] = mapped_column(Time, nullable=False)
    default_shift_end: Mapped[dt.time] = mapped_column(Time, nullable=False)

    venue: Mapped[Venue] = relationship(back_populates="settings")
