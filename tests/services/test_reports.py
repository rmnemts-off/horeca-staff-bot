"""The report of one day (TZ 5.9; part III of the stage 1 spec).

What is under test is what the manager is told, not how a workbook is laid out: who worked,
how the checklists ended and — the reason the report exists — what nobody ticked.

Two properties carry the weight and are asserted rather than assumed. **A dismissed
employee stays in the report**, because TZ 5.1 keeps their row so the history survives and
the history is what this is. And **the wording comes from the run**, not from the template
that is active now (decision B3): a report of last month must not change because somebody
edited a checklist today.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from src.db.models import ChecklistType, RunStatus, Shift, User
from src.services.checklists import GroupView, ItemView, RunView
from src.services.reports import REPORTED_CHECKLISTS, ReportService

VENUE = "PIMS"
VENUE_ID = 1
OTHER_VENUE_ID = 2
DAY = dt.date(2026, 8, 15)


def make_shift(shift_id: int, *, user_id: int, venue_id: int = VENUE_ID, **kwargs: object) -> Shift:
    return Shift(
        id=shift_id,
        venue_id=venue_id,
        user_id=user_id,
        shift_date=DAY,
        start_time=kwargs.pop("start", dt.time(8)),  # type: ignore[arg-type]
        end_time=kwargs.pop("end", dt.time(14)),  # type: ignore[arg-type]
        is_opener=bool(kwargs.pop("is_opener", False)),
        is_closer=bool(kwargs.pop("is_closer", False)),
    )


def make_item(text: str, *, is_done: bool = False, is_critical: bool = False) -> ItemView:
    return ItemView(
        item_id=abs(hash(text)) % 10_000,
        text=text,
        group_index=1,
        group_name="Станция",
        order_index=1,
        is_done=is_done,
        is_critical=is_critical,
        requires_photo=False,
        requires_comment=False,
        done_at=None,
        photo_file_id=None,
        comment=None,
    )


def make_run(
    *,
    user_id: int,
    items: Sequence[ItemView],
    status: RunStatus = RunStatus.SKIPPED,
    checklist: ChecklistType = ChecklistType.OPENING,
    skip_comment: str | None = None,
) -> RunView:
    return RunView(
        run_id=1,
        template_id=1,
        checklist_type=checklist,
        status=status,
        shift_id=1,
        user_id=user_id,
        chat_id=None,
        message_id=None,
        sent_at=None,
        started_at=None,
        completed_at=dt.datetime(2026, 8, 15, 8, 20, tzinfo=dt.UTC),
        skip_comment=skip_comment,
        groups=(GroupView(index=1, name="Станция", items=tuple(items)),),
    )


class FakeShifts:
    """`ShiftRepository.list_for_date`, scoped the way the real one is (TZ 3.3)."""

    def __init__(self, venue_id: int, store: list[Shift]) -> None:
        self.venue_id = venue_id
        self.rows = store

    async def list_for_date(self, shift_date: dt.date) -> Sequence[Shift]:
        return [
            row
            for row in self.rows
            if row.venue_id == self.venue_id and row.shift_date == shift_date
        ]


class FakeChecklists:
    """`ChecklistService.run_for_shift` — a read, keyed by shift and type."""

    def __init__(self, runs: dict[tuple[int, ChecklistType], RunView]) -> None:
        self.runs = runs
        self.asked: list[tuple[int, ChecklistType]] = []

    async def run_for_shift(
        self,
        *,
        shift_id: int,
        checklist_type: ChecklistType,
    ) -> RunView | None:
        self.asked.append((shift_id, checklist_type))
        return self.runs.get((shift_id, checklist_type))


class FakeUsers:
    """`users` is global — the report reads a name by id and nothing else."""

    def __init__(self, rows: dict[int, str]) -> None:
        self.rows = rows
        self.reads: list[int] = []

    async def get(self, user_id: int) -> User | None:
        self.reads.append(user_id)
        name = self.rows.get(user_id)
        return None if name is None else User(id=user_id, telegram_id=user_id, full_name=name)


@dataclass
class Stand:
    shifts: list[Shift] = field(default_factory=list)
    runs: dict[tuple[int, ChecklistType], RunView] = field(default_factory=dict)
    names: dict[int, str] = field(default_factory=lambda: {7: "Иван Петров"})

    def service(self, venue_id: int = VENUE_ID) -> ReportService:
        self.checklists = FakeChecklists(self.runs)
        self.users = FakeUsers(self.names)
        return ReportService(
            venue=VENUE,
            shifts=FakeShifts(venue_id, self.shifts),
            checklists=self.checklists,
            users=self.users,
        )


@pytest.fixture
def stand() -> Stand:
    return Stand()


async def test_a_day_nobody_worked_is_an_answer_and_not_a_failure(stand: Stand) -> None:
    """TZ 8.1: the empty state is a normal screen everywhere, this one included."""
    report = await stand.service().shift_report(DAY)

    assert report.is_empty
    assert report.shifts == ()
    assert report.runs == ()
    assert report.pending == ()


async def test_the_report_says_who_worked_and_who_opened(stand: Stand) -> None:
    stand.shifts.append(make_shift(1, user_id=7, is_opener=True))

    report = await stand.service().shift_report(DAY)

    (line,) = report.shifts
    assert line.full_name == "Иван Петров"
    assert (line.is_opener, line.is_closer) == (True, False)
    assert report.venue == VENUE
    assert report.day == DAY


async def test_the_unticked_lines_are_the_point_of_the_report(stand: Stand) -> None:
    stand.shifts.append(make_shift(1, user_id=7))
    stand.runs[(1, ChecklistType.OPENING)] = make_run(
        user_id=7,
        items=[make_item("Протереть стойку", is_done=True), make_item("Проверить лёд")],
        skip_comment="не привезли лёд",
    )

    report = await stand.service().shift_report(DAY)

    (run,) = report.runs
    assert (run.done, run.total) == (1, 2)
    assert run.skip_comment == "не привезли лёд"
    (pending,) = report.pending
    assert pending.text == "Проверить лёд"
    assert pending.group_name == "Станция"


async def test_critical_lines_come_first(stand: Stand) -> None:
    """TZ 6 escalates by the critical flag, and the report is read in the same order."""
    stand.shifts.append(make_shift(1, user_id=7))
    stand.runs[(1, ChecklistType.OPENING)] = make_run(
        user_id=7,
        items=[make_item("Протереть стойку"), make_item("Проверить лёд", is_critical=True)],
    )

    report = await stand.service().shift_report(DAY)

    assert [line.text for line in report.pending] == ["Проверить лёд", "Протереть стойку"]


async def test_a_dismissed_employee_is_still_named(stand: Stand) -> None:
    """TZ 5.1 keeps their row so the history survives — and the history is this report.

    The name is read from `users`, the global table, and never from the roster, which lists
    the active only. Reading it from the roster would make a report of last month go blank
    the day somebody leaves.
    """
    stand.shifts.append(make_shift(1, user_id=7))
    stand.names[7] = "Уволенный Сотрудник"

    report = await stand.service().shift_report(DAY)

    assert report.shifts[0].full_name == "Уволенный Сотрудник"


async def test_a_shift_of_another_venue_is_invisible(stand: Stand) -> None:
    """TZ 3.3, acceptance 11.3: the rows live in one table and the scope is the predicate.

    Verified by mutation: dropping the venue predicate from `FakeShifts` makes this test —
    and only this test — fail.
    """
    stand.shifts.append(make_shift(1, user_id=7, venue_id=OTHER_VENUE_ID))

    report = await stand.service(VENUE_ID).shift_report(DAY)

    assert report.is_empty
    assert not (await stand.service(OTHER_VENUE_ID).shift_report(DAY)).is_empty


async def test_both_checklists_of_a_shift_are_asked_for(stand: Stand) -> None:
    """A shift has an opening and a closing checklist, and the report is about the day."""
    stand.shifts.append(make_shift(1, user_id=7))

    await stand.service().shift_report(DAY)

    assert stand.checklists.asked == [(1, checklist) for checklist in REPORTED_CHECKLISTS]


async def test_a_name_is_read_once_per_person(stand: Stand) -> None:
    """A shift has two checklists and one person; three reads would be two too many."""
    stand.shifts.append(make_shift(1, user_id=7))
    for checklist in REPORTED_CHECKLISTS:
        stand.runs[(1, checklist)] = make_run(
            user_id=7, items=[make_item("Пункт")], checklist=checklist
        )

    await stand.service().shift_report(DAY)

    assert stand.users.reads == [7]
