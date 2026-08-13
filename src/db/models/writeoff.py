"""Write-offs and their reasons (TZ 4.5, 5.6).

Stage 1 functionality; the tables are created now so the schema is frozen once.
Reasons are venue data — not a single one is shipped (TZ 1.4#6).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKeyConstraint, Index, Integer, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, CreatedAtMixin, IdMixin, VenueScopedMixin, fk
from src.db.models.enums import Unit, unit_enum


class WriteoffReason(IdMixin, VenueScopedMixin, Base):
    """TZ 4.5: writeoff_reasons(id, venue_id, name, requires_comment, requires_photo, ...)."""

    __tablename__ = "writeoff_reasons"
    __table_args__ = (
        # Target of the composite key from `writeoffs`.
        UniqueConstraint("venue_id", "id", name="uq_writeoff_reasons_venue_id_id"),
        Index("ix_writeoff_reasons_venue_order", "venue_id", "order_index"),
    )

    name: Mapped[str]
    requires_comment: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    requires_photo: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    is_active: Mapped[bool] = mapped_column(server_default="true", nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)


class Writeoff(IdMixin, VenueScopedMixin, CreatedAtMixin, Base):
    """TZ 4.5: writeoffs(...). No stock math, TZ 5.6 keeps the scheme deliberately simple."""

    __tablename__ = "writeoffs"
    __table_args__ = (
        # Product, reason and shift are all venue data, and the composite keys keep the
        # row inside one venue at database level (TZ 9, acceptance 11.3).
        ForeignKeyConstraint(
            ["venue_id", "product_id"],
            ["products.venue_id", "products.id"],
            name="fk_writeoffs_venue_product",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["venue_id", "reason_id"],
            ["writeoff_reasons.venue_id", "writeoff_reasons.id"],
            name="fk_writeoffs_venue_reason",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["venue_id", "shift_id"],
            ["shifts.venue_id", "shifts.id"],
            name="fk_writeoffs_venue_shift",
            ondelete="SET NULL (shift_id)",
        ),
        Index("ix_writeoffs_venue_created", "venue_id", "created_at"),
    )

    user_id: Mapped[int] = fk("users.id", ondelete="RESTRICT", index=True)
    # TZ 5.6: bound to the current shift of the employee, if there is one.
    shift_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    product_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit: Mapped[Unit] = mapped_column(unit_enum, nullable=False)
    reason_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(nullable=True)
    photo_file_id: Mapped[str | None] = mapped_column(nullable=True)

    # TZ 5.6: "you can cancel your own write-off within 30 minutes, later only through the
    # manager". Cancelling marks the row, it never deletes history (TZ 5.1).
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    cancelled_by: Mapped[int | None] = fk("users.id", ondelete="SET NULL", nullable=True)
