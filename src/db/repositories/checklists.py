"""Checklists: templates, items, runs and run items (TZ 4.3, 5.4; plan task 12).

Three writes in this module are conditional updates rather than read-modify-write, and the
behaviour of the whole checklist screen rests on them (plan, task 17):

* :meth:`ChecklistRunItemRepo.set_done` updates ``WHERE is_done IS DISTINCT FROM :new`` and
  returns ``None`` when the row was already in the target state. Two taps on the same
  button — a lag spike, wet hands — are one flip and one re-render;
* :meth:`ChecklistRunRepo.complete` updates ``WHERE completed_at IS NULL`` and returns
  ``None`` when the run was already finished, which is what makes "one completion, one
  manager notification" true even when two "Done" presses race;
* :meth:`ChecklistRunRepo.mark_started` updates ``WHERE started_at IS NULL``, so the first
  tap wins and the losers change nothing;
* :meth:`ChecklistRunRepo.reopen` is the mirror image of ``complete()`` —
  ``WHERE completed_at IS NOT NULL`` — so two managers pressing "reopen" at once reopen the
  run once, exactly as two employees pressing "Done" finish it once.

The counter is never incremented: :meth:`ChecklistRunRepo.refresh_counters` recomputes
``done_items`` from ``checklist_run_items`` in one statement, so it cannot drift however
the taps interleave.

Items and run items carry no ``venue_id`` (decision D9): both repositories are
``ChildRepository`` and every query joins the venue-scoped parent, so a forged
``callback_data`` addresses nothing at all (TZ 9, acceptance 11.3).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Select, and_, case, delete, func, literal, or_, select, update
from sqlalchemy.exc import NoResultFound

from src.db.models import (
    ChecklistItem,
    ChecklistRun,
    ChecklistRunItem,
    ChecklistTemplate,
    ChecklistType,
    RunStatus,
    Shift,
)
from src.db.models.enums import run_status_enum
from src.db.repositories.base import BaseRepository, ChildRepository

#: Columns that `update(**fields)` may never touch: the identity of a row and its scope.
PROTECTED_COLUMNS = frozenset({"id", "venue_id"})


def _writable(
    model: type[Any],
    fields: Mapping[str, Any],
    *,
    exclude: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Keep the fields that are columns of `model` and are allowed to change."""
    columns = model.__table__.c
    blocked = PROTECTED_COLUMNS | exclude
    return {
        name: value for name, value in fields.items() if name in columns and name not in blocked
    }


def _ordered_items() -> tuple[Any, ...]:
    """(group, position, id) — the order TZ 5.4 renders a checklist in (decision D2)."""
    return (ChecklistItem.group_index, ChecklistItem.order_index, ChecklistItem.id)


# --------------------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------------------


class ChecklistTemplateRepo(BaseRepository[ChecklistTemplate]):
    """`checklist_templates` (TZ 4.3). Versioning policy is the service's (decision B3)."""

    model = ChecklistTemplate

    async def get(self, template_id: int) -> ChecklistTemplate | None:
        return await self._one(self.by_id(template_id))

    async def get_active(self, checklist_type: ChecklistType) -> ChecklistTemplate | None:
        """The current template of a type; one per (venue, type) by decision D7."""
        return await self._one(
            self.for_venue().where(
                ChecklistTemplate.type == checklist_type,
                ChecklistTemplate.is_active,
            )
        )

    async def list_versions(self, checklist_type: ChecklistType) -> Sequence[ChecklistTemplate]:
        stmt = (
            self.for_venue()
            .where(ChecklistTemplate.type == checklist_type)
            .order_by(ChecklistTemplate.version.desc(), ChecklistTemplate.id.desc())
        )
        rows = await self.session.scalars(stmt)
        return rows.all()

    async def create(
        self,
        *,
        checklist_type: ChecklistType,
        name: str,
        version: int,
        updated_by: int | None = None,
    ) -> ChecklistTemplate:
        template = ChecklistTemplate(
            venue_id=self.venue_id,
            type=checklist_type,
            name=name,
            version=version,
            updated_by=updated_by,
        )
        self.session.add(template)
        await self.session.flush()
        return template

    async def update(self, template_id: int, **fields: Any) -> ChecklistTemplate | None:
        values = _writable(ChecklistTemplate, fields)
        if not values:
            return await self.get(template_id)
        stmt = (
            update(ChecklistTemplate)
            .where(
                ChecklistTemplate.id == template_id,
                ChecklistTemplate.venue_id == self.venue_id,
            )
            .values(**values)
            .returning(ChecklistTemplate.id)
            .execution_options(synchronize_session=False)
        )
        changed = await self.session.scalars(stmt)
        if changed.first() is None:
            return None
        return await self.get(template_id)

    async def deactivate(self, template_id: int) -> ChecklistTemplate | None:
        """Free the partial unique index of D7 so that a new version can become active."""
        return await self.update(template_id, is_active=False)

    async def is_referenced_by_runs(self, template_id: int) -> bool:
        """Decision B3: once a run points at a template, editing it forks a new version."""
        stmt = (
            select(ChecklistRun.id)
            .where(
                ChecklistRun.venue_id == self.venue_id,
                ChecklistRun.template_id == template_id,
            )
            .limit(1)
        )
        found = await self.session.scalars(stmt)
        return found.first() is not None

    async def _one(self, statement: Select[tuple[ChecklistTemplate]]) -> ChecklistTemplate | None:
        rows = await self.session.scalars(statement.execution_options(populate_existing=True))
        return rows.first()


