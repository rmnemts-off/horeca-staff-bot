"""The tick that reports a checklist nobody finished (TZ section 6, plan task 31).

`ChecklistService.report_overdue` was already written and already tested; what was missing
was a caller, so the notification type existed and never fired. These tests are about the
caller: which runs it picks up, where it gets the deadline, and what it does with a run
that has no shift to measure against.

The transition itself — "told once, not once a minute" — belongs to `report_overdue` and is
tested in `test_checklists.py`. Here it is exercised only through repeated passes, because
that is the property the *sweep* has to preserve: the worker calls it every minute.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from src.db.models import ChecklistRun, ChecklistType, RunStatus, Shift, VenueSettings
from src.services.checklists import PendingItemsAlert
from src.services.overdue import UNFINISHED, OverdueService

VENUE_ID = 1
OTHER_VENUE_ID = 2
TIMEZONE = "Europe/Moscow"
DAY = dt.date(2026, 8, 13)
#: 09:00 Moscow is 06:00 UTC; the venue's window opens then.
START = dt.time(9, 0)
END = dt.time(21, 0)
OVERDUE_MINUTES = 30


def utc(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, 13, hour, minute, tzinfo=dt.UTC)


def make_shift(shift_id: int, *, venue_id: int = VENUE_ID) -> Shift:
    return Shift(
        id=shift_id,
        venue_id=venue_id,
        user_id=7,
        shift_date=DAY,
        start_time=START,
        end_time=END,
    )


def make_run(
    run_id: int,
    *,
    shift_id: int | None,
    status: RunStatus = RunStatus.SENT,
    venue_id: int = VENUE_ID,
) -> ChecklistRun:
    return ChecklistRun(
        id=run_id,
        venue_id=venue_id,
        shift_id=shift_id,
        user_id=7,
        template_id=1,
        type=ChecklistType.OPENING,
        status=status,
    )


class FakeRuns:
    """`ChecklistRunRepository.list_by_status`, scoped the way the real one is.

    Rows of every venue live in one list, exactly as they do in one table, and the scope is
    the predicate — so a run of the venue next door is invisible here rather than absent
    (CLAUDE.md).
    """

    def __init__(self, venue_id: int, store: list[ChecklistRun]) -> None:
        self.venue_id = venue_id
        self.rows = store

    async def list_by_status(self, status: RunStatus) -> Sequence[ChecklistRun]:
        return [row for row in self.rows if row.venue_id == self.venue_id and row.status is status]


class FakeShifts:
    """`ShiftRepository.get`, with the same venue predicate."""

    def __init__(self, venue_id: int, store: list[Shift]) -> None:
        self.venue_id = venue_id
        self.rows = store

    async def get(self, shift_id: int) -> Shift | None:
        return next(
            (row for row in self.rows if row.id == shift_id and row.venue_id == self.venue_id),
            None,
        )


class FakeSettings:
    """`VenueSettingsRepository.get` — one row per venue, or none before the wizard ran."""

    def __init__(self, overdue_minutes: int | None) -> None:
        self._row = (
            None
            if overdue_minutes is None
            else VenueSettings(
                venue_id=VENUE_ID,
                default_shift_start=START,
                default_shift_end=END,
                checklist_overdue_minutes=overdue_minutes,
            )
        )

    async def get(self) -> VenueSettings | None:
        return self._row


@dataclass
class FakeChecklists:
    """`report_overdue` with its own contract: the transition decides, not the deadline.

    Reproduced and not stubbed, because the sweep is called every minute and the property
    the pair has to keep is "one manager notification per run". A fake that reported every
    time would make the sweep look correct while the manager got a message a minute.
    """

    deadline_minutes: int = OVERDUE_MINUTES
    reported: list[int] = field(default_factory=list)
    calls: list[tuple[int, dt.datetime, int]] = field(default_factory=list)

    async def report_overdue(
        self,
        *,
        run_id: int,
        shift_start: dt.datetime,
        overdue_minutes: int,
        moment: dt.datetime,
    ) -> PendingItemsAlert | None:
        self.calls.append((run_id, shift_start, overdue_minutes))
        if moment < shift_start + dt.timedelta(minutes=overdue_minutes):
            return None
        if run_id in self.reported:
            return None
        self.reported.append(run_id)
        return None if run_id < 0 else _alert(run_id)


def _alert(run_id: int) -> PendingItemsAlert:
    return PendingItemsAlert(
        run_id=run_id,
        checklist_type=ChecklistType.OPENING,
        user_id=7,
        completed_by=None,
        skip_comment=None,
        items=(),
    )


@dataclass
class Stand:
    runs: list[ChecklistRun] = field(default_factory=list)
    shifts: list[Shift] = field(default_factory=list)
    checklists: FakeChecklists = field(default_factory=FakeChecklists)
    overdue_minutes: int | None = OVERDUE_MINUTES

    def service(self, venue_id: int = VENUE_ID) -> OverdueService:
        return OverdueService(
            timezone=TIMEZONE,
            runs=FakeRuns(venue_id, self.runs),
            shifts=FakeShifts(venue_id, self.shifts),
            settings=FakeSettings(self.overdue_minutes),
            checklists=self.checklists,
        )


def test_the_unfinished_statuses_are_the_two_ways_a_run_is_still_waiting() -> None:
    """TZ 4.3: untouched is `sent`, started is `in_progress`; the other three are closed.

    Spelled out because leaving `in_progress` out is the mistake that would be invisible —
    the checklist a bartender starts and abandons is exactly the one worth reporting.
    """
    assert {RunStatus.SENT, RunStatus.IN_PROGRESS} == UNFINISHED


async def test_a_checklist_past_its_deadline_is_reported_once_however_often_we_look() -> None:
    """The worker calls this every minute; the manager hears about it once (TZ 6)."""
    stand = Stand(runs=[make_run(10, shift_id=1)], shifts=[make_shift(1)])
    service = stand.service()

    # 06:00 UTC is the start; the deadline is 06:30.
    quiet = await service.run(now=utc(6, 20))
    late = await service.run(now=utc(6, 31))
    again = await service.run(now=utc(7, 0))

    assert (quiet.reported, late.reported, again.reported) == (0, 1, 0)
    assert stand.checklists.reported == [10]
    assert [call[0] for call in stand.checklists.calls] == [10, 10, 10], (
        "the sweep asks every pass — deciding is the service's, not the sweep's"
    )


async def test_a_started_checklist_is_swept_too() -> None:
    """`in_progress` means somebody ticked a line and stopped; nobody is told otherwise."""
    stand = Stand(
        runs=[make_run(11, shift_id=1, status=RunStatus.IN_PROGRESS)],
        shifts=[make_shift(1)],
    )

    report = await stand.service().run(now=utc(6, 31))

    assert report.reported == 1


async def test_a_finished_checklist_is_not_looked_at() -> None:
    stand = Stand(
        runs=[
            make_run(12, shift_id=1, status=RunStatus.COMPLETED),
            make_run(13, shift_id=1, status=RunStatus.OVERDUE),
            make_run(14, shift_id=1, status=RunStatus.SKIPPED),
        ],
        shifts=[make_shift(1)],
    )

    report = await stand.service().run(now=utc(20, 0))

    assert (report.examined, report.reported) == (0, 0)
    assert stand.checklists.calls == []


async def test_a_run_without_a_shift_is_skipped_and_counted() -> None:
    """A `stock` checklist has no shift, so "start plus N minutes" has nothing to anchor to.

    Skipping is the decision; inventing an anchor — the moment the run was sent, say —
    would be a business rule the TZ never wrote.
    """
    stand = Stand(runs=[make_run(15, shift_id=None)])

    report = await stand.service().run(now=utc(20, 0))

    assert (report.examined, report.reported, report.skipped) == (0, 0, 1)
    assert stand.checklists.calls == []


async def test_a_venue_that_never_ran_the_wizard_is_left_alone() -> None:
    """No `venue_settings` row means no deadline to measure against (plan task 26)."""
    stand = Stand(runs=[make_run(16, shift_id=1)], shifts=[make_shift(1)], overdue_minutes=None)

    report = await stand.service().run(now=utc(20, 0))

    assert (report.examined, report.reported, report.skipped) == (0, 0, 0)
    assert stand.checklists.calls == []


async def test_the_deadline_is_read_at_the_moment_of_the_check() -> None:
    """Decision B2, and the reason the deadline is not stored on the run.

    A manager who widens `checklist_overdue_minutes` expects it to apply to the checklist
    that is open right now, not only to the next one.
    """
    stand = Stand(runs=[make_run(17, shift_id=1)], shifts=[make_shift(1)], overdue_minutes=180)

    report = await stand.service().run(now=utc(6, 31))

    assert report.reported == 0, "half past six is inside a three-hour window"
    assert stand.checklists.calls[0][2] == 180


async def test_the_checklist_of_another_venue_is_invisible() -> None:
    """TZ 3.3, acceptance 11.3: the rows live in one table and the scope is the predicate.

    Verified by mutation: dropping `row.venue_id == self.venue_id` from `FakeRuns` makes
    this test — and only this test — fail.
    """
    stand = Stand(
        runs=[make_run(18, shift_id=1, venue_id=OTHER_VENUE_ID)],
        shifts=[make_shift(1, venue_id=OTHER_VENUE_ID)],
    )

    report = await stand.service(VENUE_ID).run(now=utc(20, 0))

    assert (report.examined, report.reported, report.skipped) == (0, 0, 0)
    assert stand.checklists.calls == []
    assert (await stand.service(OTHER_VENUE_ID).run(now=utc(20, 0))).examined == 1, (
        "its own venue reads it perfectly well"
    )


async def test_a_shift_of_another_venue_does_not_lend_its_start() -> None:
    """The child lookup is scoped too: a run whose shift is invisible is not measurable."""
    stand = Stand(
        runs=[make_run(19, shift_id=1)],
        shifts=[make_shift(1, venue_id=OTHER_VENUE_ID)],
    )

    report = await stand.service().run(now=utc(20, 0))

    assert (report.examined, report.skipped) == (0, 1)
