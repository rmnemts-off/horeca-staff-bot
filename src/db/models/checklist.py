"""Checklists: templates, items, runs (TZ 4.3, 5.4)."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKeyConstraint,
    Index,
    Integer,
    UniqueConstraint,
    column,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base, CreatedAtMixin, IdMixin, TimestampMixin, VenueScopedMixin, fk
from src.db.models.enums import (
    ChecklistType,
    RunStatus,
    checklist_type_enum,
    run_status_enum,
)

if TYPE_CHECKING:
    from src.db.models.shift import Shift


class ChecklistTemplate(IdMixin, VenueScopedMixin, TimestampMixin, Base):
    """TZ 4.3: checklist_templates(id, venue_id, type, name, version, is_active, updated_by).

    Editing rules are copy-on-write (decision B3); D7 keeps exactly one active template
    per (venue, type) with a partial unique index.
    """

    __tablename__ = "checklist_templates"
    __table_args__ = (
        UniqueConstraint(
            "venue_id", "type", "version", name="uq_checklist_templates_venue_type_version"
        ),
        # Target of the composite foreign key from `checklist_runs`: a run may only point
        # at a template of its own venue (TZ 9, acceptance 11.3).
        UniqueConstraint("venue_id", "id", name="uq_checklist_templates_venue_id_id"),
        Index(
            "uq_checklist_templates_active",
            "venue_id",
            "type",
            unique=True,
            postgresql_where=column("is_active", Boolean),
        ),
    )

    type: Mapped[ChecklistType] = mapped_column(checklist_type_enum, nullable=False)
    name: Mapped[str]
    version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    is_active: Mapped[bool] = mapped_column(server_default="true", nullable=False)
    updated_by: Mapped[int | None] = fk("users.id", ondelete="SET NULL", nullable=True)

    items: Mapped[list[ChecklistItem]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="(ChecklistItem.group_index, ChecklistItem.order_index)",
    )


class ChecklistItem(IdMixin, Base):
    """TZ 4.3 plus grouping from TZ 5.4 (decision D2).

    Child row: no venue_id, addressed only through its template (decision D9).
    A group does not exist without items, hence plain columns instead of a groups table.
    """

    __tablename__ = "checklist_items"
    __table_args__ = (
        Index("ix_checklist_items_template_order", "template_id", "group_index", "order_index"),
    )

    template_id: Mapped[int] = fk("checklist_templates.id", ondelete="CASCADE", index=True)
    # TZ 5.4: "checklist_items gets group_name and group_index".
    group_name: Mapped[str | None] = mapped_column(nullable=True)
    group_index: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str]
    requires_photo: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    requires_comment: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    is_critical: Mapped[bool] = mapped_column(server_default="false", nullable=False)

    template: Mapped[ChecklistTemplate] = relationship(back_populates="items")


class ChecklistRun(IdMixin, VenueScopedMixin, CreatedAtMixin, Base):
    """TZ 4.3 plus fields the interface of TZ 5.4 and 8.2 requires.

    Rendering always goes through `template_id` (plan, task 17), never through the currently
    active template — an edit must not change a run that is already in progress.
    """

    __tablename__ = "checklist_runs"
    __table_args__ = (
        # Both references are venue-composite: the database itself refuses a run that
        # points at the template or the shift of another venue (TZ 9, acceptance 11.3).
        # `SET NULL (shift_id)` is the PostgreSQL 15+ column list — nulling the whole key
        # would null `venue_id`, which is NOT NULL. Deleting a shift is a normal operation
        # (plan, tasks 29 and 33), and it must not take the completed checklist with it:
        # TZ 4.3 requires last month's report to stay put, TZ 5.9 reports over a period.
        ForeignKeyConstraint(
            ["venue_id", "shift_id"],
            ["shifts.venue_id", "shifts.id"],
            name="fk_checklist_runs_venue_shift",
            ondelete="SET NULL (shift_id)",
        ),
        ForeignKeyConstraint(
            ["venue_id", "template_id"],
            ["checklist_templates.venue_id", "checklist_templates.id"],
            name="fk_checklist_runs_venue_template",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_checklist_runs_shift_type",
            "shift_id",
            "type",
            unique=True,
            postgresql_where=column("shift_id").is_not(None),
        ),
        Index("ix_checklist_runs_venue_status", "venue_id", "status"),
    )

    # NULL for a `stock` checklist, which TZ 5.4 ties to venue settings rather than to a
    # shift, and after the shift the run belonged to has been deleted.
    shift_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # RESTRICT, not CASCADE: TZ 5.1 deactivates an employee and keeps the history.
    user_id: Mapped[int] = fk("users.id", ondelete="RESTRICT", index=True)
    template_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[ChecklistType] = mapped_column(checklist_type_enum, nullable=False)

    sent_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    status: Mapped[RunStatus] = mapped_column(
        run_status_enum,
        server_default=RunStatus.SENT.value,
        nullable=False,
    )
    done_items: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)

    # TZ 5.4: finishing with skipped items asks for a short reason and names the person
    # who pressed "done" (it may be a replacement, not `user_id`).
    skip_comment: Mapped[str | None] = mapped_column(nullable=True)
    completed_by: Mapped[int | None] = fk("users.id", ondelete="SET NULL", nullable=True)
    # TZ 8.2 and 5.4: the run lives in one message that is edited in place and can be
    # reopened from "My shift" after the employee swiped the message away.
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Read-only navigation: both keys are composite and share `venue_id`, so persistence
    # goes through the id columns (the repositories set them explicitly), never through
    # assigning an object to the attribute.
    template: Mapped[ChecklistTemplate] = relationship(viewonly=True)
    shift: Mapped[Shift | None] = relationship(viewonly=True)
    run_items: Mapped[list[ChecklistRunItem]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class ChecklistRunItem(IdMixin, Base):
    """TZ 4.3: checklist_run_items(id, run_id, item_id, is_done, done_at, photo, comment)."""

    __tablename__ = "checklist_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "item_id", name="uq_checklist_run_items_run_item"),
    )

    run_id: Mapped[int] = fk("checklist_runs.id", ondelete="CASCADE", index=True)
    item_id: Mapped[int] = fk("checklist_items.id", ondelete="RESTRICT")
    is_done: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    done_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    photo_file_id: Mapped[str | None] = mapped_column(nullable=True)
    comment: Mapped[str | None] = mapped_column(nullable=True)

    run: Mapped[ChecklistRun] = relationship(back_populates="run_items")
    item: Mapped[ChecklistItem] = relationship()