# --------------------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------------------


class ChecklistItemRepo(ChildRepository[ChecklistItem, ChecklistTemplate]):
    """`checklist_items` — a child of `checklist_templates` (decision D9)."""

    model = ChecklistItem
    parent = ChecklistTemplate
    parent_fk = "template_id"

    async def get(self, item_id: int) -> ChecklistItem | None:
        rows = await self.session.scalars(self.by_id(item_id))
        return rows.first()

    async def list_for_template(self, template_id: int) -> Sequence[ChecklistItem]:
        stmt = self.for_parent(template_id).order_by(*_ordered_items())
        rows = await self.session.scalars(stmt)
        return rows.all()

    async def count_for_template(self, template_id: int) -> int:
        """TZ 8.1: a template with zero items is never sent, so the count is a real answer."""
        stmt = self.for_parent(template_id).with_only_columns(func.count())
        found = await self.session.scalar(stmt)
        return int(found or 0)

    async def add(
        self,
        *,
        template_id: int,
        text: str,
        group_name: str | None,
        group_index: int,
        order_index: int,
        requires_photo: bool = False,
        requires_comment: bool = False,
        is_critical: bool = False,
    ) -> ChecklistItem:
        await self._owned_template(template_id)
        item = ChecklistItem(
            template_id=template_id,
            text=text,
            group_name=group_name,
            group_index=group_index,
            order_index=order_index,
            requires_photo=requires_photo,
            requires_comment=requires_comment,
            is_critical=is_critical,
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def add_many(
        self,
        template_id: int,
        items: Sequence[dict[str, Any]],
    ) -> Sequence[ChecklistItem]:
        """Bulk entry of decision B6: one message from the manager, forty lines at once."""
        await self._owned_template(template_id)
        rows = [
            ChecklistItem(
                template_id=template_id,
                **_writable(ChecklistItem, item, exclude=frozenset({"template_id"})),
            )
            for item in items
        ]
        if not rows:
            return []
        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def update(self, item_id: int, **fields: Any) -> ChecklistItem | None:
        values = _writable(ChecklistItem, fields)
        if not values:
            return await self.get(item_id)
        stmt = (
            update(ChecklistItem)
            .where(ChecklistItem.id.in_(self.by_id(item_id).with_only_columns(ChecklistItem.id)))
            .values(**values)
            .returning(ChecklistItem.id)
            .execution_options(synchronize_session=False)
        )
        changed = await self.session.scalars(stmt)
        if changed.first() is None:
            return None
        reloaded = await self.session.scalars(
            self.by_id(item_id).execution_options(populate_existing=True)
        )
        return reloaded.first()

    async def delete(self, item_id: int) -> bool:
        stmt = (
            delete(ChecklistItem)
            .where(ChecklistItem.id.in_(self.by_id(item_id).with_only_columns(ChecklistItem.id)))
            .returning(ChecklistItem.id)
            .execution_options(synchronize_session=False)
        )
        removed = await self.session.scalars(stmt)
        return removed.first() is not None

    async def rename_group(self, template_id: int, group_index: int, name: str) -> int:
        """A group has no table of its own (decision D2): renaming it is an update of items."""
        stmt = (
            update(ChecklistItem)
            .where(
                ChecklistItem.id.in_(
                    self.for_parent(template_id).with_only_columns(ChecklistItem.id)
                ),
                ChecklistItem.group_index == group_index,
            )
            .values(group_name=name)
            .returning(ChecklistItem.id)
            .execution_options(synchronize_session=False)
        )
        renamed = await self.session.scalars(stmt)
        return len(renamed.all())

    async def _owned_template(self, template_id: int) -> int:
        """The parent id, or `NoResultFound` when it belongs to another venue (TZ 9).

        A child row carries no `venue_id`, so this is the only place the ownership of a
        write can be established; `add()` promises a row back and therefore cannot answer
        with `None`.
        """
        stmt = select(ChecklistTemplate.id).where(
            ChecklistTemplate.id == template_id,
            ChecklistTemplate.venue_id == self.venue_id,
        )
        found = await self.session.scalars(stmt)
        owned = found.first()
        if owned is None:
            raise NoResultFound(
                f"checklist template {template_id} does not belong to venue {self.venue_id}"
            )
        return owned


# --------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------


class ChecklistRunRepo(BaseRepository[ChecklistRun]):
    """`checklist_runs` (TZ 4.3, 5.4). Every state change here is a conditional update."""

    model = ChecklistRun

    async def get(self, run_id: int) -> ChecklistRun | None:
        return await self._reload(run_id)

    async def get_by_shift(
        self,
        shift_id: int,
        checklist_type: ChecklistType,
    ) -> ChecklistRun | None:
        """TZ 5.4: one checklist of a shift is one run (partial unique index on the pair)."""
        stmt = self.for_venue().where(
            ChecklistRun.shift_id == shift_id,
            ChecklistRun.type == checklist_type,
        )
        rows = await self.session.scalars(stmt.execution_options(populate_existing=True))
        return rows.first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        date_from: dt.date,
        date_to: dt.date,
    ) -> Sequence[ChecklistRun]:
        """Runs of one employee over a range of *shift* dates.

        The range is a range of dates, and the only date a run has is the date of its
        shift; `sent_at` is an instant in UTC and would answer a different question near
        midnight (TZ 3.4). A `stock` run, which TZ 5.4 ties to venue settings rather than
        to a shift, therefore does not appear here.
        """
        stmt = (
            self.for_venue()
            .join(
                Shift,
                and_(Shift.id == ChecklistRun.shift_id, Shift.venue_id == ChecklistRun.venue_id),
            )
            .where(
                ChecklistRun.user_id == user_id,
                Shift.shift_date >= date_from,
                Shift.shift_date <= date_to,
            )
            .order_by(Shift.shift_date, ChecklistRun.id)
        )
        rows = await self.session.scalars(stmt)
        return rows.all()

    async def list_by_status(self, status: RunStatus) -> Sequence[ChecklistRun]:
        stmt = self.for_venue().where(ChecklistRun.status == status).order_by(ChecklistRun.id)
        rows = await self.session.scalars(stmt)
        return rows.all()

    async def create(
        self,
        *,
        shift_id: int | None,
        user_id: int,
        template_id: int,
        checklist_type: ChecklistType,
        total_items: int,
        sent_at: dt.datetime | None = None,
    ) -> ChecklistRun:
        """The composite foreign keys refuse a template or a shift of another venue."""
        run = ChecklistRun(
            venue_id=self.venue_id,
            shift_id=shift_id,
            user_id=user_id,
            template_id=template_id,
            type=checklist_type,
            total_items=total_items,
            sent_at=sent_at,
            status=RunStatus.SENT,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def set_message(self, run_id: int, *, chat_id: int, message_id: int) -> None:
        """TZ 8.2: the run lives in one message that is edited instead of re-sent."""
        stmt = (
            update(ChecklistRun)
            .where(ChecklistRun.id == run_id, ChecklistRun.venue_id == self.venue_id)
            .values(chat_id=chat_id, message_id=message_id)
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(stmt)

    async def mark_started(self, run_id: int, moment: dt.datetime) -> None:
        """First tap, not delivery (schema notes): `WHERE started_at IS NULL`.

        The status moves `sent` -> `in_progress` in the same statement; any other status is
        left alone, so a run already marked `overdue` does not lose that fact by being
        touched.
        """
        stmt = (
            update(ChecklistRun)
            .where(
                ChecklistRun.id == run_id,
                ChecklistRun.venue_id == self.venue_id,
                ChecklistRun.started_at.is_(None),
            )
            .values(
                started_at=moment,
                status=case(
                    (
                        ChecklistRun.status == RunStatus.SENT,
                        literal(RunStatus.IN_PROGRESS, run_status_enum),
                    ),
                    else_=ChecklistRun.status,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(stmt)

    async def complete(
        self,
        run_id: int,
        *,
        moment: dt.datetime,
        status: RunStatus,
        completed_by: int,
        skip_comment: str | None = None,
    ) -> ChecklistRun | None:
        """One completion, one notification: `WHERE completed_at IS NULL`.

        `None` means the run was already finished — the caller lost the race with another
        "Done" and must not notify the manager a second time (TZ 5.4).
        """
        stmt = (
            update(ChecklistRun)
            .where(
                ChecklistRun.id == run_id,
                ChecklistRun.venue_id == self.venue_id,
                ChecklistRun.completed_at.is_(None),
            )
            .values(
                completed_at=moment,
                status=status,
                completed_by=completed_by,
                skip_comment=skip_comment,
            )
            .returning(ChecklistRun.id)
            .execution_options(synchronize_session=False)
        )
        finished = await self.session.scalars(stmt)
        if finished.first() is None:
            return None
        return await self._reload(run_id)

    async def mark_overdue(self, run_id: int, moment: dt.datetime) -> ChecklistRun | None:
        """Record that a run passed its deadline (TZ 5.4, section 6; contract addition).

        `list_by_status(OVERDUE)` had no way of ever seeing a row: `complete()` is the only
        other status write and it requires `completed_by` and stamps `completed_at`, while
        an overdue run is by definition not finished. The status is in the enum of TZ 4.3
        and TZ 5.4 asks for it, so it has to be writable.

        Conditional twice over: a finished run is never overdue, and a run cannot become
        overdue before it was sent. `None` means neither happened — the caller notifies
        nobody, which is the same "one event, one notification" rule as `complete()`.
        """
        stmt = (
            update(ChecklistRun)
            .where(
                ChecklistRun.id == run_id,
                ChecklistRun.venue_id == self.venue_id,
                ChecklistRun.completed_at.is_(None),
                ChecklistRun.status != RunStatus.OVERDUE,
                or_(ChecklistRun.sent_at.is_(None), ChecklistRun.sent_at <= moment),
            )
            .values(status=RunStatus.OVERDUE)
            .returning(ChecklistRun.id)
            .execution_options(synchronize_session=False)
        )
        marked = await self.session.scalars(stmt)
        if marked.first() is None:
            return None
        return await self._reload(run_id)

    async def reopen(self, run_id: int) -> ChecklistRun | None:
        """Undo a completion so the person can finish the run properly (TZ 5.4).

        The mirror image of `complete()`, and conditional in the same way: the row has to be
        finished for there to be anything to undo, so the predicate is
        `completed_at IS NOT NULL`. `None` means the run is unknown, belongs to another
        venue, or was never finished — two managers pressing "reopen" at once reopen it
        once, exactly as two employees pressing "Done" finish it once.

        The status goes back to `in_progress` rather than to `sent`: the ticks are still
        there and `started_at` keeps the moment the first one was made, so the run is
        started by any reading of the word.

        `completed_by` and `skip_comment` are deliberately left standing. They record who
        finished the run and why lines were skipped; that happened, and reopening does not
        unhappen it. Schema note Q4 gives reopening no columns of its own — the fact belongs
        in `audit_log` and is written by the caller.
        """
        stmt = (
            update(ChecklistRun)
            .where(
                ChecklistRun.id == run_id,
                ChecklistRun.venue_id == self.venue_id,
                ChecklistRun.completed_at.is_not(None),
            )
            .values(completed_at=None, status=RunStatus.IN_PROGRESS)
            .returning(ChecklistRun.id)
            .execution_options(synchronize_session=False)
        )
        reopened = await self.session.scalars(stmt)
        if reopened.first() is None:
            return None
        return await self._reload(run_id)

    async def refresh_counters(self, run_id: int) -> ChecklistRun | None:
        """Recompute the counters from `checklist_run_items` — never increment them.

        Both numbers come from the rows of the run itself in a single statement, so two
        taps that interleave cannot leave the counter out of step with the ticks.
        """
        stmt = (
            update(ChecklistRun)
            .where(ChecklistRun.id == run_id, ChecklistRun.venue_id == self.venue_id)
            .values(
                done_items=(
                    select(func.count())
                    .select_from(ChecklistRunItem)
                    .where(
                        ChecklistRunItem.run_id == ChecklistRun.id,
                        ChecklistRunItem.is_done,
                    )
                    .scalar_subquery()
                ),
                total_items=(
                    select(func.count())
                    .select_from(ChecklistRunItem)
                    .where(ChecklistRunItem.run_id == ChecklistRun.id)
                    .scalar_subquery()
                ),
            )
            .returning(ChecklistRun.id)
            .execution_options(synchronize_session=False)
        )
        refreshed = await self.session.scalars(stmt)
        if refreshed.first() is None:
            return None
        return await self._reload(run_id)

    async def _reload(self, run_id: int) -> ChecklistRun | None:
        stmt = self.by_id(run_id).execution_options(populate_existing=True)
        rows = await self.session.scalars(stmt)
        return rows.first()


# --------------------------------------------------------------------------------------
# Run items
# --------------------------------------------------------------------------------------


class ChecklistRunItemRepo(ChildRepository[ChecklistRunItem, ChecklistRun]):
    """`checklist_run_items` — a child of `checklist_runs` (decision D9)."""

    model = ChecklistRunItem
    parent = ChecklistRun
    parent_fk = "run_id"

    async def list_for_run(self, run_id: int) -> Sequence[ChecklistRunItem]:
        stmt = self.for_parent(run_id).order_by(ChecklistRunItem.id)
        rows = await self.session.scalars(stmt)
        return rows.all()

    async def create_for_run(self, run_id: int, item_ids: Sequence[int]) -> int:
        """Pin the composition of a run: an edit of the template must not reach it (B3).

        Returns the number of rows written, and zero for a run of another venue — a
        foreign id addresses nothing here rather than writing into someone else's run.
        """
        if not item_ids:
            return 0
        stmt = select(ChecklistRun.id).where(
            ChecklistRun.id == run_id,
            ChecklistRun.venue_id == self.venue_id,
        )
        found = await self.session.scalars(stmt)
        if found.first() is None:
            return 0
        rows = [ChecklistRunItem(run_id=run_id, item_id=item_id) for item_id in item_ids]
        self.session.add_all(rows)
        await self.session.flush()
        return len(rows)

    async def set_done(
        self,
        *,
        run_id: int,
        item_id: int,
        is_done: bool,
        moment: dt.datetime,
        photo_file_id: str | None = None,
        comment: str | None = None,
    ) -> ChecklistRunItem | None:
        """One `UPDATE ... WHERE is_done IS DISTINCT FROM :new`; `None` when nothing moved.

        That is the whole of the double-tap guarantee (plan, task 17): the second tap of a
        pair matches no row, returns `None` and the caller treats it as a repaint. The
        venue travels through the join to `checklist_runs`, so a forged `run_id` also
        matches nothing.
        """
        stmt = (
            update(ChecklistRunItem)
            .where(
                ChecklistRunItem.id.in_(
                    self.for_parent(run_id).with_only_columns(ChecklistRunItem.id)
                ),
                ChecklistRunItem.item_id == item_id,
                ChecklistRunItem.is_done.is_distinct_from(is_done),
            )
            .values(
                is_done=is_done,
                done_at=moment if is_done else None,
                photo_file_id=photo_file_id if is_done else None,
                comment=comment if is_done else None,
            )
            .returning(ChecklistRunItem.id)
            .execution_options(synchronize_session=False)
        )
        changed = await self.session.scalars(stmt)
        if changed.first() is None:
            return None
        reloaded = await self.session.scalars(
            self.for_parent(run_id)
            .where(ChecklistRunItem.item_id == item_id)
            .execution_options(populate_existing=True)
        )
        return reloaded.first()

    async def count_done(self, run_id: int) -> int:
        stmt = (
            self.for_parent(run_id).where(ChecklistRunItem.is_done).with_only_columns(func.count())
        )
        found = await self.session.scalar(stmt)
        return int(found or 0)


__all__ = [
    "PROTECTED_COLUMNS",
    "ChecklistItemRepo",
    "ChecklistRunItemRepo",
    "ChecklistRunRepo",
    "ChecklistTemplateRepo",
]
