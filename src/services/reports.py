"""What happened on one day, as data (TZ 5.9; part III of the stage 1 spec).

The manager's question is short — how did the shift go — and the answer is three things:
who worked, what the checklists came to, and **what was left unticked**. The last one is
the reason the report exists at all; the first two are context for it.

Three properties of this module are decisions, not accidents.

**It returns a snapshot of scalars, never ORM rows.** The workbook is built in a worker
thread (openpyxl is synchronous and CPU-bound, and the bot is one process), and a lazy
attribute touched from another thread reaches for an async session that does not belong to
it. Everything below is frozen dataclasses of plain values, read while the session is open.

**The wording of an item comes from the run, not from the template that is active now.**
`ChecklistService` builds its view from `checklist_runs.template_id`, a version frozen at
the moment the checklist was sent (decision B3), so a report of last month does not quietly
change when somebody edits a checklist today. This module reuses that view for exactly that
reason rather than reading `checklist_items` itself.

**A dismissed employee stays in the report.** TZ 5.1 keeps their `venue_members` row so the
history survives, and the history is what this is. Names are therefore read from `users` by
id — the global table — and never from the roster, which lists the active only. The venue
scope is not lost by that: the shifts and runs were found through venue-scoped
repositories, and a name is all `users` is asked for.

Not here: any wording. Column headings live in `src/bot/texts/reports.py`, and the workbook
itself in `src/exporters/`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from src.db.models import ChecklistType, RunStatus, Shift, User
from src.services.checklists import RunView

#: The checklists a shift can have, in the order they happen. `stock` is not here: TZ 5.4
#: ties it to the venue rather than to a shift, and `checklist_runs.shift_id` is null for
#: one, so a report built around shifts has nowhere to hang it (stage 1 owns that screen).
REPORTED_CHECKLISTS: tuple[ChecklistType, ...] = (ChecklistType.OPENING, ChecklistType.CLOSING)


@dataclass(frozen=True, slots=True)
class PendingLine:
    """One line nobody ticked — the half of the report the manager opens it for."""

    checklist: ChecklistType
    full_name: str
    text: str
    group_name: str | None
    is_critical: bool


@dataclass(frozen=True, slots=True)
class RunLine:
    """One checklist of one shift, as it ended."""

    checklist: ChecklistType
    full_name: str
    status: RunStatus
    done: int
    total: int
    completed_at: dt.datetime | None
    skip_comment: str | None


@dataclass(frozen=True, slots=True)
class ShiftLine:
    """One person's shift on the day."""

    full_name: str
    start: dt.time
    end: dt.time
    is_opener: bool
    is_closer: bool


@dataclass(frozen=True, slots=True)
class ShiftReport:
    """One venue, one day, everything the manager asked about."""

    venue: str
    day: dt.date
    shifts: tuple[ShiftLine, ...]
    runs: tuple[RunLine, ...]
    pending: tuple[PendingLine, ...]

    @property
    def is_empty(self) -> bool:
        """Nobody worked that day. A normal answer, not a failure (TZ 8.1)."""
        return not self.shifts


class ShiftsOfDay(Protocol):
    """The one question this module asks of `shifts`, through the venue's repository."""

    async def list_for_date(self, shift_date: dt.date, /) -> Sequence[Shift]: ...


class RunsOfShift(Protocol):
    """`ChecklistService.run_for_shift` — a read, and never a create."""

    async def run_for_shift(
        self,
        *,
        shift_id: int,
        checklist_type: ChecklistType,
    ) -> RunView | None: ...


class People(Protocol):
    """Names by id, including people who no longer work here (TZ 5.1)."""

    async def get(self, user_id: int, /) -> User | None: ...


class ReportService:
    """Builds :class:`ShiftReport`. Reads only; writes nothing anywhere."""

    def __init__(
        self,
        *,
        venue: str,
        shifts: ShiftsOfDay,
        checklists: RunsOfShift,
        users: People,
    ) -> None:
        self._venue = venue
        self._shifts = shifts
        self._checklists = checklists
        self._users = users

    async def shift_report(self, day: dt.date) -> ShiftReport:
        """Everything that happened on one venue-local date (TZ 5.9)."""
        names: dict[int, str] = {}
        shifts: list[ShiftLine] = []
        runs: list[RunLine] = []
        pending: list[PendingLine] = []

        for shift in await self._shifts.list_for_date(day):
            shifts.append(
                ShiftLine(
                    full_name=await self._name_of(shift.user_id, names),
                    start=shift.start_time,
                    end=shift.end_time,
                    is_opener=shift.is_opener,
                    is_closer=shift.is_closer,
                )
            )
            for checklist in REPORTED_CHECKLISTS:
                view = await self._checklists.run_for_shift(
                    shift_id=shift.id,
                    checklist_type=checklist,
                )
                if view is None:
                    continue
                who = await self._name_of(view.user_id, names)
                runs.append(_run_line(view, full_name=who))
                pending.extend(_pending_lines(view, full_name=who))

        return ShiftReport(
            venue=self._venue,
            day=day,
            shifts=tuple(shifts),
            runs=tuple(runs),
            pending=tuple(pending),
        )

    async def _name_of(self, user_id: int, cache: dict[int, str]) -> str:
        """One `users` read per person and not per line: a shift has two checklists."""
        if user_id not in cache:
            person = await self._users.get(user_id)
            cache[user_id] = "" if person is None else person.full_name
        return cache[user_id]


def _run_line(view: RunView, *, full_name: str) -> RunLine:
    total = sum(len(group.items) for group in view.groups)
    done = sum(1 for group in view.groups for item in group.items if item.is_done)
    return RunLine(
        checklist=view.checklist_type,
        full_name=full_name,
        status=view.status,
        done=done,
        total=total,
        completed_at=view.completed_at,
        skip_comment=view.skip_comment,
    )


def _pending_lines(view: RunView, *, full_name: str) -> tuple[PendingLine, ...]:
    """The unticked lines, critical ones first — the order TZ 6 escalates in."""
    lines = [
        PendingLine(
            checklist=view.checklist_type,
            full_name=full_name,
            text=item.text,
            group_name=group.name,
            is_critical=item.is_critical,
        )
        for group in view.groups
        for item in group.items
        if not item.is_done
    ]
    lines.sort(key=lambda line: not line.is_critical)
    return tuple(lines)


__all__ = [
    "REPORTED_CHECKLISTS",
    "PendingLine",
    "People",
    "ReportService",
    "RunLine",
    "RunsOfShift",
    "ShiftLine",
    "ShiftReport",
    "ShiftsOfDay",
]
