"""Service tables: notifications, audit log, import jobs (TZ 4.7, 6, 7)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, Index, Integer, column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, CreatedAtMixin, IdMixin, TimestampMixin, VenueScopedMixin, fk
from src.db.models.enums import NotificationStatus, notification_status_enum

#: Statuses in which a row still owns its `dedup_key` (decision D5, criterion 11.4).
#: A `sent`, `failed` or `cancelled` row is history and holds nothing back.
ACTIVE_NOTIFICATION_STATUSES: tuple[NotificationStatus, ...] = (
    NotificationStatus.SCHEDULED,
    NotificationStatus.SENDING,
)


class Notification(IdMixin, VenueScopedMixin, TimestampMixin, Base):
    """TZ 4.7 and section 6, extended by decision D5.

    The table is the single queue: APScheduler keeps no persistent job store, otherwise two
    sources of truth diverge. `dedup_key` makes acceptance criterion 11.4 (neither a lost nor
    a duplicated notification) testable.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        # Uniqueness of `dedup_key` holds over the live part of the queue only. A global
        # unique constraint would make two ordinary scenarios impossible: task 33 of the
        # plan (an opener is removed from a shift -> the row goes to `cancelled`, the
        # opener is assigned back -> the same key has to be schedulable again) and the
        # repeat of an event notification of TZ section 6 that has already been delivered.
        # This partial index is the `ON CONFLICT` target of `upsert_scheduled` (task 14).
        Index(
            "uq_notifications_dedup_key",
            "dedup_key",
            unique=True,
            postgresql_where=column("status").in_(
                [status.value for status in ACTIVE_NOTIFICATION_STATUSES]
            ),
        ),
        Index("ix_notifications_status_scheduled", "status", "scheduled_at"),
        Index("ix_notifications_entity", "entity", "entity_id"),
    )

    # NULL for a message addressed to the working group rather than to a person (TZ 5.10).
    user_id: Mapped[int | None] = fk("users.id", ondelete="CASCADE", nullable=True, index=True)
    # Target chat: personal chat of the user or `venue_settings.group_chat_id` (D5).
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Notification type from the registry in the service layer (plan, task 19), not an enum:
    # the closed list of section 6 grows by stages and lives next to its renderer.
    type: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    scheduled_at: Mapped[dt.datetime]
    sent_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    status: Mapped[NotificationStatus] = mapped_column(
        notification_status_enum,
        server_default=NotificationStatus.SCHEDULED.value,
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(nullable=True)

    # D5: idempotency key, "one live notification of this type for this object". The
    # constraint itself is the partial index above, not a column-level UNIQUE.
    dedup_key: Mapped[str] = mapped_column(nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    # What the notification is about: shift / checklist_run / order ... plus its id, so that
    # rescheduling and cancelling can find the rows without parsing the payload.
    entity: Mapped[str | None] = mapped_column(nullable=True)
    entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Moment the worker moved the row into `sending`; a row stuck longer than N minutes is
    # returned to `scheduled` with attempts + 1 (plan, task 31).
    claimed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)


class AuditLog(IdMixin, VenueScopedMixin, CreatedAtMixin, Base):
    """TZ 2 and 4.7: every data change is recorded with who, what and when."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_venue_created", "venue_id", "created_at"),
        Index("ix_audit_log_entity", "entity", "entity_id"),
    )

    user_id: Mapped[int | None] = fk("users.id", ondelete="SET NULL", nullable=True)
    entity: Mapped[str]
    entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str]
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class ImportJob(IdMixin, VenueScopedMixin, TimestampMixin, Base):
    """TZ 4.7 and section 7: one import run of one file."""

    __tablename__ = "import_jobs"
    __table_args__ = (Index("ix_import_jobs_venue_created", "venue_id", "created_at"),)

    user_id: Mapped[int | None] = fk("users.id", ondelete="SET NULL", nullable=True)
    # What is being imported: checklist / products / suppliers / recipes / schedule ...
    type: Mapped[str]
    file_id: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str]
    rows_total: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    rows_ok: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    rows_failed: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # TZ 7 p.7: the confirmed column mapping is stored so that the next monthly import of the
    # same file does not ask the wizard questions again.
    mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # TZ 7 p.6: an import can be rolled back within 24 hours, so the job keeps the ids of
    # every row it created.
    created_ids: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
