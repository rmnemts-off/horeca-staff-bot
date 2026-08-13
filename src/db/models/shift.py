"""Shift schedule (TZ 4.2, 5.3)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Date, Index, Numeric, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base, IdMixin, TimestampMixin, VenueScopedMixin, fk
from src.db.models.enums import (
    ShiftSource,
    ShiftStatus,
    shift_source_enum,
    shift_status_enum,
)

if TYPE_CHECKING:
    from src.db.models.user import User


class Shift(IdMixin, VenueScopedMixin, TimestampMixin, Base):
    """TZ 4.2: shifts(...). A night shift belongs to the date it starts on (TZ 5.3)."""

    __tablename__ = "shifts"
    __table_args__ = (
        # Target of the composite foreign keys from `checklist_runs` and `writeoffs`.
        UniqueConstraint("venue_id", "id", name="uq_shifts_venue_id_id"),
        Index("ix_shifts_venue_date", "venue_id", "shift_date"),
        Index("ix_shifts_user_date", "user_id", "shift_date"),
    )

    # RESTRICT: TZ 5.1 — deactivating an employee must not delete the history, and a shift
    # is history. Deleting a `users` row is therefore blocked while shifts reference it.
    user_id: Mapped[int] = fk("users.id", ondelete="RESTRICT")
    shift_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    # Venue-local time (D12).
    start_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    end_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    # TZ 4.2: the source table keeps the number of hours in the cell.
    hours: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    is_opener: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    is_closer: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    status: Mapped[ShiftStatus] = mapped_column(
        shift_status_enum,
        server_default=ShiftStatus.PLANNED.value,
        nullable=False,
    )
    source: Mapped[ShiftSource] = mapped_column(
        shift_source_enum,
        server_default=ShiftSource.MANUAL.value,
        nullable=False,
    )
    created_by: Mapped[int | None] = fk("users.id", ondelete="SET NULL", nullable=True)

    user: Mapped[User] = relationship(foreign_keys="Shift.user_id")
