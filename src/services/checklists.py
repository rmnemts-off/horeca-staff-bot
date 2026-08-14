"""Checklist runs: render model, toggle, completion, overdue (TZ 5.4, plan task 17).

Four rules of TZ 5.4 shape everything below, and each of them exists because of a way the
naive version breaks in a real bar:

* **The render model is always built from `run.template_id`.** Never from the currently
  active template. A manager editing the checklist while an employee has it open would
  otherwise move the items under their hands mid-shift; TZ 4.3 wants last month's report
  to stay put for the same reason. Copy-on-write (decision B3) freezes the version a run
  points at, and the composition of a run is additionally pinned to the rows created in
  `checklist_run_items` when the run was created.
* **Toggling is idempotent.** Two taps on the same button arrive concurrently often enough
  (a lag spike, a double tap with wet hands). The repository performs a single
  ``UPDATE ... WHERE is_done IS DISTINCT FROM :new`` and returns nothing when the row was
  already in the target state, so the second tap is a no-op instead of a second flip. The
  counter is never incremented: :meth:`ChecklistRunRepository.refresh_counters` recomputes
  ``done_items`` from ``checklist_run_items``, so it cannot drift no matter how the taps
  interleave.
* **Completion happens at most once.** The repository completes with
  ``UPDATE ... WHERE completed_at IS NULL`` and returns ``None`` when the run was already
  finished, so a double "Done" produces one completion and one manager notification.
* **The overdue alert happens at most once, for the same reason.** The scheduler ticks
  every few minutes and every tick after the deadline sees the same unfinished run, so
  "past the deadline" cannot be what decides to send. The status is: TZ 5.4 asks for
  ``overdue`` in so many words, and
  :meth:`ChecklistRunRepository.mark_overdue` moves it with
  ``UPDATE ... WHERE completed_at IS NULL AND status != 'overdue'``. Only the tick that
  actually moved the status notifies; the rest get ``None`` back and stay silent.
* **A checklist without items is not sent at all** (TZ 8.1): no run is created, the manager
  is told the template is empty, and the employee gets nothing rather than an empty message.

The service returns structures; wording is task 21's. Nothing here calls the clock: every
entry point takes ``moment`` (UTC), because the venue's timezone conversion lives in one
module and one module only (decision D12).
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from src.db.models import (
    ChecklistItem,
    ChecklistRun,
    ChecklistRunItem,
    ChecklistType,
    RunStatus,
)
from src.db.repositories.protocols import (
    ChecklistItemRepository,
    ChecklistRunItemRepository,
    ChecklistRunRepository,
    ChecklistTemplateRepository,
)
from src.services.access import AccessContext, require_manager, require_venue
from src.services.timezones import ensure_utc

# --------------------------------------------------------------------------------------
# Render model
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemView:
    """One checklist line: wording from the template, state from the run."""

    item_id: int
    text: str
    group_index: int
    group_name: str | None
    order_index: int
    is_done: bool
    is_critical: bool
    requires_photo: bool
    requires_comment: bool
    done_at: dt.datetime | None
    photo_file_id: str | None
    comment: str | None


@dataclass(frozen=True, slots=True)
class GroupView:
    """A group of items (TZ 5.4: a flat list of forty lines is unreadable on a phone)."""

    index: int
    name: str | None
    items: tuple[ItemView, ...]

    @property
    def done_count(self) -> int:
        return sum(1 for item in self.items if item.is_done)

    @property
    def total_count(self) -> int:
        return len(self.items)

    @property
    def is_complete(self) -> bool:
        return all(item.is_done for item in self.items)


@dataclass(frozen=True, slots=True)
class RunView:
    """Everything a screen or a notification needs about one run."""

    run_id: int
    template_id: int
    checklist_type: ChecklistType
    status: RunStatus
    shift_id: int | None
    user_id: int
    chat_id: int | None
    message_id: int | None
    sent_at: dt.datetime | None
    started_at: dt.datetime | None
    completed_at: dt.datetime | None
    skip_comment: str | None
    groups: tuple[GroupView, ...]

    @property
    def items(self) -> tuple[ItemView, ...]:
        return tuple(item for group in self.groups for item in group.items)

    @property
    def done_items(self) -> int:
        return sum(1 for item in self.items if item.is_done)

    @property
    def total_items(self) -> int:
        return len(self.items)

    @property
    def is_empty(self) -> bool:
        return not self.groups

    @property
    def is_finished(self) -> bool:
        return self.completed_at is not None

    @property
    def pending(self) -> tuple[ItemView, ...]:
        """Unticked items, critical ones first (TZ 5.4, TZ 6: critical items escalate)."""
        return tuple(sorted((item for item in self.items if not item.is_done), key=_pending_order))


def _pending_order(item: ItemView) -> tuple[bool, int, int, int]:
    return (not item.is_critical, item.group_index, item.order_index, item.item_id)


def _item_order(item: ChecklistItem) -> tuple[int, int, int]:
    return (item.group_index, item.order_index, item.id)


# --------------------------------------------------------------------------------------
# What the manager is told (TZ 6; the registry of notification types is task 19)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingItem:
    """An unticked line as it appears in a manager notification."""

    item_id: int
    text: str
    group_name: str | None
    is_critical: bool


@dataclass(frozen=True, slots=True)
class EmptyTemplateAlert:
    """TZ 8.1: an empty checklist is not sent; the manager hears about it instead."""

    checklist_type: ChecklistType
    template_id: int | None
    shift_id: int | None
    user_id: int


@dataclass(frozen=True, slots=True)
class PendingItemsAlert:
    """What was left unticked — for both the skipped and the overdue notification.

    ``items`` is already ordered with the critical ones first and every line carries
    ``is_critical``, so the renderer neither sorts nor decides what to mark.
    """

    run_id: int
    checklist_type: ChecklistType
    user_id: int
    completed_by: int | None
    skip_comment: str | None
    items: tuple[PendingItem, ...]

    @property
    def critical(self) -> tuple[PendingItem, ...]:
        return tuple(item for item in self.items if item.is_critical)

    @property
    def has_critical(self) -> bool:
        return any(item.is_critical for item in self.items)


class ChecklistNotifier(Protocol):
    """The notification side of the world, as this service needs it.

    Deliberately narrow: the service knows three things worth telling a manager and
    nothing about queues, chat ids or wording. The implementation is
    ``notification_service`` (plan, task 19), which owns the type registry, ``dedup_key``
    and the delivery worker — a handler never sends anything itself (plan, task 24).
    """

    async def checklist_template_empty(self, alert: EmptyTemplateAlert) -> None: ...

    async def checklist_finished_with_skips(self, alert: PendingItemsAlert) -> None: ...

    async def checklist_overdue(self, alert: PendingItemsAlert) -> None: ...


# --------------------------------------------------------------------------------------
# The one write TZ 5.4 needs and the frozen contract does not have yet
# --------------------------------------------------------------------------------------


class ReopenableRunRepository(ChecklistRunRepository, Protocol):
    """`ChecklistRunRepository` plus the write that "reopen" is made of.

    TZ 5.4, last block of rules: a checklist cannot be taken a second time, but a manager
    may reopen a run when the person got it wrong. Every other status write in the contract
    moves *forward* — `complete()` stamps `completed_at`, `mark_overdue()` refuses a
    finished run — so there is no way to spell that rule with the methods that exist.

    Schema note Q4 already settled the shape: no `reopened_at` / `reopened_by` columns, the
    fact is written to `audit_log` by the caller. The statement is therefore the mirror of
    `complete()` and conditional in the same way::

        UPDATE checklist_runs
           SET completed_at = NULL, status = 'in_progress'
         WHERE id = :run_id AND venue_id = :venue_id AND completed_at IS NOT NULL
        RETURNING id

    `None` means the run was unknown or was not finished in the first place — two managers
    pressing "reopen" at once reopen it once, exactly as two employees pressing "Done"
    complete it once.

    Declared here rather than in `src/db/repositories/protocols.py` because that file and
    the implementation belong to another wave; see the report accompanying this change.
    """

    async def reopen(self, run_id: int) -> ChecklistRun | None: ...


# --------------------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------------------


class RunCreation(enum.StrEnum):
    """Why `create_run` did or did not produce a run."""

    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    EMPTY_TEMPLATE = "empty_template"
    NO_ACTIVE_TEMPLATE = "no_active_template"


class ToggleOutcome(enum.StrEnum):
    TOGGLED = "toggled"
    #: The row was already in the target state — a concurrent or repeated tap.
    UNCHANGED = "unchanged"
    #: A tap inside a message that belongs to a run already finished: data stays put.
    RUN_FINISHED = "run_finished"
    #: Unknown run or an item that is not part of it (a forged callback, TZ 9).
    NOT_FOUND = "not_found"


class ReopenOutcome(enum.StrEnum):
    REOPENED = "reopened"
    #: The run was not finished — there is nothing to reopen and nothing was changed.
    NOT_FINISHED = "not_finished"
    NOT_FOUND = "not_found"


class CompletionOutcome(enum.StrEnum):
    COMPLETED = "completed"
    #: Something is unticked and no reason was given yet (TZ 5.4 asks for a short comment).
    NEEDS_REASON = "needs_reason"
    SKIPPED = "skipped"
    ALREADY_FINISHED = "already_finished"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class RunCreated:
    outcome: RunCreation
    run: ChecklistRun | None
    total_items: int
    alert: EmptyTemplateAlert | None = None


@dataclass(frozen=True, slots=True)
class ToggleResult:
    outcome: ToggleOutcome
    item_id: int
    is_done: bool
    view: RunView | None

    @property
    def changed(self) -> bool:
        return self.outcome is ToggleOutcome.TOGGLED


@dataclass(frozen=True, slots=True)
class ReopenResult:
    outcome: ReopenOutcome
    run: ChecklistRun | None
    #: The screen to redraw after a reopen; ``None`` only when there is no run to draw.
    view: RunView | None = None


@dataclass(frozen=True, slots=True)
class CompletionResult:
    outcome: CompletionOutcome
    run: ChecklistRun | None
    pending: tuple[ItemView, ...]
    alert: PendingItemsAlert | None = None

    @property
    def notified(self) -> bool:
        return self.alert is not None


def overdue_deadline(shift_start: dt.datetime, overdue_minutes: int) -> dt.datetime:
    """TZ 5.4 and decision B2, literally: shift start plus ``checklist_overdue_minutes``.

    ``shift_start`` is an absolute instant (UTC), so adding a fixed number of minutes is
    unambiguous and needs no timezone of its own — the venue-local ``TIME`` has already
    been resolved into an instant by ``src/services/timezones.py`` (decision D12).

    B2 also records what this formula does *not* survive: for a closing checklist, which
    leaves half an hour before the *end* of the shift, "shift start + N" marks the run
    overdue hours before it is even sent. Stage 0 has the opening checklist only; stage 1
    moves the anchor into the checklist type.

    A naive ``shift_start`` is refused rather than assumed to be UTC: guessing here is how
    a checklist ends up marked overdue three hours early in a venue east of Moscow (D12).
    """
    return ensure_utc(shift_start) + dt.timedelta(minutes=overdue_minutes)


class ChecklistService:
    """Business logic of TZ 5.4. Holds no Telegram types and no session of its own."""

    def __init__(
        self,
        *,
        templates: ChecklistTemplateRepository,
        items: ChecklistItemRepository,
        runs: ReopenableRunRepository,
        run_items: ChecklistRunItemRepository,
        notifier: ChecklistNotifier,
    ) -> None:
        self._templates = templates
        self._items = items
        self._runs = runs
        self._run_items = run_items
        self._notifier = notifier

    # ---------------------------------------------------------------------------------
    # Creation
    # ---------------------------------------------------------------------------------

    async def create_run(
        self,
        *,
        shift_id: int | None,
        user_id: int,
        checklist_type: ChecklistType,
        moment: dt.datetime,
    ) -> RunCreated:
        """Create the run for a shift, or explain why no checklist is going out.

        TZ 8.1: a template without items produces no run and no message to the employee —
        the manager is told the template is empty. TZ 5.4: one checklist is one run, so an
        already existing run for this shift and type is returned instead of a second one.
        """
        sent_at = ensure_utc(moment)
        if shift_id is not None:
            existing = await self._runs.get_by_shift(shift_id, checklist_type)
            if existing is not None:
                return RunCreated(
                    outcome=RunCreation.ALREADY_EXISTS,
                    run=existing,
                    total_items=existing.total_items,
                )

        template = await self._templates.get_active(checklist_type)
        if template is None:
            alert = EmptyTemplateAlert(
                checklist_type=checklist_type,
                template_id=None,
                shift_id=shift_id,
                user_id=user_id,
            )
            await self._notifier.checklist_template_empty(alert)
            return RunCreated(
                outcome=RunCreation.NO_ACTIVE_TEMPLATE,
                run=None,
                total_items=0,
                alert=alert,
            )

        items = sorted(await self._items.list_for_template(template.id), key=_item_order)
        if not items:
            alert = EmptyTemplateAlert(
                checklist_type=checklist_type,
                template_id=template.id,
                shift_id=shift_id,
                user_id=user_id,
            )
            await self._notifier.checklist_template_empty(alert)
            return RunCreated(
                outcome=RunCreation.EMPTY_TEMPLATE,
                run=None,
                total_items=0,
                alert=alert,
            )

        run = await self._runs.create(
            shift_id=shift_id,
            user_id=user_id,
            template_id=template.id,
            checklist_type=checklist_type,
            total_items=len(items),
            sent_at=sent_at,
        )
        await self._run_items.create_for_run(run.id, [item.id for item in items])
        return RunCreated(outcome=RunCreation.CREATED, run=run, total_items=len(items))

    async def remember_message(self, *, run_id: int, chat_id: int, message_id: int) -> None:
        """Store the message a run lives in, so it is edited rather than re-sent (TZ 8.2)."""
        await self._runs.set_message(run_id, chat_id=chat_id, message_id=message_id)

    # ---------------------------------------------------------------------------------
    # Rendering
    # ---------------------------------------------------------------------------------

    async def view(self, run_id: int) -> RunView | None:
        """Grouped render model of a run, or ``None`` for an unknown or foreign run."""
        run = await self._runs.get(run_id)
        if run is None:
            return None
        return await self._build_view(run)

    async def run_for_shift(
        self,
        *,
        shift_id: int,
        checklist_type: ChecklistType,
    ) -> RunView | None:
        """The run already sent for this shift, or ``None`` if none was.

        The shift screen of TZ 5.3 offers a way back into the checklist — the employee
        swiped the message away, or simply scrolled past it — and the button carries a
        ``run_id`` it has to get from somewhere. Without this the screen either draws no
        button at all (which is what it did) or the handler reaches for a repository, which
        is the one thing `tests/bot/test_handler_boundary.py` exists to prevent.

        **This never creates anything.** Creation is the worker's, at the moment TZ 5.4
        names (`opening_checklist_lead_minutes` before the shift), and opening the shift
        screen an hour earlier must not bring the checklist forward — the whole point of
        principle 1.4#2 is that the bot decides when, not the employee.
        """
        run = await self._runs.get_by_shift(shift_id, checklist_type)
        if run is None:
            return None
        return await self._build_view(run)

    async def _build_view(self, run: ChecklistRun) -> RunView:
        """Wording from ``run.template_id``, composition from the run's own rows.

        Both halves matter. Reading the template through the run rather than through
        ``get_active()`` is what keeps an edit from reaching an open checklist; keeping
        only the items the run actually has rows for is what keeps it from reaching one
        even if that template were edited in place before it was ever referenced (B3).
        """
        template_items = await self._items.list_for_template(run.template_id)
        state = {row.item_id: row for row in await self._run_items.list_for_run(run.id)}

        views: list[ItemView] = []
        for item in sorted(template_items, key=_item_order):
            row = state.get(item.id)
            if row is None:
                continue
            views.append(_item_view(item, row))

        return RunView(
            run_id=run.id,
            template_id=run.template_id,
            checklist_type=run.type,
            status=run.status,
            shift_id=run.shift_id,
            user_id=run.user_id,
            chat_id=run.chat_id,
            message_id=run.message_id,
            sent_at=run.sent_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            skip_comment=run.skip_comment,
            groups=_group(views),
        )

    # ---------------------------------------------------------------------------------
    # Toggle
    # ---------------------------------------------------------------------------------

    async def toggle(
        self,
        *,
        run_id: int,
        item_id: int,
        moment: dt.datetime,
        photo_file_id: str | None = None,
        comment: str | None = None,
    ) -> ToggleResult:
        """Flip one item. Safe to call twice with the same arguments, concurrently.

        The service never computes the new counter: it asks the repository to flip the row
        with a single conditional update and then to recompute ``done_items`` from
        ``checklist_run_items``. A second tap that lost the race changes nothing and the
        count still matches the rows.
        """
        moment = ensure_utc(moment)
        run = await self._runs.get(run_id)
        if run is None:
            return ToggleResult(
                outcome=ToggleOutcome.NOT_FOUND, item_id=item_id, is_done=False, view=None
            )

        if run.completed_at is not None:
            # A tap inside a stale message (plan, task 24): re-render, change nothing.
            return ToggleResult(
                outcome=ToggleOutcome.RUN_FINISHED,
                item_id=item_id,
                is_done=_current_state(await self._run_items.list_for_run(run.id), item_id),
                view=await self._build_view(run),
            )

        rows = await self._run_items.list_for_run(run.id)
        current = next((row for row in rows if row.item_id == item_id), None)
        if current is None:
            # The id is not part of this run: a forged callback, or an item added to the
            # template after the run was created (TZ 9).
            return ToggleResult(
                outcome=ToggleOutcome.NOT_FOUND, item_id=item_id, is_done=False, view=None
            )

        target = not current.is_done
        changed = await self._run_items.set_done(
            run_id=run_id,
            item_id=item_id,
            is_done=target,
            moment=moment,
            photo_file_id=photo_file_id,
            comment=comment,
        )

        if changed is not None and run.started_at is None:
            # TZ 4.3 / schema notes: `started_at` is the first tap, `sent_at` is delivery.
            # The repository guards it with `WHERE started_at IS NULL`, so a race is fine.
            await self._runs.mark_started(run_id, moment)

        refreshed = await self._runs.refresh_counters(run_id)
        view = await self._build_view(refreshed if refreshed is not None else run)
        outcome = ToggleOutcome.TOGGLED if changed is not None else ToggleOutcome.UNCHANGED
        return ToggleResult(
            outcome=outcome,
            item_id=item_id,
            is_done=changed.is_done if changed is not None else target,
            view=view,
        )

    # ---------------------------------------------------------------------------------
    # Completion
    # ---------------------------------------------------------------------------------

    async def complete(
        self,
        *,
        run_id: int,
        completed_by: int,
        moment: dt.datetime,
        skip_comment: str | None = None,
    ) -> CompletionResult:
        """Press "Done" (TZ 5.4).

        Everything ticked -> ``completed``, the manager hears nothing. Something unticked
        and no reason yet -> ``NEEDS_REASON`` and the list of what is missing, so the bot
        can ask "Send as is?" and then for the short comment. With a reason -> ``skipped``
        plus one manager notification, critical items first.
        """
        moment = ensure_utc(moment)
        run = await self._runs.get(run_id)
        if run is None:
            return CompletionResult(outcome=CompletionOutcome.NOT_FOUND, run=None, pending=())
        if run.completed_at is not None:
            return CompletionResult(outcome=CompletionOutcome.ALREADY_FINISHED, run=run, pending=())

        view = await self._build_view(run)
        pending = view.pending
        reason = _clean(skip_comment)
        if pending and reason is None:
            return CompletionResult(
                outcome=CompletionOutcome.NEEDS_REASON, run=run, pending=pending
            )

        status = RunStatus.SKIPPED if pending else RunStatus.COMPLETED
        finished = await self._runs.complete(
            run_id,
            moment=moment,
            status=status,
            completed_by=completed_by,
            skip_comment=reason if pending else None,
        )
        if finished is None:
            # Lost the race with another "Done": one completion, one notification.
            return CompletionResult(
                outcome=CompletionOutcome.ALREADY_FINISHED, run=run, pending=pending
            )

        if not pending:
            return CompletionResult(outcome=CompletionOutcome.COMPLETED, run=finished, pending=())

        alert = _pending_alert(view, completed_by=completed_by, skip_comment=reason)
        await self._notifier.checklist_finished_with_skips(alert)
        return CompletionResult(
            outcome=CompletionOutcome.SKIPPED, run=finished, pending=pending, alert=alert
        )

    # ---------------------------------------------------------------------------------
    # Overdue
    # ---------------------------------------------------------------------------------

    async def report_overdue(
        self,
        *,
        run_id: int,
        shift_start: dt.datetime,
        overdue_minutes: int,
        moment: dt.datetime,
    ) -> PendingItemsAlert | None:
        """Mark an unfinished checklist overdue and tell the manager once (TZ 5.4, 6).

        The scheduler asks this on every tick, so the deadline cannot be what decides to
        send: three ticks after it would be three identical messages to the same manager.
        What decides is the *transition*. The status is written first — TZ 5.4 wants
        ``overdue`` recorded anyway — and the repository moves it with
        ``WHERE completed_at IS NULL AND status != 'overdue'``, so exactly one caller ever
        gets a row back and only that caller notifies.

        Returns ``None`` — and notifies nobody — when the run is unknown, already finished,
        already marked overdue, or still inside ``shift start + checklist_overdue_minutes``
        (decision B2).
        """
        moment = ensure_utc(moment)
        run = await self._runs.get(run_id)
        if run is None or run.completed_at is not None:
            return None
        if moment < overdue_deadline(shift_start, overdue_minutes):
            return None

        marked = await self._runs.mark_overdue(run_id, moment)
        if marked is None:
            # Another tick was here first, or the run was finished between the read and
            # the write. Either way the manager has been told, or has nothing to be told.
            return None

        view = await self._build_view(marked)
        alert = _pending_alert(view, completed_by=None, skip_comment=None)
        await self._notifier.checklist_overdue(alert)
        return alert

    # ---------------------------------------------------------------------------------
    # Reopening
    # ---------------------------------------------------------------------------------

    async def reopen(self, actor: AccessContext, run_id: int) -> ReopenResult:
        """TZ 5.4: a run cannot be taken twice, but a manager may reopen one.

        The right is checked on the server, on the object, and in that order (TZ 2, TZ 9):
        `staff` is refused whatever the callback says, and a run of another venue is
        refused even to a manager. Nothing else changes — `started_at` stays where it was,
        the ticks stay ticked, and the person only gets the screen back.

        Schema note Q4: reopening leaves no columns of its own, the fact belongs in
        `audit_log` and is written by the caller — this service holds no audit repository.
        """
        require_manager(actor)
        run = await self._runs.get(run_id)
        if run is None:
            return ReopenResult(outcome=ReopenOutcome.NOT_FOUND, run=None, view=None)
        require_venue(actor, run.venue_id)

        reopened = await self._runs.reopen(run_id)
        if reopened is None:
            # Nothing to reopen: the run was never finished, or another manager was first
            # and it is already open again.
            return ReopenResult(
                outcome=ReopenOutcome.NOT_FINISHED,
                run=run,
                view=await self._build_view(run),
            )
        return ReopenResult(
            outcome=ReopenOutcome.REOPENED,
            run=reopened,
            view=await self._build_view(reopened),
        )


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Blank input is absent input: a reason of spaces is not a reason."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _current_state(rows: Sequence[ChecklistRunItem], item_id: int) -> bool:
    return any(row.item_id == item_id and row.is_done for row in rows)


def _item_view(item: ChecklistItem, row: ChecklistRunItem) -> ItemView:
    return ItemView(
        item_id=item.id,
        text=item.text,
        group_index=item.group_index,
        group_name=item.group_name,
        order_index=item.order_index,
        is_done=row.is_done,
        is_critical=item.is_critical,
        requires_photo=item.requires_photo,
        requires_comment=item.requires_comment,
        done_at=row.done_at,
        photo_file_id=row.photo_file_id,
        comment=row.comment,
    )


def _group(views: Iterable[ItemView]) -> tuple[GroupView, ...]:
    """Split an already ordered sequence into groups, keeping that order.

    A group does not exist without items (decision D2), so grouping is a fold over the
    items and never a lookup in a table of groups.
    """
    groups: list[GroupView] = []
    bucket: list[ItemView] = []
    index: int | None = None
    name: str | None = None

    for view in views:
        if index is not None and view.group_index != index:
            groups.append(GroupView(index=index, name=name, items=tuple(bucket)))
            bucket = []
        index, name = view.group_index, view.group_name
        bucket.append(view)

    if index is not None:
        groups.append(GroupView(index=index, name=name, items=tuple(bucket)))
    return tuple(groups)


def _pending_alert(
    view: RunView,
    *,
    completed_by: int | None,
    skip_comment: str | None,
) -> PendingItemsAlert:
    return PendingItemsAlert(
        run_id=view.run_id,
        checklist_type=view.checklist_type,
        user_id=view.user_id,
        completed_by=completed_by,
        skip_comment=skip_comment,
        items=tuple(
            PendingItem(
                item_id=item.item_id,
                text=item.text,
                group_name=item.group_name,
                is_critical=item.is_critical,
            )
            for item in view.pending
        ),
    )


__all__ = [
    "ChecklistNotifier",
    "ChecklistService",
    "CompletionOutcome",
    "CompletionResult",
    "EmptyTemplateAlert",
    "GroupView",
    "ItemView",
    "PendingItem",
    "PendingItemsAlert",
    "ReopenOutcome",
    "ReopenResult",
    "ReopenableRunRepository",
    "RunCreated",
    "RunCreation",
    "RunView",
    "ToggleOutcome",
    "ToggleResult",
    "overdue_deadline",
]
