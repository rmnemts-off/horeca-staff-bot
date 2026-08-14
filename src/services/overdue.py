"""The tick that notices a checklist nobody finished (TZ 5.4, section 6; plan task 31).

`ChecklistService.report_overdue` was written for a caller that did not exist. It is
careful about being asked repeatedly — the status transition, not the deadline, is what
decides whether the manager hears anything, so three ticks past the deadline still produce
one message — and that care is only worth something if somebody ticks. Nobody did, so the
«checklist is overdue» row of TZ section 6 was declared, rendered, tested and never sent.

This module is the caller. It belongs to the services and not to `src/scheduler/` because
what it decides is business: which runs are candidates, where their deadline comes from,
and what happens when a run has no shift behind it. The worker only calls it, once per
venue per pass, next to the delivery it already does.

**Why the deadline is computed here rather than stored.** A run keeps no deadline of its
own, and that is deliberate (decision B2): the deadline is the start of the shift plus
`checklist_overdue_minutes`, both halves of which the manager may change after the run was
created. Reading them at the moment of the check means an edit to the venue's settings
takes effect on the checklist that is open right now, which is what a manager who has just
widened the window expects.

**A run with no shift is skipped, silently and on purpose.** `checklist_runs.shift_id` is
nullable because a `stock` checklist has no shift (schema notes); without a shift there is
no start, so "start plus N minutes" has nothing to anchor to. Inventing an anchor — the
moment the run was sent, say — would be a business rule nobody asked for, and stage 0 has
no such runs to begin with.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from zoneinfo import ZoneInfo

from src.db.models import ChecklistRun, RunStatus, Shift, VenueSettings
from src.services.checklists import PendingItemsAlert
from src.services.timezones import ensure_utc, shift_window, venue_timezone

#: The statuses a run can be in and still be late. `COMPLETED` is done, `OVERDUE` has
#: already been reported once, `SKIPPED` was closed with a reason (TZ 5.4) — none of the
#: three is waiting for anybody.
UNFINISHED: frozenset[RunStatus] = frozenset({RunStatus.SENT, RunStatus.IN_PROGRESS})


class OpenRuns(Protocol):
    """The one question this module asks of `checklist_runs`.

    Narrower than `ChecklistRunRepository` for the reason `RowLookup` in the resolver is:
    what the sweep needs is a listing by status, and asking for the whole repository would
    let a later edit reach for `complete()` from a place that has no actor to attribute it
    to. The venue predicate is the repository's either way — this protocol cannot loosen it.
    """

    async def list_by_status(self, status: RunStatus, /) -> Sequence[ChecklistRun]: ...


class ShiftLookup(Protocol):
    """One shift by id, through the venue's own repository."""

    async def get(self, shift_id: int, /) -> Shift | None: ...


class SettingsLookup(Protocol):
    """The venue's settings row, or `None` before the wizard of task 26 has run."""

    async def get(self) -> VenueSettings | None: ...


class OverdueReporter(Protocol):
    """The half of `ChecklistService` this sweep uses, and nothing more."""

    async def report_overdue(
        self,
        *,
        run_id: int,
        shift_start: dt.datetime,
        overdue_minutes: int,
        moment: dt.datetime,
    ) -> PendingItemsAlert | None: ...


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What one pass over one venue found. Read in the worker's log."""

    #: Runs that were still unfinished and had a shift to measure against.
    examined: int = 0
    #: Runs that crossed the deadline on this pass — one manager notification each.
    reported: int = 0
    #: Runs with no shift behind them; see the module docstring.
    skipped: int = 0


class OverdueSweeper(Protocol):
    """What the worker needs of this module — one call per venue per pass.

    A protocol and not the class, for the reason `RowLookup` in the resolver is one: the
    worker's collaborator should be describable by a fake that records the call, and a
    concrete class in the signature makes the fake either impossible or a subclass carrying
    a repository it never uses.
    """

    async def run(self, *, now: dt.datetime) -> SweepReport: ...


class OverdueService:
    """One venue's unfinished checklists, measured against their deadlines."""

    def __init__(
        self,
        *,
        timezone: str | ZoneInfo,
        runs: OpenRuns,
        shifts: ShiftLookup,
        settings: SettingsLookup,
        checklists: OverdueReporter,
    ) -> None:
        self._tz = timezone if isinstance(timezone, ZoneInfo) else venue_timezone(timezone)
        self._runs = runs
        self._shifts = shifts
        self._settings = settings
        self._checklists = checklists

    async def run(self, *, now: dt.datetime) -> SweepReport:
        """Report every checklist that has just gone past its deadline.

        "Just" is the point: :meth:`ChecklistService.report_overdue` answers `None` for a
        run that was already marked, so a run stays reported once however often this is
        called (TZ 6 — the manager is told, not reminded).

        A venue with no settings row has no deadline to measure against and is left alone;
        it cannot have runs either, because it never came through the wizard (task 26).
        """
        moment = ensure_utc(now)
        settings = await self._settings.get()
        if settings is None:
            return SweepReport()

        examined = reported = skipped = 0
        for run in await self._unfinished():
            shift = await self._shift_of(run)
            if shift is None:
                skipped += 1
                continue
            start, _ = shift_window(shift.shift_date, shift.start_time, shift.end_time, self._tz)
            examined += 1
            alert = await self._checklists.report_overdue(
                run_id=run.id,
                shift_start=start,
                overdue_minutes=settings.checklist_overdue_minutes,
                moment=moment,
            )
            if alert is not None:
                reported += 1
        return SweepReport(examined=examined, reported=reported, skipped=skipped)

    async def _unfinished(self) -> Sequence[ChecklistRun]:
        """Runs that are still waiting for somebody, in both shapes of waiting.

        Two queries and not one because the repository is asked by status and the schema
        has two of them for "open": a checklist nobody has touched is `sent`, and one with
        a tick in it is `in_progress` (TZ 4.3). Missing the second would mean the checklist
        a bartender started and abandoned is the one nobody is ever told about.
        """
        found: list[ChecklistRun] = []
        for status in sorted(UNFINISHED):
            found.extend(await self._runs.list_by_status(status))
        return found

    async def _shift_of(self, run: ChecklistRun) -> Shift | None:
        if run.shift_id is None:
            return None
        return await self._shifts.get(run.shift_id)


__all__ = [
    "UNFINISHED",
    "OpenRuns",
    "OverdueReporter",
    "OverdueService",
    "OverdueSweeper",
    "SettingsLookup",
    "ShiftLookup",
    "SweepReport",
]
