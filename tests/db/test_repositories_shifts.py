"""Integration tests for `ShiftRepo` (plan, task 12; TZ 4.2, 5.3).

Three things a live database has to prove and the static guard cannot:

* a night shift is addressed by the date it **starts** on, so `18:00-02:00` is one row on the
  evening it opened and nothing at all on the morning it ends (TZ 5.3);
* the range queries are inclusive on both ends, which is what the two-week schedule screen
  and the one-day lookback of "the shift running now" depend on;
* a shift of another venue is not readable, not editable and not deletable, whatever id is
  handed in (TZ 9, acceptance 11.3).

Marked `db`; the harness in `tests/conftest.py` gives every test its own rolled-back
transaction.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import Shift, ShiftSource, ShiftStatus, User, Venue
from src.db.repositories.shifts import ShiftRepo

from tests.factories import create_shift, create_user, create_venue

pytestmark = pytest.mark.db

DAY = dt.date(2026, 6, 1)
MORNING = dt.time(8, 0)
EVENING = dt.time(18, 0)
NIGHT_END = dt.time(2, 0)
CLOSING = dt.time(23, 0)


async def a_venue_with_a_person(session: AsyncSession) -> tuple[Venue, User]:
    return await create_venue(session), await create_user(session)


async def a_shift(
    session: AsyncSession,
    venue: Venue,
    user: User,
    *,
    shift_date: dt.date = DAY,
    start_time: dt.time = MORNING,
    end_time: dt.time = CLOSING,
    is_opener: bool = False,
    is_closer: bool = False,
) -> Shift:
    return await create_shift(
        session,
        venue,
        user,
        shift_date=shift_date,
        start_time=start_time,
        end_time=end_time,
        is_opener=is_opener,
        is_closer=is_closer,
    )


# --------------------------------------------------------------------------------------
# create / read / update / delete
# --------------------------------------------------------------------------------------


async def test_create_takes_the_venue_from_the_scope(session: AsyncSession) -> None:
    venue, user = await a_venue_with_a_person(session)
    repo = ShiftRepo(session, venue.id)

    created = await repo.create(
        user_id=user.id,
        shift_date=DAY,
        start_time=MORNING,
        end_time=CLOSING,
        hours=Decimal("15.00"),
        status=ShiftStatus.PLANNED,
        source=ShiftSource.MANUAL,
        is_opener=True,
        is_closer=False,
    )

    stored = await repo.get(created.id)
    assert stored is not None
    assert stored.venue_id == venue.id
    assert stored.user_id == user.id
    assert stored.hours == Decimal("15.00")
    assert stored.is_opener is True


async def test_update_recomputed_fields_are_written_back(session: AsyncSession) -> None:
    venue, user = await a_venue_with_a_person(session)
    repo = ShiftRepo(session, venue.id)
    shift = await a_shift(session, venue, user)

    updated = await repo.update(shift.id, end_time=EVENING, hours=Decimal("10.00"))

    assert updated is not None
    assert updated.end_time == EVENING
    assert updated.hours == Decimal("10.00")


async def test_update_refuses_a_column_that_does_not_exist(session: AsyncSession) -> None:
    venue, user = await a_venue_with_a_person(session)
    shift = await a_shift(session, venue, user)

    with pytest.raises(AttributeError):
        await ShiftRepo(session, venue.id).update(shift.id, hour=Decimal("1.00"))


async def test_delete_reports_whether_a_row_went_away(session: AsyncSession) -> None:
    venue, user = await a_venue_with_a_person(session)
    repo = ShiftRepo(session, venue.id)
    shift = await a_shift(session, venue, user)

    assert await repo.delete(shift.id) is True
    assert await repo.get(shift.id) is None
    assert await repo.delete(shift.id) is False


# --------------------------------------------------------------------------------------
# a night shift belongs to the date it starts on (TZ 5.3)
# --------------------------------------------------------------------------------------


async def test_a_night_shift_is_listed_on_the_date_it_starts(session: AsyncSession) -> None:
    venue, user = await a_venue_with_a_person(session)
    night = await a_shift(session, venue, user, start_time=EVENING, end_time=NIGHT_END)
    repo = ShiftRepo(session, venue.id)

    on_the_evening = await repo.list_for_date(DAY)
    on_the_morning = await repo.list_for_date(DAY + dt.timedelta(days=1))

    assert [shift.id for shift in on_the_evening] == [night.id]
    assert list(on_the_morning) == []


async def test_a_shift_ending_at_midnight_stays_on_its_own_date(session: AsyncSession) -> None:
    """`14:00-00:00` ends on the next calendar day and is still dated by its start."""
    venue, user = await a_venue_with_a_person(session)
    shift = await a_shift(session, venue, user, start_time=dt.time(14, 0), end_time=dt.time(0, 0))

    found = await ShiftRepo(session, venue.id).list_for_date(DAY)

    assert [row.id for row in found] == [shift.id]


# --------------------------------------------------------------------------------------
# the composition of a date, and the ranges
# --------------------------------------------------------------------------------------


async def test_the_roster_of_a_date_is_ordered_by_start_time(session: AsyncSession) -> None:
    venue, first = await a_venue_with_a_person(session)
    second = await create_user(session)
    late = await a_shift(session, venue, second, start_time=EVENING, end_time=NIGHT_END)
    early = await a_shift(session, venue, first, start_time=MORNING, end_time=CLOSING)

    found = await ShiftRepo(session, venue.id).list_for_date(DAY)

    assert [shift.id for shift in found] == [early.id, late.id]


async def test_the_roster_of_a_date_ignores_other_venues(session: AsyncSession) -> None:
    venue, user = await a_venue_with_a_person(session)
    other_venue, other_user = await a_venue_with_a_person(session)
    here = await a_shift(session, venue, user)
    await a_shift(session, other_venue, other_user)

    found = await ShiftRepo(session, venue.id).list_for_date(DAY)

    assert [shift.id for shift in found] == [here.id]


async def test_the_range_of_one_person_is_inclusive_on_both_ends(session: AsyncSession) -> None:
    venue, user = await a_venue_with_a_person(session)
    colleague = await create_user(session)
    before = await a_shift(session, venue, user, shift_date=DAY - dt.timedelta(days=1))
    first = await a_shift(session, venue, user, shift_date=DAY)
    last = await a_shift(session, venue, user, shift_date=DAY + dt.timedelta(days=2))
    after = await a_shift(session, venue, user, shift_date=DAY + dt.timedelta(days=3))
    theirs = await a_shift(session, venue, colleague, shift_date=DAY)

    found = await ShiftRepo(session, venue.id).list_for_user(
        user.id,
        date_from=DAY,
        date_to=DAY + dt.timedelta(days=2),
    )

    ids = [shift.id for shift in found]
    assert ids == [first.id, last.id]
    assert before.id not in ids
    assert after.id not in ids
    assert theirs.id not in ids


async def test_the_range_of_the_whole_venue_covers_everyone(session: AsyncSession) -> None:
    venue, user = await a_venue_with_a_person(session)
    colleague = await create_user(session)
    other_venue, other_user = await a_venue_with_a_person(session)
    mine = await a_shift(session, venue, user, shift_date=DAY)
    theirs = await a_shift(session, venue, colleague, shift_date=DAY + dt.timedelta(days=1))
    await a_shift(session, other_venue, other_user, shift_date=DAY)

    found = await ShiftRepo(session, venue.id).list_between(
        date_from=DAY,
        date_to=DAY + dt.timedelta(days=1),
    )

    assert [shift.id for shift in found] == [mine.id, theirs.id]


async def test_an_empty_schedule_is_an_empty_list(session: AsyncSession) -> None:
    """TZ 8.1: a venue that has entered nothing yet is the main scenario."""
    venue, user = await a_venue_with_a_person(session)
    repo = ShiftRepo(session, venue.id)

    assert list(await repo.list_for_date(DAY)) == []
    assert list(await repo.list_between(date_from=DAY, date_to=DAY)) == []
    assert list(await repo.list_for_user(user.id, date_from=DAY, date_to=DAY)) == []


# --------------------------------------------------------------------------------------
# who opens and who closes
# --------------------------------------------------------------------------------------


async def test_opener_and_closer_are_read_per_date(session: AsyncSession) -> None:
    venue, opener = await a_venue_with_a_person(session)
    closer = await create_user(session)
    opening = await a_shift(session, venue, opener, is_opener=True)
    closing = await a_shift(
        session, venue, closer, start_time=EVENING, end_time=NIGHT_END, is_closer=True
    )
    repo = ShiftRepo(session, venue.id)

    found_opener = await repo.get_opener(DAY)
    found_closer = await repo.get_closer(DAY)

    assert found_opener is not None
    assert found_opener.id == opening.id
    assert found_closer is not None
    assert found_closer.id == closing.id


async def test_a_date_without_marks_has_neither(session: AsyncSession) -> None:
    """Decision B4: a date with nobody opening it is saved and reported, not refused."""
    venue, user = await a_venue_with_a_person(session)
    await a_shift(session, venue, user)
    repo = ShiftRepo(session, venue.id)

    assert await repo.get_opener(DAY) is None
    assert await repo.get_closer(DAY) is None


async def test_the_opener_of_another_venue_is_not_seen(session: AsyncSession) -> None:
    venue, _ = await a_venue_with_a_person(session)
    other_venue, other_user = await a_venue_with_a_person(session)
    await a_shift(session, other_venue, other_user, is_opener=True)

    assert await ShiftRepo(session, venue.id).get_opener(DAY) is None


# --------------------------------------------------------------------------------------
# venue isolation (acceptance 11.3)
# --------------------------------------------------------------------------------------


async def test_a_shift_of_another_venue_is_unreachable(session: AsyncSession) -> None:
    venue, _ = await a_venue_with_a_person(session)
    other_venue, other_user = await a_venue_with_a_person(session)
    foreign = await a_shift(session, other_venue, other_user)

    repo = ShiftRepo(session, venue.id)

    assert await repo.get(foreign.id) is None
    assert await repo.update(foreign.id, status=ShiftStatus.CANCELLED) is None
    assert await repo.delete(foreign.id) is False

    survivor = await ShiftRepo(session, other_venue.id).get(foreign.id)
    assert survivor is not None
    assert survivor.status is ShiftStatus.PLANNED
