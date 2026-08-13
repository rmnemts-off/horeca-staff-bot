"""Checklist service (plan, task 17; TZ 5.4, 8.1).

The fakes below are not stand-ins for "a database": they reproduce the two guarantees the
plan puts on the repository layer, because that is what the service is built on top of.

* :meth:`FakeRunItems.set_done` yields control *before* it looks at the row and then
  checks and writes without another await — one ``UPDATE ... WHERE is_done IS DISTINCT
  FROM :new``. A tap that finds the row already in the target state returns nothing.
* :meth:`FakeRuns.complete` behaves like ``UPDATE ... WHERE completed_at IS NULL``: the
  second "Done" gets nothing back.
* :meth:`FakeRuns.mark_overdue` and :meth:`FakeRuns.reopen` are conditional in the same
  way, which is what makes "one deadline, one message to the manager" testable at all.
* :meth:`FakeRuns.refresh_counters` recomputes ``done_items`` from the rows instead of
  adding one to it, so a counter that drifts is a bug the tests can see.

Without the awaits the concurrency tests would pass by accident — the two coroutines would
never interleave.

All four fakes are **scoped to one venue**, and the tables they read are shared between
venues, because that is the shape of the real thing (decision D9, TZ 3.3):

* ``checklist_runs`` owns ``venue_id``, so every statement of ``ChecklistRunRepo`` carries
  ``AND venue_id = :venue_id`` — :meth:`FakeRuns._scoped` is that predicate;
* ``checklist_templates`` owns it too, and ``ChecklistTemplateRepo`` builds every query out
  of ``for_venue()`` / ``by_id()`` — :meth:`FakeTemplates._scoped`;
* ``checklist_run_items`` owns none, so ``ChecklistRunItemRepo`` reaches its venue through a
  join to ``checklist_runs`` and by no other route — :meth:`FakeRunItems._parent` is that
  join;
* ``checklist_items`` owns none either, and ``ChecklistItemRepo`` is the same shape one
  level up: a join to ``checklist_templates`` — :meth:`FakeItems._parent`.

Addressing a row by its bare id used to be enough here, which made this the one class of bug
the fakes could not see: exactly the leak that showed up between venues in the schedule. A
run of another venue is not "refused" in either module — it is simply not there (TZ 9,
acceptance 11.3).

A fake that promises more than the repository is worse than no fake: it makes the service
tests green about behaviour that does not exist. Two methods are therefore copied from
``src/db/repositories/checklists.py`` rather than written the convenient way —
:meth:`FakeRuns.mark_started` moves the status only out of ``sent`` (the ``CASE WHEN`` of
the real statement), and :meth:`FakeRunItems.set_done` writes ``photo_file_id`` and
``comment`` exactly as the real ``UPDATE`` does, clearing both when a line is unticked.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from typing import Any

import pytest
from src.db.models import (
    Base,
    ChecklistItem,
    ChecklistRun,
    ChecklistRunItem,
    ChecklistTemplate,
    ChecklistType,
    MemberRole,
    RunStatus,
)
from src.services.access import (
    AccessContext,
    PermissionDeniedError,
    VenueMismatchError,
)
from src.services.checklists import (
    ChecklistService,
    CompletionOutcome,
    EmptyTemplateAlert,
    PendingItemsAlert,
    ReopenOutcome,
    RunCreation,
    ToggleOutcome,
    overdue_deadline,
)
from src.services.timezones import NaiveDatetimeError

VENUE_ID = 1
#: The bar next door: its rows live in the same tables and must be unreachable from here.
OTHER_VENUE_ID = 2
USER_ID = 7
MANAGER_ID = 9
SHIFT_ID = 42

MOMENT = dt.datetime(2026, 8, 13, 5, 50, tzinfo=dt.UTC)
SHIFT_START = dt.datetime(2026, 8, 13, 6, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------------------
# Model factories (declared here on purpose: tests/factories.py belongs to task 5a)
# --------------------------------------------------------------------------------------


def make_template(
    template_id: int,
    *,
    version: int = 1,
    is_active: bool = True,
    checklist_type: ChecklistType = ChecklistType.OPENING,
    venue_id: int = VENUE_ID,
) -> ChecklistTemplate:
    return ChecklistTemplate(
        id=template_id,
        venue_id=venue_id,
        type=checklist_type,
        name=f"template_v{version}",
        version=version,
        is_active=is_active,
    )


def make_item(
    item_id: int,
    template_id: int,
    *,
    text: str,
    group_index: int = 0,
    group_name: str | None = None,
    order_index: int = 0,
    is_critical: bool = False,
    requires_photo: bool = False,
    requires_comment: bool = False,
) -> ChecklistItem:
    return ChecklistItem(
        id=item_id,
        template_id=template_id,
        group_index=group_index,
        group_name=group_name,
        order_index=order_index,
        text=text,
        requires_photo=requires_photo,
        requires_comment=requires_comment,
        is_critical=is_critical,
    )


def make_run(
    run_id: int,
    template_id: int,
    *,
    shift_id: int | None = SHIFT_ID,
    status: RunStatus = RunStatus.SENT,
    total_items: int = 0,
    venue_id: int = VENUE_ID,
) -> ChecklistRun:
    return ChecklistRun(
        id=run_id,
        venue_id=venue_id,
        shift_id=shift_id,
        user_id=USER_ID,
        template_id=template_id,
        type=ChecklistType.OPENING,
        status=status,
        sent_at=MOMENT,
        done_items=0,
        total_items=total_items,
    )


# --------------------------------------------------------------------------------------
# Fake repositories
# --------------------------------------------------------------------------------------


class FakeTemplates:
    """`ChecklistTemplateRepository` — only the two reads the service performs.

    Scoped to one venue over a table every venue writes into: `checklist_templates` owns
    `venue_id`, so `get()` is `by_id()` (`WHERE id = :id AND venue_id = :venue_id`) and
    `get_active()` is `for_venue()` with the type and the flag on top. A fake that answered
    by id alone would hand the neighbour's wording to this venue's employees, and the
    forged `callback_data` of TZ 9 would open a template the bar has never seen.
    """

    def __init__(
        self,
        items: FakeItems,
        templates: Sequence[ChecklistTemplate] = (),
        *,
        venue_id: int = VENUE_ID,
        table: dict[int, ChecklistTemplate] | None = None,
    ) -> None:
        #: The whole `checklist_templates` table, every venue in it. `table` adopts the
        #: table of another repository, the way two venues share the real one.
        self.templates = (
            {template.id: template for template in templates} if table is None else table
        )
        self.venue_id = venue_id
        self._items = items
        # The child repository reaches its venue through this table and nowhere else.
        items.bind_templates(self.templates)

    @property
    def items(self) -> FakeItems:
        """The child repository bound to this one — same tables, same venue."""
        return self._items

    def neighbour(self, venue_id: int) -> FakeTemplates:
        """The same two tables seen through another venue's repositories (11.3).

        What makes "another venue's template" a row that genuinely exists and is genuinely
        unreachable from here, rather than a row nobody ever wrote.
        """
        items = FakeItems(venue_id=venue_id, table=self._items.items)
        return FakeTemplates(items, venue_id=venue_id, table=self.templates)

    def _scoped(self, template_id: int) -> ChecklistTemplate | None:
        """`WHERE id = :template_id AND venue_id = :venue_id`, in every statement.

        `None` covers both "no such template" and "not this venue's template": the second
        is not a refusal, the row is outside every query this repository can build (TZ 9).
        """
        template = self.templates.get(template_id)
        if template is None or template.venue_id != self.venue_id:
            return None
        return template

    async def get(self, template_id: int) -> ChecklistTemplate | None:
        return self._scoped(template_id)

    async def get_active(self, checklist_type: ChecklistType) -> ChecklistTemplate | None:
        for template in self.templates.values():
            if template.venue_id != self.venue_id:
                continue
            if template.type is checklist_type and template.is_active:
                return template
        return None

    async def list_versions(self, checklist_type: ChecklistType) -> Sequence[ChecklistTemplate]:
        raise NotImplementedError

    async def create(
        self,
        *,
        checklist_type: ChecklistType,
        name: str,
        version: int,
        updated_by: int | None = None,
    ) -> ChecklistTemplate:
        raise NotImplementedError

    async def update(self, template_id: int, **fields: Any) -> ChecklistTemplate | None:
        raise NotImplementedError

    async def deactivate(self, template_id: int) -> ChecklistTemplate | None:
        raise NotImplementedError

    async def is_referenced_by_runs(self, template_id: int) -> bool:
        raise NotImplementedError


class FakeItems:
    """`ChecklistItemRepository`; returns items in insertion order, unsorted on purpose.

    The service is the one that has to sort by (group_index, order_index) — a fake that
    handed them back already ordered would hide it.

    A child repository (decision D9): `checklist_items` owns no `venue_id`, so every read —
    including `get()`, which goes through `by_id()` — is a join to `checklist_templates`
    and answers nothing about a template of another venue. The writes stay
    `NotImplementedError`: the service never calls them, and a fake that implemented them
    anyway would be one more thing able to disagree with `ChecklistItemRepo` in silence.
    """

    def __init__(
        self,
        items: Sequence[ChecklistItem] = (),
        *,
        venue_id: int = VENUE_ID,
        table: list[ChecklistItem] | None = None,
    ) -> None:
        #: The whole `checklist_items` table, every venue in it: the scope is the `WHERE`
        #: and not the storage, so another venue's repository may adopt the same list.
        self.items: list[ChecklistItem] = list(items) if table is None else table
        self.venue_id = venue_id
        #: The templates table this child hangs off (decision D9). `checklist_items` has no
        #: `venue_id`, so the real repository resolves the venue through
        #: `checklist_templates`; `FakeTemplates` owns that dict and hands it over.
        self._templates: dict[int, ChecklistTemplate] = {}

    def bind_templates(self, templates: dict[int, ChecklistTemplate]) -> None:
        """Called by `FakeTemplates`: the same dict object, so later rows are visible here."""
        self._templates = templates

    @property
    def parent(self) -> type[Base]:
        return ChecklistTemplate

    @property
    def parent_fk(self) -> str:
        return "template_id"

    def _parent(self, template_id: int) -> ChecklistTemplate | None:
        """`JOIN checklist_templates ON ... WHERE checklist_templates.venue_id = :vid` (D9).

        `None` means the template is unknown *or* belongs to another venue — one answer for
        both, which is what makes a forged `template_id` address nothing (TZ 9).
        """
        template = self._templates.get(template_id)
        if template is None or template.venue_id != self.venue_id:
            return None
        return template

    async def get(self, item_id: int) -> ChecklistItem | None:
        item = next((row for row in self.items if row.id == item_id), None)
        if item is None or self._parent(item.template_id) is None:
            return None
        return item

    async def list_for_template(self, template_id: int) -> Sequence[ChecklistItem]:
        if self._parent(template_id) is None:
            return []
        return [item for item in self.items if item.template_id == template_id]

    async def count_for_template(self, template_id: int) -> int:
        return len(await self.list_for_template(template_id))

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
        raise NotImplementedError

    async def add_many(
        self,
        template_id: int,
        items: Sequence[dict[str, Any]],
    ) -> Sequence[ChecklistItem]:
        raise NotImplementedError

    async def update(self, item_id: int, **fields: Any) -> ChecklistItem | None:
        raise NotImplementedError

    async def delete(self, item_id: int) -> bool:
        raise NotImplementedError

    async def rename_group(self, template_id: int, group_index: int, name: str) -> int:
        raise NotImplementedError


class FakeRunItems:
    """`ChecklistRunItemRepository` with the conditional update of plan task 17.

    A child repository (decision D9): the rows own no `venue_id`, so every method resolves
    the parent run first and answers nothing when that run belongs to another venue. That
    resolution is the join the real statements carry, not an extra check on top of them.
    """

    def __init__(
        self,
        *,
        venue_id: int = VENUE_ID,
        rows: list[ChecklistRunItem] | None = None,
    ) -> None:
        #: The whole `checklist_run_items` table, every venue in it: the scope is the `WHERE`
        #: and not the storage, so another venue's repository may adopt the same list.
        self.rows: list[ChecklistRunItem] = [] if rows is None else rows
        self.venue_id = venue_id
        self.updates = 0
        #: The runs table this child hangs off (decision D9). `checklist_run_items` has no
        #: `venue_id`, so the real repository resolves the venue through `checklist_runs`
        #: before every read and write; `FakeRuns` owns that dict and hands it over.
        self._runs: dict[int, ChecklistRun] = {}

    def bind_runs(self, runs: dict[int, ChecklistRun]) -> None:
        """Called by `FakeRuns`: the same dict object, so later runs are visible here too."""
        self._runs = runs

    @property
    def parent(self) -> type[Base]:
        return ChecklistRun

    @property
    def parent_fk(self) -> str:
        return "run_id"

    def new_id(self) -> int:
        """Ids are unique across the table, which two venues share."""
        return max((row.id for row in self.rows), default=0) + 1

    def _parent(self, run_id: int) -> ChecklistRun | None:
        """`JOIN checklist_runs ON ... WHERE checklist_runs.venue_id = :venue_id` (D9).

        `None` means the run is unknown *or* belongs to another venue — the two are the same
        answer here, which is exactly what makes a forged `run_id` address nothing (TZ 9).
        """
        run = self._runs.get(run_id)
        if run is None or run.venue_id != self.venue_id:
            return None
        return run

    async def list_for_run(self, run_id: int) -> Sequence[ChecklistRunItem]:
        await asyncio.sleep(0)
        if self._parent(run_id) is None:
            return []
        return [row for row in self.rows if row.run_id == run_id]

    async def create_for_run(self, run_id: int, item_ids: Sequence[int]) -> int:
        # Straight from `ChecklistRunItemRepo.create_for_run`: an empty list is a no-op, and
        # a run that does not belong to this venue takes no rows at all — the check comes
        # first and the answer is 0, not a write into someone else's run (D9, TZ 9).
        if not item_ids:
            return 0
        if self._parent(run_id) is None:
            return 0
        for item_id in item_ids:
            self.rows.append(
                ChecklistRunItem(id=self.new_id(), run_id=run_id, item_id=item_id, is_done=False)
            )
        return len(item_ids)

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
        # The await stands for the round trip to PostgreSQL; everything after it is the
        # single statement `UPDATE ... WHERE is_done IS DISTINCT FROM :new` and therefore
        # runs without yielding.
        await asyncio.sleep(0)
        # The venue travels through the join to `checklist_runs` and is part of the same
        # `WHERE`: a forged `run_id` matches no row rather than flipping somebody else's.
        if self._parent(run_id) is None:
            return None
        row = next(
            (item for item in self.rows if item.run_id == run_id and item.item_id == item_id),
            None,
        )
        if row is None or row.is_done == is_done:
            return None
        row.is_done = is_done
        row.done_at = moment if is_done else None
        # Straight from `ChecklistRunItemRepo.set_done`: both columns are always written,
        # so unticking a line drops the photo and the note it was ticked with. A fake that
        # kept them would let the service promise evidence the database no longer has.
        row.photo_file_id = photo_file_id if is_done else None
        row.comment = comment if is_done else None
        self.updates += 1
        return row

    async def count_done(self, run_id: int) -> int:
        if self._parent(run_id) is None:
            return 0
        return sum(1 for row in self.rows if row.run_id == run_id and row.is_done)


class FakeRuns:
    """`ChecklistRunRepository`, scoped to one venue (TZ 3.3).

    `complete`, `mark_overdue`, `reopen` and `refresh_counters` carry the conditional-update
    guarantees; every one of them, and every read, also carries the venue predicate that
    `checklist_runs.venue_id` puts into the real statements.
    """

    def __init__(
        self,
        run_items: FakeRunItems,
        runs: Sequence[ChecklistRun] = (),
        *,
        venue_id: int = VENUE_ID,
        table: dict[int, ChecklistRun] | None = None,
    ) -> None:
        #: The whole `checklist_runs` table, every venue in it. `table` adopts the table of
        #: another repository, so two venues share one the way they share the real one.
        self.runs = {run.id: run for run in runs} if table is None else table
        self.venue_id = venue_id
        self._run_items = run_items
        self.completions = 0
        self.reopenings = 0
        # The child repository reaches its venue through this table and nowhere else.
        run_items.bind_runs(self.runs)

    @property
    def run_items(self) -> FakeRunItems:
        """The child repository bound to this one — same tables, same venue."""
        return self._run_items

    def neighbour(self, venue_id: int) -> FakeRuns:
        """The same two tables seen through another venue's repositories (11.3).

        What makes "another venue's run" a row that genuinely exists and is genuinely
        unreachable from here, rather than a row nobody ever wrote.
        """
        items = FakeRunItems(venue_id=venue_id, rows=self._run_items.rows)
        return FakeRuns(items, venue_id=venue_id, table=self.runs)

    def _scoped(self, run_id: int) -> ChecklistRun | None:
        """`WHERE id = :run_id AND venue_id = :venue_id`, in every statement of the repo.

        `None` covers both "no such run" and "not this venue's run": the second is not a
        refusal, the row is simply outside every query this repository can build (TZ 9).
        """
        run = self.runs.get(run_id)
        if run is None or run.venue_id != self.venue_id:
            return None
        return run

    async def get(self, run_id: int) -> ChecklistRun | None:
        await asyncio.sleep(0)
        return self._scoped(run_id)

    async def get_by_shift(
        self,
        shift_id: int,
        checklist_type: ChecklistType,
    ) -> ChecklistRun | None:
        return next(
            (
                run
                for run in self.runs.values()
                if run.venue_id == self.venue_id
                and run.shift_id == shift_id
                and run.type is checklist_type
            ),
            None,
        )

    async def list_for_user(
        self,
        user_id: int,
        *,
        date_from: dt.date,
        date_to: dt.date,
    ) -> Sequence[ChecklistRun]:
        raise NotImplementedError

    async def list_by_status(self, status: RunStatus) -> Sequence[ChecklistRun]:
        return [
            run
            for run in self.runs.values()
            if run.venue_id == self.venue_id and run.status is status
        ]

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
        # `venue_id` comes from the scope and never from the caller (TZ 3.3).
        run = ChecklistRun(
            id=max(self.runs, default=0) + 1,
            venue_id=self.venue_id,
            shift_id=shift_id,
            user_id=user_id,
            template_id=template_id,
            type=checklist_type,
            status=RunStatus.SENT,
            sent_at=sent_at,
            done_items=0,
            total_items=total_items,
        )
        self.runs[run.id] = run
        return run

    async def set_message(self, run_id: int, *, chat_id: int, message_id: int) -> None:
        run = self._scoped(run_id)
        if run is not None:
            run.chat_id = chat_id
            run.message_id = message_id

    async def mark_started(self, run_id: int, moment: dt.datetime) -> None:
        # `UPDATE ... WHERE started_at IS NULL`: the second caller changes nothing.
        run = self._scoped(run_id)
        if run is None or run.started_at is not None:
            return
        run.started_at = moment
        # The `CASE WHEN status = 'sent'` of the real statement: any other status is left
        # alone, so a tap does not quietly take a run back out of `overdue`.
        if run.status is RunStatus.SENT:
            run.status = RunStatus.IN_PROGRESS

    async def complete(
        self,
        run_id: int,
        *,
        moment: dt.datetime,
        status: RunStatus,
        completed_by: int,
        skip_comment: str | None = None,
    ) -> ChecklistRun | None:
        await asyncio.sleep(0)
        run = self._scoped(run_id)
        if run is None or run.completed_at is not None:
            return None
        run.completed_at = moment
        run.status = status
        run.completed_by = completed_by
        run.skip_comment = skip_comment
        self.completions += 1
        return run

    async def mark_overdue(self, run_id: int, moment: dt.datetime) -> ChecklistRun | None:
        # The compiled `WHERE` of `ChecklistRunRepo.mark_overdue`, all three arms of it:
        # `completed_at IS NULL AND status != 'overdue' AND (sent_at IS NULL OR sent_at <=
        # :moment)`. The third one is why `moment` is an argument at all — a run cannot be
        # late before it was sent, and a fake without that arm would let the scheduler pass
        # tests it fails against the database.
        run = self._scoped(run_id)
        if run is None or run.completed_at is not None or run.status is RunStatus.OVERDUE:
            return None
        if run.sent_at is not None and run.sent_at > moment:
            return None
        run.status = RunStatus.OVERDUE
        return run

    async def reopen(self, run_id: int) -> ChecklistRun | None:
        # `UPDATE ... WHERE completed_at IS NOT NULL`: an open run is not reopened, and
        # two managers pressing the button at once reopen it once (TZ 5.4).
        await asyncio.sleep(0)
        run = self._scoped(run_id)
        if run is None or run.completed_at is None:
            return None
        run.completed_at = None
        run.status = RunStatus.IN_PROGRESS
        self.reopenings += 1
        return run

    async def refresh_counters(self, run_id: int) -> ChecklistRun | None:
        run = self._scoped(run_id)
        if run is None:
            return None
        rows = [row for row in self._run_items.rows if row.run_id == run_id]
        run.done_items = sum(1 for row in rows if row.is_done)
        run.total_items = len(rows)
        return run


class FakeNotifier:
    def __init__(self) -> None:
        self.empty: list[EmptyTemplateAlert] = []
        self.skipped: list[PendingItemsAlert] = []
        self.overdue: list[PendingItemsAlert] = []

    async def checklist_template_empty(self, alert: EmptyTemplateAlert) -> None:
        self.empty.append(alert)

    async def checklist_finished_with_skips(self, alert: PendingItemsAlert) -> None:
        self.skipped.append(alert)

    async def checklist_overdue(self, alert: PendingItemsAlert) -> None:
        self.overdue.append(alert)


# --------------------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------------------


class Harness:
    def __init__(
        self,
        *,
        templates: Sequence[ChecklistTemplate],
        items: Sequence[ChecklistItem],
        runs: Sequence[ChecklistRun] = (),
    ) -> None:
        self.items = FakeItems(items)
        self.templates = FakeTemplates(self.items, templates)
        self.run_items = FakeRunItems()
        self.runs = FakeRuns(self.run_items, runs)
        self.notifier = FakeNotifier()
        self.service = ChecklistService(
            templates=self.templates,
            items=self.items,
            runs=self.runs,
            run_items=self.run_items,
            notifier=self.notifier,
        )

    def seed_run_items(self, run_id: int, item_ids: Sequence[int]) -> None:
        for item_id in item_ids:
            self.run_items.rows.append(
                ChecklistRunItem(
                    id=self.run_items.new_id(),
                    run_id=run_id,
                    item_id=item_id,
                    is_done=False,
                )
            )

    def seed_foreign_template(
        self,
        template_id: int = 800,
        *,
        item_ids: Sequence[int] = (81,),
        checklist_type: ChecklistType = ChecklistType.OPENING,
        is_active: bool = True,
    ) -> ChecklistTemplate:
        """The active template of the bar next door, in the shared tables.

        Its type is the one this venue also runs, so a fake without the venue predicate
        would hand it to `create_run()` here — the neighbour's wording sent to this bar's
        employees (TZ 3.3, TZ 9, acceptance 11.3).
        """
        template = make_template(
            template_id,
            checklist_type=checklist_type,
            is_active=is_active,
            venue_id=OTHER_VENUE_ID,
        )
        self.templates.templates[template.id] = template
        for order_index, item_id in enumerate(item_ids):
            self.items.items.append(
                make_item(
                    item_id, template.id, text=f"foreign_line_{item_id}", order_index=order_index
                )
            )
        return template

    def seed_foreign_run(
        self,
        run_id: int = 900,
        *,
        item_ids: Sequence[int] = (11,),
        shift_id: int | None = SHIFT_ID,
    ) -> ChecklistRun:
        """A run of the bar next door, written straight into the shared tables.

        A row that genuinely exists, with an id a forged `callback_data` could name and a
        `shift_id` this venue also uses — and that none of this venue's repositories may
        answer anything about (decision D9, TZ 9, acceptance 11.3).
        """
        run = make_run(
            run_id,
            template_id=1,
            shift_id=shift_id,
            total_items=len(item_ids),
            venue_id=OTHER_VENUE_ID,
        )
        self.runs.runs[run.id] = run
        self.seed_run_items(run.id, item_ids)
        return run


def opening_harness() -> Harness:
    """A run on template 1 while template 2 is the active one — the mid-shift edit case.

    Template 1: two groups, one of the items critical. Template 2 is what a manager saved
    while the employee already had the checklist open; nothing from it may show up.
    """
    frozen = make_template(1, version=1, is_active=False)
    edited = make_template(2, version=2, is_active=True)
    items = [
        make_item(11, 1, text="station_ice", group_index=0, group_name="station", order_index=1),
        make_item(
            12,
            1,
            text="station_berries",
            group_index=0,
            group_name="station",
            order_index=0,
            is_critical=True,
        ),
        make_item(13, 1, text="visual_lights", group_index=1, group_name="visual", order_index=0),
        make_item(21, 2, text="edited_item", group_index=0, group_name="station", order_index=0),
        make_item(22, 2, text="edited_extra", group_index=0, group_name="station", order_index=1),
    ]
    run = make_run(100, template_id=1, total_items=3)
    harness = Harness(templates=[frozen, edited], items=items, runs=[run])
    harness.seed_run_items(100, [11, 12, 13])
    return harness


def actor(role: MemberRole = MemberRole.MANAGER, *, venue_id: int = VENUE_ID) -> AccessContext:
    """One membership, as the permission guards of TZ 2 want to receive it."""
    return AccessContext(
        user_id=MANAGER_ID,
        telegram_id=500_000_000,
        venue_id=venue_id,
        member_id=1,
        role=role,
        full_name="Member 1",
    )


# --------------------------------------------------------------------------------------
# Render model: always from run.template_id
# --------------------------------------------------------------------------------------


async def test_view_is_built_from_the_run_template_not_the_active_one() -> None:
    harness = opening_harness()

    view = await harness.service.view(100)

    assert view is not None
    assert view.template_id == 1
    assert [item.item_id for item in view.items] == [12, 11, 13]
    assert all(item.text.startswith(("station_", "visual_")) for item in view.items)


async def test_view_groups_items_and_keeps_the_template_order() -> None:
    harness = opening_harness()

    view = await harness.service.view(100)

    assert view is not None
    assert [group.index for group in view.groups] == [0, 1]
    assert [group.name for group in view.groups] == ["station", "visual"]
    assert [item.item_id for item in view.groups[0].items] == [12, 11]
    assert view.total_items == 3
    assert view.done_items == 0


async def test_view_ignores_items_the_run_has_no_row_for() -> None:
    """An item added to the template after the run started does not join it mid-shift."""
    harness = opening_harness()
    harness.items.items.append(make_item(14, 1, text="late_addition", group_index=1, order_index=9))

    view = await harness.service.view(100)

    assert view is not None
    assert 14 not in [item.item_id for item in view.items]
    assert view.total_items == 3


async def test_view_of_an_unknown_run_is_none() -> None:
    harness = opening_harness()

    assert await harness.service.view(999) is None


# --------------------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------------------


async def test_create_run_snapshots_the_active_template() -> None:
    template = make_template(5)
    items = [
        make_item(51, 5, text="first_line", order_index=0),
        make_item(52, 5, text="second_line", order_index=1),
    ]
    harness = Harness(templates=[template], items=items)

    created = await harness.service.create_run(
        shift_id=SHIFT_ID,
        user_id=USER_ID,
        checklist_type=ChecklistType.OPENING,
        moment=MOMENT,
    )

    assert created.outcome is RunCreation.CREATED
    assert created.run is not None
    assert created.run.template_id == 5
    assert created.run.total_items == 2
    assert created.run.sent_at == MOMENT
    assert sorted(row.item_id for row in harness.run_items.rows) == [51, 52]
    assert harness.notifier.empty == []


async def test_empty_template_creates_no_run_and_tells_the_manager() -> None:
    """TZ 8.1: a checklist without items is not sent at all."""
    harness = Harness(templates=[make_template(5)], items=[])

    created = await harness.service.create_run(
        shift_id=SHIFT_ID,
        user_id=USER_ID,
        checklist_type=ChecklistType.OPENING,
        moment=MOMENT,
    )

    assert created.outcome is RunCreation.EMPTY_TEMPLATE
    assert created.run is None
    assert harness.runs.runs == {}
    assert harness.run_items.rows == []
    assert len(harness.notifier.empty) == 1
    assert harness.notifier.empty[0].template_id == 5
    assert harness.notifier.empty[0].user_id == USER_ID


async def test_missing_active_template_creates_no_run_and_tells_the_manager() -> None:
    harness = Harness(templates=[make_template(5, is_active=False)], items=[])

    created = await harness.service.create_run(
        shift_id=SHIFT_ID,
        user_id=USER_ID,
        checklist_type=ChecklistType.OPENING,
        moment=MOMENT,
    )

    assert created.outcome is RunCreation.NO_ACTIVE_TEMPLATE
    assert created.run is None
    assert len(harness.notifier.empty) == 1
    assert harness.notifier.empty[0].template_id is None


async def test_a_second_create_returns_the_existing_run() -> None:
    """TZ 5.4: one checklist is one run."""
    harness = opening_harness()

    created = await harness.service.create_run(
        shift_id=SHIFT_ID,
        user_id=USER_ID,
        checklist_type=ChecklistType.OPENING,
        moment=MOMENT,
    )

    assert created.outcome is RunCreation.ALREADY_EXISTS
    assert created.run is not None
    assert created.run.id == 100
    assert len(harness.runs.runs) == 1


async def test_remember_message_stores_the_pair_to_edit_later() -> None:
    harness = opening_harness()

    await harness.service.remember_message(run_id=100, chat_id=555, message_id=777)

    assert harness.runs.runs[100].chat_id == 555
    assert harness.runs.runs[100].message_id == 777


# --------------------------------------------------------------------------------------
# Toggle
# --------------------------------------------------------------------------------------


async def test_first_toggle_marks_the_run_started() -> None:
    harness = opening_harness()

    result = await harness.service.toggle(run_id=100, item_id=11, moment=MOMENT)

    run = harness.runs.runs[100]
    assert result.outcome is ToggleOutcome.TOGGLED
    assert result.is_done is True
    assert run.started_at == MOMENT
    assert run.status is RunStatus.IN_PROGRESS
    assert run.done_items == 1


async def test_a_later_toggle_does_not_move_started_at() -> None:
    harness = opening_harness()
    later = MOMENT + dt.timedelta(minutes=3)

    await harness.service.toggle(run_id=100, item_id=11, moment=MOMENT)
    await harness.service.toggle(run_id=100, item_id=12, moment=later)

    assert harness.runs.runs[100].started_at == MOMENT


async def test_toggle_flips_back_and_the_counter_follows() -> None:
    harness = opening_harness()

    await harness.service.toggle(run_id=100, item_id=11, moment=MOMENT)
    result = await harness.service.toggle(run_id=100, item_id=11, moment=MOMENT)

    assert result.outcome is ToggleOutcome.TOGGLED
    assert result.is_done is False
    assert harness.runs.runs[100].done_items == 0


async def test_a_concurrent_double_tap_flips_once_and_does_not_double_the_counter() -> None:
    """Plan, task 17 and test 42: `asyncio.gather` on the same button."""
    harness = opening_harness()

    first, second = await asyncio.gather(
        harness.service.toggle(run_id=100, item_id=11, moment=MOMENT),
        harness.service.toggle(run_id=100, item_id=11, moment=MOMENT),
    )

    outcomes = sorted([first.outcome, second.outcome])
    assert outcomes == sorted([ToggleOutcome.TOGGLED, ToggleOutcome.UNCHANGED])
    assert harness.run_items.updates == 1
    assert harness.runs.runs[100].done_items == 1
    for result in (first, second):
        assert result.view is not None
        assert result.view.done_items == 1


async def test_forty_toggles_of_the_same_item_leave_the_counter_consistent() -> None:
    harness = opening_harness()

    results = await asyncio.gather(
        *(harness.service.toggle(run_id=100, item_id=11, moment=MOMENT) for _ in range(40))
    )

    run = harness.runs.runs[100]
    done = [row for row in harness.run_items.rows if row.run_id == 100 and row.is_done]
    assert run.done_items == len(done)
    assert all(result.outcome is not ToggleOutcome.NOT_FOUND for result in results)


async def test_toggle_inside_a_finished_run_changes_nothing() -> None:
    harness = opening_harness()
    harness.runs.runs[100].completed_at = MOMENT
    harness.runs.runs[100].status = RunStatus.COMPLETED

    result = await harness.service.toggle(run_id=100, item_id=11, moment=MOMENT)

    assert result.outcome is ToggleOutcome.RUN_FINISHED
    assert harness.run_items.updates == 0
    assert result.view is not None
    assert result.view.done_items == 0


async def test_toggle_of_an_item_outside_the_run_is_refused() -> None:
    """A forged callback_data must not reach a row (TZ 9)."""
    harness = opening_harness()

    result = await harness.service.toggle(run_id=100, item_id=21, moment=MOMENT)

    assert result.outcome is ToggleOutcome.NOT_FOUND
    assert result.view is None
    assert harness.run_items.updates == 0


async def test_toggle_of_a_foreign_run_is_refused() -> None:
    harness = opening_harness()

    result = await harness.service.toggle(run_id=999, item_id=11, moment=MOMENT)

    assert result.outcome is ToggleOutcome.NOT_FOUND
    assert harness.run_items.updates == 0


async def test_toggle_carries_photo_and_comment_to_the_row() -> None:
    harness = opening_harness()

    await harness.service.toggle(
        run_id=100,
        item_id=11,
        moment=MOMENT,
        photo_file_id="file_id_stub",
        comment="short_note",
    )

    row = next(row for row in harness.run_items.rows if row.item_id == 11)
    assert row.photo_file_id == "file_id_stub"
    assert row.comment == "short_note"


async def test_unticking_drops_the_photo_and_the_comment_as_the_repository_does() -> None:
    """`set_done` writes both columns on every flip, so untick clears the evidence."""
    harness = opening_harness()
    await harness.service.toggle(
        run_id=100,
        item_id=11,
        moment=MOMENT,
        photo_file_id="file_id_stub",
        comment="short_note",
    )

    result = await harness.service.toggle(run_id=100, item_id=11, moment=MOMENT)

    row = next(row for row in harness.run_items.rows if row.item_id == 11)
    assert result.is_done is False
    assert row.done_at is None
    assert row.photo_file_id is None
    assert row.comment is None


# --------------------------------------------------------------------------------------
# Completion
# --------------------------------------------------------------------------------------


async def _tick_all(harness: Harness) -> None:
    for item_id in (11, 12, 13):
        await harness.service.toggle(run_id=100, item_id=item_id, moment=MOMENT)


async def test_completion_with_everything_ticked_tells_the_manager_nothing() -> None:
    harness = opening_harness()
    await _tick_all(harness)

    result = await harness.service.complete(run_id=100, completed_by=USER_ID, moment=MOMENT)

    assert result.outcome is CompletionOutcome.COMPLETED
    assert result.run is not None
    assert result.run.status is RunStatus.COMPLETED
    assert result.run.completed_at == MOMENT
    assert result.pending == ()
    assert harness.notifier.skipped == []


async def test_completion_with_gaps_asks_for_a_reason_first() -> None:
    harness = opening_harness()
    await harness.service.toggle(run_id=100, item_id=11, moment=MOMENT)

    result = await harness.service.complete(run_id=100, completed_by=USER_ID, moment=MOMENT)

    assert result.outcome is CompletionOutcome.NEEDS_REASON
    assert [item.item_id for item in result.pending] == [12, 13]
    assert harness.runs.runs[100].completed_at is None
    assert harness.notifier.skipped == []


async def test_a_blank_reason_is_not_a_reason() -> None:
    harness = opening_harness()

    result = await harness.service.complete(
        run_id=100, completed_by=USER_ID, moment=MOMENT, skip_comment="   "
    )

    assert result.outcome is CompletionOutcome.NEEDS_REASON
    assert harness.runs.runs[100].completed_at is None


async def test_completion_with_a_reason_is_skipped_and_alerts_the_manager() -> None:
    harness = opening_harness()
    await harness.service.toggle(run_id=100, item_id=11, moment=MOMENT)

    result = await harness.service.complete(
        run_id=100,
        completed_by=MANAGER_ID,
        moment=MOMENT,
        skip_comment="  delivery_late  ",
    )

    run = harness.runs.runs[100]
    assert result.outcome is CompletionOutcome.SKIPPED
    assert run.status is RunStatus.SKIPPED
    assert run.skip_comment == "delivery_late"
    assert run.completed_by == MANAGER_ID
    assert len(harness.notifier.skipped) == 1


async def test_the_skip_alert_puts_critical_items_first_and_marks_them() -> None:
    harness = opening_harness()

    await harness.service.complete(
        run_id=100, completed_by=USER_ID, moment=MOMENT, skip_comment="no_time"
    )

    alert = harness.notifier.skipped[0]
    assert [item.item_id for item in alert.items] == [12, 11, 13]
    assert alert.items[0].is_critical is True
    assert alert.has_critical is True
    assert [item.item_id for item in alert.critical] == [12]
    assert alert.skip_comment == "no_time"
    assert alert.run_id == 100


async def test_a_concurrent_double_done_completes_once_and_notifies_once() -> None:
    """Plan, task 17: two "Done" must not produce two completions and two messages."""
    harness = opening_harness()

    first, second = await asyncio.gather(
        harness.service.complete(
            run_id=100, completed_by=USER_ID, moment=MOMENT, skip_comment="no_time"
        ),
        harness.service.complete(
            run_id=100, completed_by=USER_ID, moment=MOMENT, skip_comment="no_time"
        ),
    )

    outcomes = sorted([first.outcome, second.outcome])
    assert outcomes == sorted([CompletionOutcome.SKIPPED, CompletionOutcome.ALREADY_FINISHED])
    assert harness.runs.completions == 1
    assert len(harness.notifier.skipped) == 1


async def test_completion_of_an_unknown_run_is_refused() -> None:
    harness = opening_harness()

    result = await harness.service.complete(run_id=999, completed_by=USER_ID, moment=MOMENT)

    assert result.outcome is CompletionOutcome.NOT_FOUND
    assert result.run is None


async def test_completing_a_finished_run_again_changes_nothing() -> None:
    harness = opening_harness()
    await _tick_all(harness)
    await harness.service.complete(run_id=100, completed_by=USER_ID, moment=MOMENT)

    later = MOMENT + dt.timedelta(minutes=5)
    result = await harness.service.complete(run_id=100, completed_by=USER_ID, moment=later)

    assert result.outcome is CompletionOutcome.ALREADY_FINISHED
    assert harness.runs.runs[100].completed_at == MOMENT
    assert harness.runs.completions == 1


# --------------------------------------------------------------------------------------
# Overdue (decision B2)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("minutes", [0, 10, 60])
def test_the_deadline_is_the_shift_start_plus_the_setting(minutes: int) -> None:
    assert overdue_deadline(SHIFT_START, minutes) == SHIFT_START + dt.timedelta(minutes=minutes)


async def test_nothing_is_reported_before_the_deadline() -> None:
    harness = opening_harness()

    alert = await harness.service.report_overdue(
        run_id=100,
        shift_start=SHIFT_START,
        overdue_minutes=60,
        moment=SHIFT_START + dt.timedelta(minutes=59),
    )

    assert alert is None
    assert harness.notifier.overdue == []


async def test_an_unfinished_run_past_the_deadline_names_what_is_missing() -> None:
    harness = opening_harness()
    await harness.service.toggle(run_id=100, item_id=13, moment=MOMENT)

    alert = await harness.service.report_overdue(
        run_id=100,
        shift_start=SHIFT_START,
        overdue_minutes=60,
        moment=SHIFT_START + dt.timedelta(minutes=61),
    )

    assert alert is not None
    assert [item.item_id for item in alert.items] == [12, 11]
    assert alert.items[0].is_critical is True
    assert alert.completed_by is None
    assert harness.notifier.overdue == [alert]
    assert harness.runs.runs[100].status is RunStatus.OVERDUE, "TZ 5.4 asks for the status"


async def test_three_scheduler_ticks_past_the_deadline_send_exactly_one_message() -> None:
    """The bug this test exists for: the deadline is true forever, the transition once.

    The scheduler asks every few minutes and the run stays unfinished, so a service that
    decides on "moment >= deadline" wakes the manager on every tick. What is allowed to
    decide is the status moving to `overdue`, and it moves once.
    """
    harness = opening_harness()
    past = SHIFT_START + dt.timedelta(minutes=61)

    alerts = [
        await harness.service.report_overdue(
            run_id=100,
            shift_start=SHIFT_START,
            overdue_minutes=60,
            moment=past + dt.timedelta(minutes=5 * tick),
        )
        for tick in range(3)
    ]

    assert [alert is not None for alert in alerts] == [True, False, False]
    assert len(harness.notifier.overdue) == 1
    assert harness.runs.runs[100].status is RunStatus.OVERDUE


async def test_a_tap_on_an_overdue_run_does_not_bring_it_back_to_in_progress() -> None:
    """`mark_started` moves the status only out of `sent`; the manager was already told."""
    harness = opening_harness()
    past = SHIFT_START + dt.timedelta(minutes=61)
    await harness.service.report_overdue(
        run_id=100, shift_start=SHIFT_START, overdue_minutes=60, moment=past
    )

    result = await harness.service.toggle(run_id=100, item_id=11, moment=past)

    run = harness.runs.runs[100]
    assert result.outcome is ToggleOutcome.TOGGLED
    assert run.started_at == past
    assert run.status is RunStatus.OVERDUE
    assert run.done_items == 1


async def test_a_finished_run_is_not_marked_overdue_either() -> None:
    harness = opening_harness()
    await _tick_all(harness)
    await harness.service.complete(run_id=100, completed_by=USER_ID, moment=MOMENT)

    await harness.service.report_overdue(
        run_id=100,
        shift_start=SHIFT_START,
        overdue_minutes=60,
        moment=SHIFT_START + dt.timedelta(hours=3),
    )

    assert harness.runs.runs[100].status is RunStatus.COMPLETED


async def test_a_finished_run_is_never_overdue() -> None:
    harness = opening_harness()
    await _tick_all(harness)
    await harness.service.complete(run_id=100, completed_by=USER_ID, moment=MOMENT)

    alert = await harness.service.report_overdue(
        run_id=100,
        shift_start=SHIFT_START,
        overdue_minutes=60,
        moment=SHIFT_START + dt.timedelta(hours=3),
    )

    assert alert is None
    assert harness.notifier.overdue == []


async def test_an_unknown_run_is_never_overdue() -> None:
    harness = opening_harness()

    alert = await harness.service.report_overdue(
        run_id=999,
        shift_start=SHIFT_START,
        overdue_minutes=60,
        moment=SHIFT_START + dt.timedelta(hours=3),
    )

    assert alert is None


# --------------------------------------------------------------------------------------
# Reopening (TZ 5.4: taken once, but a manager may reopen a run after a mistake)
# --------------------------------------------------------------------------------------


async def test_a_manager_reopens_a_finished_run() -> None:
    harness = opening_harness()
    await _tick_all(harness)
    await harness.service.complete(run_id=100, completed_by=USER_ID, moment=MOMENT)

    result = await harness.service.reopen(actor(), run_id=100)

    run = harness.runs.runs[100]
    assert result.outcome is ReopenOutcome.REOPENED
    assert run.completed_at is None
    assert run.status is RunStatus.IN_PROGRESS
    assert result.view is not None
    assert result.view.done_items == 3, "the ticks stay where the employee left them"


async def test_a_reopened_run_can_be_finished_again() -> None:
    """The point of reopening: the person made a mistake and gets the screen back."""
    harness = opening_harness()
    await _tick_all(harness)
    await harness.service.complete(run_id=100, completed_by=USER_ID, moment=MOMENT)
    await harness.service.reopen(actor(), run_id=100)

    later = MOMENT + dt.timedelta(minutes=5)
    untick = await harness.service.toggle(run_id=100, item_id=11, moment=later)
    retick = await harness.service.toggle(run_id=100, item_id=11, moment=later)
    result = await harness.service.complete(run_id=100, completed_by=USER_ID, moment=later)

    assert untick.is_done is False
    assert retick.is_done is True
    assert result.outcome is CompletionOutcome.COMPLETED
    assert harness.runs.runs[100].completed_at == later


async def test_staff_cannot_reopen_a_run() -> None:
    """TZ 2: the right is checked on the server, not by hiding the button."""
    harness = opening_harness()
    await _tick_all(harness)
    await harness.service.complete(run_id=100, completed_by=USER_ID, moment=MOMENT)

    with pytest.raises(PermissionDeniedError):
        await harness.service.reopen(actor(MemberRole.STAFF), run_id=100)

    assert harness.runs.runs[100].completed_at == MOMENT
    assert harness.runs.reopenings == 0


async def test_a_manager_of_another_venue_cannot_reopen_a_run() -> None:
    """Acceptance 11.3: a forged callback_data does not cross the venue border."""
    harness = opening_harness()
    await _tick_all(harness)
    await harness.service.complete(run_id=100, completed_by=USER_ID, moment=MOMENT)

    with pytest.raises(VenueMismatchError):
        await harness.service.reopen(actor(venue_id=VENUE_ID + 1), run_id=100)

    assert harness.runs.runs[100].completed_at == MOMENT
    assert harness.runs.reopenings == 0


async def test_reopening_a_run_that_was_never_finished_changes_nothing() -> None:
    harness = opening_harness()

    result = await harness.service.reopen(actor(), run_id=100)

    assert result.outcome is ReopenOutcome.NOT_FINISHED
    assert harness.runs.runs[100].status is RunStatus.SENT
    assert harness.runs.reopenings == 0


async def test_two_managers_reopening_at_once_reopen_once() -> None:
    harness = opening_harness()
    await _tick_all(harness)
    await harness.service.complete(run_id=100, completed_by=USER_ID, moment=MOMENT)

    first, second = await asyncio.gather(
        harness.service.reopen(actor(), run_id=100),
        harness.service.reopen(actor(), run_id=100),
    )

    outcomes = sorted([first.outcome, second.outcome])
    assert outcomes == sorted([ReopenOutcome.REOPENED, ReopenOutcome.NOT_FINISHED])
    assert harness.runs.reopenings == 1


async def test_reopening_an_unknown_run_is_refused() -> None:
    harness = opening_harness()

    result = await harness.service.reopen(actor(), run_id=999)

    assert result.outcome is ReopenOutcome.NOT_FOUND
    assert result.run is None
    assert result.view is None


# --------------------------------------------------------------------------------------
# The fakes themselves, held to the statements they stand for
# --------------------------------------------------------------------------------------
#
# Everything above is only as true as the fakes are. A fake that answers where the real
# `WHERE` matches nothing turns a service test into a test of the fake, and the difference
# shows up in production and nowhere else — so the two arms that are easiest to forget are
# asserted here directly, against the compiled statements in
# `tests/db/test_repositories_checklists.py`.


async def test_the_fake_refuses_to_mark_a_run_overdue_before_it_was_sent() -> None:
    """`(sent_at IS NULL OR sent_at <= :moment)` — the third arm of the real `WHERE`."""
    harness = opening_harness()
    run = harness.runs.runs[100]
    assert run.sent_at == MOMENT

    too_early = await harness.runs.mark_overdue(100, MOMENT - dt.timedelta(minutes=1))
    assert too_early is None
    assert run.status is RunStatus.SENT

    on_time = await harness.runs.mark_overdue(100, MOMENT)
    assert on_time is not None
    assert run.status is RunStatus.OVERDUE


async def test_the_fake_writes_no_rows_for_a_run_it_cannot_see() -> None:
    """Decision D9: the child rows reach their venue through `checklist_runs` or not at all."""
    harness = opening_harness()
    before = len(harness.run_items.rows)

    assert await harness.run_items.create_for_run(999, [11, 12]) == 0
    assert await harness.run_items.create_for_run(100, []) == 0
    assert len(harness.run_items.rows) == before

    assert await harness.run_items.create_for_run(100, [14]) == 1
    assert len(harness.run_items.rows) == before + 1


# --------------------------------------------------------------------------------------
# The venue predicate, which is what a bare id lookup used to be missing
# --------------------------------------------------------------------------------------
#
# `checklist_runs` owns `venue_id` and `checklist_run_items` reaches it through a join to
# the run (decision D9). Addressing a row by its id alone is the exact shape of the leak
# that happened between venues in the schedule, and a fake without the predicate is unable
# to see it: every test above would stay green while production hands over another bar's
# checklist (TZ 3.3, TZ 9, acceptance 11.3).


async def test_a_template_of_another_venue_is_addressed_by_nothing_here() -> None:
    """`checklist_templates` owns `venue_id`; both reads of the repository carry it."""
    harness = opening_harness()
    foreign = harness.seed_foreign_template()

    assert await harness.templates.get(foreign.id) is None
    # The neighbour's is active too, and of the same type: the predicate is what keeps
    # `get_active()` from picking it up on a venue that has its own.
    active = await harness.templates.get_active(ChecklistType.OPENING)
    assert active is harness.templates.templates[2]

    # Hidden by the predicate, not by an absence: its own venue reads the row back.
    neighbour = harness.templates.neighbour(OTHER_VENUE_ID)
    assert await neighbour.get(foreign.id) is foreign
    assert await neighbour.get_active(ChecklistType.OPENING) is foreign


async def test_the_items_of_another_venues_template_are_unreachable() -> None:
    """A child row is addressed through its parent or not at all (decision D9)."""
    harness = opening_harness()
    foreign = harness.seed_foreign_template(item_ids=(81, 82))

    assert list(await harness.items.list_for_template(foreign.id)) == []
    assert await harness.items.count_for_template(foreign.id) == 0
    assert await harness.items.get(81) is None

    # The rows are there, and the venue they belong to works with them normally.
    neighbour = harness.templates.neighbour(OTHER_VENUE_ID).items
    assert [item.id for item in await neighbour.list_for_template(foreign.id)] == [81, 82]
    assert await neighbour.count_for_template(foreign.id) == 2
    assert await neighbour.get(81) is not None


async def test_a_venue_without_a_template_of_its_own_borrows_nobodys() -> None:
    """TZ 8.1 end to end: the empty state, not the bar next door's checklist.

    The only active opening template in the table belongs to the neighbour. The manager is
    told the venue has none — which is the whole of what a fresh venue may be told.
    """
    harness = Harness(templates=[], items=[])
    harness.seed_foreign_template()

    created = await harness.service.create_run(
        shift_id=SHIFT_ID,
        user_id=USER_ID,
        checklist_type=ChecklistType.OPENING,
        moment=MOMENT,
    )

    assert created.outcome is RunCreation.NO_ACTIVE_TEMPLATE
    assert created.run is None
    assert harness.runs.runs == {}
    assert [alert.template_id for alert in harness.notifier.empty] == [None]


async def test_a_run_of_another_venue_is_addressed_by_nothing_here() -> None:
    harness = opening_harness()
    foreign = harness.seed_foreign_run()

    assert await harness.runs.get(foreign.id) is None
    on_the_shift = await harness.runs.get_by_shift(SHIFT_ID, ChecklistType.OPENING)
    assert on_the_shift is harness.runs.runs[100]
    assert [run.id for run in await harness.runs.list_by_status(RunStatus.SENT)] == [100]
    assert await harness.runs.refresh_counters(foreign.id) is None
    assert await harness.runs.mark_overdue(foreign.id, MOMENT) is None
    assert (
        await harness.runs.complete(
            foreign.id, moment=MOMENT, status=RunStatus.COMPLETED, completed_by=USER_ID
        )
        is None
    )
    await harness.runs.mark_started(foreign.id, MOMENT)
    await harness.runs.set_message(foreign.id, chat_id=555, message_id=777)

    # Not one of those calls touched the row.
    assert (foreign.started_at, foreign.chat_id, foreign.message_id) == (None, None, None)
    assert (foreign.status, foreign.completed_at) == (RunStatus.SENT, None)
    assert harness.runs.completions == 0

    # `reopen` needs a finished run to have anything to undo — it is refused too.
    foreign.completed_at = MOMENT
    foreign.status = RunStatus.COMPLETED
    assert await harness.runs.reopen(foreign.id) is None
    assert foreign.completed_at == MOMENT
    assert harness.runs.reopenings == 0

    # Hidden by the predicate, not by an absence: its own venue reads the row back.
    assert await harness.runs.neighbour(OTHER_VENUE_ID).get(foreign.id) is foreign


async def test_the_run_items_of_another_venues_run_are_unreachable() -> None:
    """A child row is addressed through its parent or not at all (decision D9)."""
    harness = opening_harness()
    foreign = harness.seed_foreign_run(item_ids=(11, 12))

    assert list(await harness.run_items.list_for_run(foreign.id)) == []
    assert await harness.run_items.count_done(foreign.id) == 0
    assert await harness.run_items.create_for_run(foreign.id, [13]) == 0
    flipped = await harness.run_items.set_done(
        run_id=foreign.id, item_id=11, is_done=True, moment=MOMENT
    )

    assert flipped is None
    assert harness.run_items.updates == 0
    assert [row.is_done for row in harness.run_items.rows if row.run_id == foreign.id] == [
        False,
        False,
    ]

    # The rows are there, and the venue they belong to works with them normally.
    neighbour = harness.runs.neighbour(OTHER_VENUE_ID).run_items
    assert [row.item_id for row in await neighbour.list_for_run(foreign.id)] == [11, 12]
    assert (
        await neighbour.set_done(run_id=foreign.id, item_id=11, is_done=True, moment=MOMENT)
        is not None
    )
    assert await neighbour.count_done(foreign.id) == 1


async def test_the_service_reaches_none_of_another_venues_run() -> None:
    """The forged `callback_data` of acceptance 11.3, end to end: no screen, no flip, no undo."""
    harness = opening_harness()
    foreign = harness.seed_foreign_run()
    foreign.completed_at = MOMENT
    foreign.status = RunStatus.COMPLETED

    toggled = await harness.service.toggle(run_id=foreign.id, item_id=11, moment=MOMENT)
    completed = await harness.service.complete(
        run_id=foreign.id, completed_by=USER_ID, moment=MOMENT
    )
    reopened = await harness.service.reopen(actor(), run_id=foreign.id)

    assert await harness.service.view(foreign.id) is None
    assert toggled.outcome is ToggleOutcome.NOT_FOUND
    assert completed.outcome is CompletionOutcome.NOT_FOUND
    # NOT_FOUND rather than a `VenueMismatchError`: the run never reaches the guard, because
    # the scoped repository has nothing to hand it.
    assert reopened.outcome is ReopenOutcome.NOT_FOUND
    assert harness.run_items.updates == 0
    assert harness.runs.reopenings == 0
    assert foreign.completed_at == MOMENT


async def test_a_foreign_run_on_the_same_shift_does_not_stand_in_for_this_venues() -> None:
    """TZ 5.4: one checklist of a shift is one run — of *this* venue's shift (TZ 3.3)."""
    harness = Harness(templates=[make_template(5)], items=[make_item(51, 5, text="first_line")])
    foreign = harness.seed_foreign_run(item_ids=(51,))

    created = await harness.service.create_run(
        shift_id=SHIFT_ID,
        user_id=USER_ID,
        checklist_type=ChecklistType.OPENING,
        moment=MOMENT,
    )

    assert created.outcome is RunCreation.CREATED
    assert created.run is not None
    assert created.run.venue_id == VENUE_ID
    assert created.run.id != foreign.id


# --------------------------------------------------------------------------------------
# Decision D12: a naive moment is a bug, not an assumption
# --------------------------------------------------------------------------------------


def test_a_naive_shift_start_is_refused_rather_than_assumed_to_be_utc() -> None:
    with pytest.raises(NaiveDatetimeError):
        overdue_deadline(SHIFT_START.replace(tzinfo=None), 60)


async def test_a_naive_moment_is_refused_at_every_entry_point() -> None:
    harness = opening_harness()
    naive = MOMENT.replace(tzinfo=None)

    with pytest.raises(NaiveDatetimeError):
        await harness.service.toggle(run_id=100, item_id=11, moment=naive)
    with pytest.raises(NaiveDatetimeError):
        await harness.service.complete(run_id=100, completed_by=USER_ID, moment=naive)
    with pytest.raises(NaiveDatetimeError):
        await harness.service.create_run(
            shift_id=None,
            user_id=USER_ID,
            checklist_type=ChecklistType.OPENING,
            moment=naive,
        )
    assert harness.run_items.updates == 0
    assert harness.runs.completions == 0
