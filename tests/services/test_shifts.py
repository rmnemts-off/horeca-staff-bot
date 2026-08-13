"""Shift service (plan, task 16; TZ 4.2, 5.3; decisions B4, D8, D12).

Everything here runs against fake repositories, so the whole module is green on a machine
without PostgreSQL. The fakes are not a database emulator — they reproduce exactly the three
promises `src/db/repositories/protocols.py` makes, because those are what the service is
built on:

* a repository belongs to one venue and never takes `venue_id` as an argument, so
  :class:`FakeShifts` and :class:`FakeMembers` filter the shared store by the venue they were
  built for. A shift — or an employee — of another venue is not "forbidden", it simply is not
  there (TZ 3.3, acceptance 11.3);
* a missing row comes back as `None`, never as an exception;
* the lists arrive in the order `ShiftRepo` puts them in — `ORDER BY start_time, id` for a
  date, `ORDER BY shift_date, start_time, id` for a range. Not a convenience: `get_opener`
  and `get_closer` answer with the *first* row of a date carrying the mark, and decision D8
  leaves "two of them at once" legal in the middle of the stage 1 replacement transaction,
  so a fake handing rows back in insertion order would name a different shift than the
  database does;
* a write is as narrow as the statement behind it. :meth:`FakeShifts.update` goes through
  :func:`apply_fields`, the `_apply` of `src/db/repositories/shifts.py`, so a misspelt column
  raises here instead of passing green and failing against PostgreSQL; and `hours` is a
  required argument of :meth:`FakeShiftStore.add`, because `shifts.hours` is `NOT NULL` and
  `ShiftRepo.create` passes the caller's fields through untouched (TZ 4.2: the column is
  computed by the service, never by the repository).

The clock is never read: every method under test takes `now` as an aware UTC argument
(decision D12), which is what makes the midnight cases below plain arithmetic.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from src.db.models import MemberRole, Shift, ShiftSource, ShiftStatus, User, VenueMember
from src.services.access import AccessContext, PermissionDeniedError, VenueMismatchError
from src.services.shifts import (
    SCHEDULE_HORIZON_DAYS,
    ShiftNotFoundError,
    ShiftNotifier,
    ShiftRole,
    ShiftRoleTakenError,
    ShiftService,
    ShiftUserNotInVenueError,
    ShiftWarning,
    shift_hours,
)
from src.services.timezones import venue_timezone

if TYPE_CHECKING:  # pragma: no cover — a type-level tripwire, never executed
    from src.services.notifications import NotificationService

    def _the_notification_service_fits_the_seam(service: NotificationService) -> ShiftNotifier:
        """The two halves of "any edit reschedules" must keep fitting each other.

        :class:`ShiftNotifier` is declared in `src/services/shifts.py` and implemented by
        `NotificationService` (plan, task 19) — two files, two waves. This line is checked
        by mypy and by nothing else: if either side renames a method or changes an argument,
        the type check fails instead of the integration failing silently later.
        """
        return service


VENUE_ID = 1
OTHER_VENUE_ID = 2

# Answer A2: the test venue is PIMS, Moscow, standard shift 08:00-23:00.
VENUE_TZ = "Europe/Moscow"
DEFAULT_START = dt.time(8, 0)
DEFAULT_END = dt.time(23, 0)

STAFF_ID = 7
MATE_ID = 8
MANAGER_ID = 9
#: A manager of the *other* venue, and the person a leak would hand over (acceptance 11.3).
OUTSIDER_ID = 11
#: Worked here, was dismissed: the `venue_members` row survives, `is_active` does not (TZ 5.1).
DISMISSED_ID = 12
#: Known to `users` and to no venue at all — the state of somebody who only pressed /start.
NOBODY_ID = 13

DAY = dt.date(2026, 8, 13)


# --------------------------------------------------------------------------------------
# Actors (declared here: tests/factories.py belongs to task 5a)
# --------------------------------------------------------------------------------------


def actor(
    role: MemberRole,
    *,
    user_id: int,
    venue_id: int = VENUE_ID,
) -> AccessContext:
    return AccessContext(
        user_id=user_id,
        telegram_id=100_000 + user_id,
        venue_id=venue_id,
        member_id=user_id,
        role=role,
        full_name=f"user-{user_id}",
    )


STAFF = actor(MemberRole.STAFF, user_id=STAFF_ID)
MATE = actor(MemberRole.STAFF, user_id=MATE_ID)
MANAGER = actor(MemberRole.MANAGER, user_id=MANAGER_ID)
STRANGER = actor(MemberRole.MANAGER, user_id=OUTSIDER_ID, venue_id=OTHER_VENUE_ID)


def make_user(user_id: int, full_name: str) -> User:
    return User(
        id=user_id,
        telegram_id=100_000 + user_id,
        full_name=full_name,
        is_active=True,
        is_bot_blocked=False,
    )


# --------------------------------------------------------------------------------------
# Fake repositories
# --------------------------------------------------------------------------------------


def apply_fields(row: object, fields: Mapping[str, Any]) -> None:
    """`_apply` of `src/db/repositories/shifts.py` and `members.py`, character for character.

    A bare `setattr` loop is the generous version: `update(shift_id, hourz=...)` would create
    an instance attribute, the fake would answer happily and the same call would fail against
    the database. An unknown column is a typo, and a typo has to be loud here.
    """
    for name, value in fields.items():
        if not hasattr(type(row), name):
            raise AttributeError(f"{type(row).__name__} has no column {name!r}")
        setattr(row, name, value)


def _by_time(row: Shift) -> tuple[dt.time, int]:
    """`ORDER BY start_time, id` of `ShiftRepo.list_for_date` and `_role_holder`."""
    return (row.start_time, row.id)


def _by_date_and_time(row: Shift) -> tuple[dt.date, dt.time, int]:
    """`ORDER BY shift_date, start_time, id` of `list_for_user` and `list_between`."""
    return (row.shift_date, row.start_time, row.id)


class FakeShiftStore:
    """One table for every venue, exactly like the real `shifts`.

    Two venues share it so that "another venue's shift" is a row that genuinely exists and
    is genuinely unreachable through the wrong repository — the point of acceptance 11.3.
    """

    def __init__(self) -> None:
        self.rows: list[Shift] = []
        self._next_id = 1

    def add(
        self,
        *,
        venue_id: int,
        user_id: int,
        shift_date: dt.date,
        start_time: dt.time,
        end_time: dt.time,
        hours: Decimal,
        is_opener: bool = False,
        is_closer: bool = False,
        status: ShiftStatus = ShiftStatus.PLANNED,
        source: ShiftSource = ShiftSource.MANUAL,
        created_by: int | None = None,
    ) -> Shift:
        """`hours` has no default here, exactly as `shifts.hours` has none in the schema.

        It used to be computed when the argument was omitted, which made the fake kinder than
        the table: `ShiftRepo.create` hands `Shift(**fields)` to the session untouched, so a
        service that forgot the column would raise `NotNullViolation` in production and pass
        every test here (TZ 4.2 — `shift_hours` is the service's job and nobody else's).
        """
        shift = Shift(
            id=self._next_id,
            venue_id=venue_id,
            user_id=user_id,
            shift_date=shift_date,
            start_time=start_time,
            end_time=end_time,
            hours=hours,
            is_opener=is_opener,
            is_closer=is_closer,
            status=status,
            source=source,
            created_by=created_by,
        )
        self._next_id += 1
        self.rows.append(shift)
        return shift


class FakeShifts:
    """`ShiftRepository`, scoped to one venue (TZ 3.3)."""

    def __init__(self, store: FakeShiftStore, venue_id: int) -> None:
        self.store = store
        self.venue_id = venue_id

    def _scoped(self) -> list[Shift]:
        return [row for row in self.store.rows if row.venue_id == self.venue_id]

    async def get(self, shift_id: int) -> Shift | None:
        return next((row for row in self._scoped() if row.id == shift_id), None)

    async def list_for_date(self, shift_date: dt.date) -> Sequence[Shift]:
        """`ORDER BY start_time, id` — the order the day runs (`ShiftRepo.list_for_date`)."""
        return sorted(
            (row for row in self._scoped() if row.shift_date == shift_date),
            key=_by_time,
        )

    async def list_for_user(
        self,
        user_id: int,
        *,
        date_from: dt.date,
        date_to: dt.date,
    ) -> Sequence[Shift]:
        """`ORDER BY shift_date, start_time, id`, as the real statement spells it."""
        return sorted(
            (
                row
                for row in self._scoped()
                if row.user_id == user_id and date_from <= row.shift_date <= date_to
            ),
            key=_by_date_and_time,
        )

    async def list_between(self, *, date_from: dt.date, date_to: dt.date) -> Sequence[Shift]:
        return sorted(
            (row for row in self._scoped() if date_from <= row.shift_date <= date_to),
            key=_by_date_and_time,
        )

    async def get_opener(self, shift_date: dt.date) -> Shift | None:
        """The *first* marked row of the date — which is why the order above is not cosmetic."""
        roster = await self.list_for_date(shift_date)
        return next((row for row in roster if row.is_opener), None)

    async def get_closer(self, shift_date: dt.date) -> Shift | None:
        roster = await self.list_for_date(shift_date)
        return next((row for row in roster if row.is_closer), None)

    async def create(self, **fields: Any) -> Shift:
        """Fields go to the table as they are: `ShiftRepo.create` computes nothing (TZ 4.2).

        `hours` is therefore not defaulted anywhere on this path — a caller that omits it
        gets a `TypeError` here and a `NOT NULL` violation against PostgreSQL.
        """
        return self.store.add(venue_id=self.venue_id, **fields)

    async def update(self, shift_id: int, **fields: Any) -> Shift | None:
        row = await self.get(shift_id)
        if row is None:
            return None
        apply_fields(row, fields)
        return row

    async def delete(self, shift_id: int) -> bool:
        row = await self.get(shift_id)
        if row is None:
            return False
        self.store.rows.remove(row)
        return True


class FakeUsers:
    """`UserRepository` — global, because a person may work in several venues (TZ 2)."""

    def __init__(self, users: Sequence[User] = ()) -> None:
        self.users = {user.id: user for user in users}

    async def get(self, user_id: int) -> User | None:
        return self.users.get(user_id)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        return next((u for u in self.users.values() if u.telegram_id == telegram_id), None)

    async def create(
        self,
        *,
        telegram_id: int,
        full_name: str,
        username: str | None = None,
        phone: str | None = None,
    ) -> User:
        raise NotImplementedError

    async def update(self, user_id: int, **fields: Any) -> User | None:
        raise NotImplementedError

    async def touch_last_seen(self, user_id: int, moment: dt.datetime) -> None:
        raise NotImplementedError

    async def set_active_venue(self, user_id: int, venue_id: int | None) -> None:
        raise NotImplementedError

    async def set_bot_blocked(self, user_id: int, *, blocked: bool) -> None:
        raise NotImplementedError


class FakeMemberStore:
    """One `venue_members` table for every venue, like the real one.

    The point of sharing it: an employee of the other venue is a row that genuinely exists,
    with a real `user_id` a forged form could name — and is still unreachable through this
    venue's repository (TZ 3.3, acceptance 11.3).
    """

    def __init__(self) -> None:
        self.rows: list[VenueMember] = []
        self._next_id = 1

    def add(
        self,
        *,
        venue_id: int,
        user_id: int,
        role: MemberRole = MemberRole.STAFF,
        is_active: bool = True,
    ) -> VenueMember:
        member = VenueMember(
            id=self._next_id,
            venue_id=venue_id,
            user_id=user_id,
            role=role,
            position=None,
            is_active=is_active,
        )
        self._next_id += 1
        self.rows.append(member)
        return member


class FakeMembers:
    """`VenueMemberRepository`, scoped to one venue (TZ 3.3)."""

    def __init__(self, store: FakeMemberStore, venue_id: int) -> None:
        self.store = store
        self.venue_id = venue_id

    def _scoped(self) -> list[VenueMember]:
        return [row for row in self.store.rows if row.venue_id == self.venue_id]

    async def get(self, member_id: int) -> VenueMember | None:
        return next((row for row in self._scoped() if row.id == member_id), None)

    async def get_for_user(self, user_id: int) -> VenueMember | None:
        """A dismissed employee still has a row: `is_active` is the caller's to read."""
        return next((row for row in self._scoped() if row.user_id == user_id), None)

    async def list_active(self) -> Sequence[VenueMember]:
        return [row for row in self._scoped() if row.is_active]

    async def list_by_role(self, role: MemberRole) -> Sequence[VenueMember]:
        return [row for row in self._scoped() if row.role is role]

    async def add(
        self,
        *,
        user_id: int,
        role: MemberRole,
        position: str | None = None,
    ) -> VenueMember:
        raise NotImplementedError

    async def update(self, member_id: int, **fields: Any) -> VenueMember | None:
        raise NotImplementedError

    async def set_active(self, member_id: int, *, is_active: bool) -> VenueMember | None:
        raise NotImplementedError


class RecordingNotifier:
    """`ShiftNotifier`: remembers that it was called, decides nothing.

    Plan, task 16: *any* change to a shift has to reach the scheduler. What the scheduler
    then does with it is task 19's business and is tested there.
    """

    def __init__(self) -> None:
        self.saved: list[Shift] = []
        self.removed: list[int] = []

    async def shift_saved(self, shift: Shift) -> object:
        self.saved.append(shift)
        return None

    async def shift_removed(self, shift_id: int) -> object:
        self.removed.append(shift_id)
        return None

    @property
    def calls(self) -> int:
        return len(self.saved) + len(self.removed)


# --------------------------------------------------------------------------------------
# Stand
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Stand:
    store: FakeShiftStore
    users: FakeUsers
    roster: FakeMemberStore
    notifier: RecordingNotifier
    service: ShiftService
    #: The same store seen through another venue's repository, for the isolation tests.
    other: ShiftService
    people: dict[int, str] = field(default_factory=dict)

    def add(self, **fields: Any) -> Shift:
        """Seed a row the way the service would have written it — `hours` included.

        The arithmetic is here, in the fixture, and not inside the store: a row planted by
        hand has to carry the same `hours` the service computes (TZ 4.2), while the write
        path stays as strict as `shifts.hours NOT NULL`.
        """
        fields.setdefault("venue_id", VENUE_ID)
        fields.setdefault("user_id", STAFF_ID)
        fields.setdefault("shift_date", DAY)
        fields.setdefault("start_time", DEFAULT_START)
        fields.setdefault("end_time", DEFAULT_END)
        fields.setdefault("hours", shift_hours(fields["start_time"], fields["end_time"]))
        return self.store.add(**fields)


def build(timezone: str = VENUE_TZ) -> Stand:
    store = FakeShiftStore()
    users = FakeUsers(
        [
            make_user(STAFF_ID, "Анна"),
            make_user(MATE_ID, "Борис"),
            make_user(MANAGER_ID, "Виктор"),
            make_user(OUTSIDER_ID, "Chuck"),
            make_user(DISMISSED_ID, "Dana"),
            make_user(NOBODY_ID, "Eve"),
        ]
    )
    roster = FakeMemberStore()
    roster.add(venue_id=VENUE_ID, user_id=STAFF_ID)
    roster.add(venue_id=VENUE_ID, user_id=MATE_ID)
    roster.add(venue_id=VENUE_ID, user_id=MANAGER_ID, role=MemberRole.MANAGER)
    roster.add(venue_id=VENUE_ID, user_id=DISMISSED_ID, is_active=False)
    roster.add(venue_id=OTHER_VENUE_ID, user_id=OUTSIDER_ID, role=MemberRole.MANAGER)
    notifier = RecordingNotifier()
    return Stand(
        store=store,
        users=users,
        roster=roster,
        notifier=notifier,
        service=ShiftService(
            venue_id=VENUE_ID,
            timezone=timezone,
            shifts=FakeShifts(store, VENUE_ID),
            users=users,
            members=FakeMembers(roster, VENUE_ID),
            notifier=notifier,
        ),
        other=ShiftService(
            venue_id=OTHER_VENUE_ID,
            timezone=timezone,
            shifts=FakeShifts(store, OTHER_VENUE_ID),
            users=users,
            members=FakeMembers(roster, OTHER_VENUE_ID),
            notifier=notifier,
        ),
    )


@pytest.fixture
def stand() -> Stand:
    return build()


def utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.UTC)


# --------------------------------------------------------------------------------------
# hours: computed, never typed in (TZ 4.2, plan task 16)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        # Answer A2: the standard shift of the test venue.
        (dt.time(8, 0), dt.time(23, 0), "15.00"),
        # The midnight edge: the end belongs to the next day, so this is ten hours, not
        # a negative fourteen and not zero.
        (dt.time(14, 0), dt.time(0, 0), "10.00"),
        # A night shift, the other side of the same rule.
        (dt.time(18, 0), dt.time(2, 0), "8.00"),
        (dt.time(10, 30), dt.time(19, 0), "8.50"),
        # start == end is a full day: the only reading consistent with `shift_window`.
        (dt.time(12, 0), dt.time(12, 0), "24.00"),
    ],
)
def test_hours_come_from_the_window(start: dt.time, end: dt.time, expected: str) -> None:
    assert shift_hours(start, end) == Decimal(expected)


async def test_create_computes_hours_and_refuses_to_be_told_them(stand: Stand) -> None:
    saved = await stand.service.create(
        MANAGER,
        user_id=STAFF_ID,
        shift_date=DAY,
        start_time=dt.time(14, 0),
        end_time=dt.time(0, 0),
    )

    assert saved.shift.hours == Decimal("10.00")
    # TZ 4.2: the column is derived, so there is no way to hand it a different number.
    with pytest.raises(TypeError):
        await stand.service.create(  # type: ignore[call-arg]
            MANAGER,
            user_id=STAFF_ID,
            shift_date=DAY + dt.timedelta(days=1),
            start_time=dt.time(14, 0),
            end_time=dt.time(0, 0),
            hours=Decimal("99.00"),
        )


async def test_update_recomputes_hours_from_the_new_window(stand: Stand) -> None:
    shift = stand.add(start_time=dt.time(14, 0), end_time=dt.time(0, 0))

    saved = await stand.service.update(MANAGER, shift.id, start_time=dt.time(18, 0))

    # Only the start moved; the end is still midnight, so the row must say six hours.
    assert saved.shift.hours == Decimal("6.00")


async def test_an_edit_that_leaves_the_window_alone_leaves_hours_alone(stand: Stand) -> None:
    shift = stand.add(start_time=dt.time(14, 0), end_time=dt.time(0, 0))

    saved = await stand.service.update(MANAGER, shift.id, status=ShiftStatus.CONFIRMED)

    assert saved.shift.hours == Decimal("10.00")
    assert saved.shift.status is ShiftStatus.CONFIRMED


# --------------------------------------------------------------------------------------
# The window: a shift belongs to the date it starts on (TZ 5.3, D12)
# --------------------------------------------------------------------------------------


async def test_a_shift_ending_at_midnight_ends_on_the_next_day(stand: Stand) -> None:
    """14:00-00:00, the edge the plan singles out: the end is already tomorrow."""
    shift = stand.add(start_time=dt.time(14, 0), end_time=dt.time(0, 0))

    view = await stand.service.get(MANAGER, shift.id)

    assert view is not None
    # Moscow is UTC+3 the whole year round.
    assert view.starts_at == utc(2026, 8, 13, 11, 0)
    assert view.ends_at == utc(2026, 8, 13, 21, 0)
    assert view.ends_at - view.starts_at == dt.timedelta(hours=10)
    assert view.shift_date == DAY
    assert view.crosses_midnight is True


async def test_a_night_shift_is_dated_by_its_start(stand: Stand) -> None:
    """18:00-02:00 is one shift dated the day it opened, not two halves (TZ 5.3)."""
    shift = stand.add(start_time=dt.time(18, 0), end_time=dt.time(2, 0))

    view = await stand.service.get(MANAGER, shift.id)

    assert view is not None
    assert view.starts_at == utc(2026, 8, 13, 15, 0)
    assert view.ends_at == utc(2026, 8, 13, 23, 0)
    assert view.shift_date == DAY
    assert view.crosses_midnight is True
    assert view.hours == Decimal("8.00")


async def test_an_ordinary_shift_does_not_cross_midnight(stand: Stand) -> None:
    shift = stand.add()

    view = await stand.service.get(MANAGER, shift.id)

    assert view is not None
    assert view.starts_at == utc(2026, 8, 13, 5, 0)
    assert view.ends_at == utc(2026, 8, 13, 20, 0)
    assert view.crosses_midnight is False


async def test_the_window_survives_a_daylight_saving_transition() -> None:
    """The wall clock and the elapsed time disagree, and each column says its own thing.

    Europe/Berlin, the night of 2026-03-29: 02:00 becomes 03:00. A 22:00-06:00 shift reads
    eight hours on the schedule sheet — that is what TZ 4.2 calls `hours` — while the clock
    actually runs for seven, and that is what the scheduler must use.
    """
    berlin = build("Europe/Berlin")
    shift = berlin.add(
        shift_date=dt.date(2026, 3, 28),
        start_time=dt.time(22, 0),
        end_time=dt.time(6, 0),
    )

    view = await berlin.service.get(MANAGER, shift.id)

    assert view is not None
    assert view.hours == Decimal("8.00")
    assert view.starts_at == utc(2026, 3, 28, 21, 0)
    assert view.ends_at == utc(2026, 3, 29, 4, 0)
    assert view.ends_at - view.starts_at == dt.timedelta(hours=7)


async def test_the_timezone_may_be_handed_over_as_a_zone_object() -> None:
    stand = build()
    named = ShiftService(
        venue_id=VENUE_ID,
        timezone=venue_timezone(VENUE_TZ),
        shifts=FakeShifts(stand.store, VENUE_ID),
        users=stand.users,
        members=FakeMembers(stand.roster, VENUE_ID),
        notifier=stand.notifier,
    )
    assert named.timezone == stand.service.timezone
    assert named.venue_id == VENUE_ID


# --------------------------------------------------------------------------------------
# Current shift, next shift, the fortnight (TZ 5.3, plan task 23)
# --------------------------------------------------------------------------------------


async def test_the_shift_ending_at_midnight_is_over_at_half_past(stand: Stand) -> None:
    """The other half of the midnight edge: 00:00 is the end, so 00:30 is not "during"."""
    stand.add(start_time=dt.time(14, 0), end_time=dt.time(0, 0))

    # 00:30 Moscow on the next day.
    half_past = utc(2026, 8, 13, 21, 30)
    assert await stand.service.current_shift(STAFF, now=half_past) is None

    # One second before midnight it is still being worked.
    assert await stand.service.current_shift(STAFF, now=utc(2026, 8, 13, 20, 59, 59)) is not None
    # And the end instant itself is already outside: the window is half-open.
    assert await stand.service.current_shift(STAFF, now=utc(2026, 8, 13, 21, 0)) is None


async def test_a_night_shift_is_still_current_after_midnight(stand: Stand) -> None:
    """18:00-02:00 at 00:30: the date rolled over, the shift did not (plan, task 23)."""
    shift = stand.add(start_time=dt.time(18, 0), end_time=dt.time(2, 0))

    running = await stand.service.current_shift(STAFF, now=utc(2026, 8, 13, 21, 30))

    assert running is not None
    assert running.shift_id == shift.id
    # A query on "today" would have missed it: locally it is already the 14th.
    assert running.shift_date == DAY


async def test_my_shift_prefers_the_running_one_over_the_next(stand: Stand) -> None:
    running = stand.add(start_time=dt.time(18, 0), end_time=dt.time(2, 0))
    tomorrow = stand.add(shift_date=DAY + dt.timedelta(days=1))

    midnight_half = utc(2026, 8, 13, 21, 30)
    nearest = await stand.service.nearest_shift(STAFF, now=midnight_half)
    following = await stand.service.next_shift(STAFF, now=midnight_half)

    assert nearest is not None
    assert nearest.shift_id == running.id
    assert following is not None
    assert following.shift_id == tomorrow.id


async def test_off_duty_my_shift_falls_back_to_the_next_one(stand: Stand) -> None:
    stand.add(start_time=dt.time(14, 0), end_time=dt.time(0, 0))
    tomorrow = stand.add(shift_date=DAY + dt.timedelta(days=1))

    nearest = await stand.service.nearest_shift(STAFF, now=utc(2026, 8, 13, 21, 30))

    assert nearest is not None
    assert nearest.shift_id == tomorrow.id


async def test_nothing_planned_is_an_empty_answer_not_a_failure(stand: Stand) -> None:
    """TZ 8.1: the empty state is the normal one for a venue that just started."""
    assert await stand.service.nearest_shift(STAFF, now=utc(2026, 8, 13, 9, 0)) is None
    assert await stand.service.fortnight(STAFF, now=utc(2026, 8, 13, 9, 0)) == ()
    assert await stand.service.roster(STAFF, DAY) == ()


async def test_the_fortnight_covers_two_weeks_including_today(stand: Stand) -> None:
    for offset in (0, SCHEDULE_HORIZON_DAYS - 1, SCHEDULE_HORIZON_DAYS):
        stand.add(shift_date=DAY + dt.timedelta(days=offset))
    yesterday = stand.add(shift_date=DAY - dt.timedelta(days=1))

    views = await stand.service.fortnight(STAFF, now=utc(2026, 8, 13, 9, 0))

    dates = [view.shift_date for view in views]
    assert dates == [DAY, DAY + dt.timedelta(days=SCHEDULE_HORIZON_DAYS - 1)]
    assert yesterday.shift_date not in dates


async def test_the_next_shift_is_not_capped_by_a_horizon_the_tz_never_set(stand: Stand) -> None:
    """TZ 5.3 asks for "the nearest shift" and names no cut-off, so there is none.

    A schedule imported for the month after next is exactly the case a horizon would break:
    the row is in the table, and the screen has to say so rather than "nothing planned".
    """
    far = stand.add(shift_date=DAY + dt.timedelta(days=90))

    following = await stand.service.next_shift(STAFF, now=utc(2026, 8, 13, 9, 0))

    assert following is not None
    assert following.shift_id == far.id


async def test_the_nearest_of_several_future_shifts_wins(stand: Stand) -> None:
    """The unbounded query still answers with the *next* one, not with the last one found."""
    stand.add(shift_date=DAY + dt.timedelta(days=90))
    soon = stand.add(shift_date=DAY + dt.timedelta(days=2))

    following = await stand.service.next_shift(STAFF, now=utc(2026, 8, 13, 9, 0))

    assert following is not None
    assert following.shift_id == soon.id


async def test_the_roster_of_a_date_is_ordered_by_the_way_the_day_runs(stand: Stand) -> None:
    late = stand.add(user_id=MATE_ID, start_time=dt.time(18, 0), end_time=dt.time(2, 0))
    early = stand.add(user_id=STAFF_ID, start_time=dt.time(8, 0), end_time=dt.time(17, 0))

    roster = await stand.service.roster(STAFF, DAY)

    assert [view.shift_id for view in roster] == [early.id, late.id]
    assert [view.full_name for view in roster] == ["Анна", "Борис"]


# --------------------------------------------------------------------------------------
# One opener and one closer per date, checked in the service (decision D8)
# --------------------------------------------------------------------------------------


async def test_a_second_opener_for_a_date_is_refused(stand: Stand) -> None:
    first = await stand.service.create(
        MANAGER,
        user_id=STAFF_ID,
        shift_date=DAY,
        start_time=DEFAULT_START,
        end_time=DEFAULT_END,
        is_opener=True,
    )
    stand.notifier.saved.clear()

    with pytest.raises(ShiftRoleTakenError) as refusal:
        await stand.service.create(
            MANAGER,
            user_id=MATE_ID,
            shift_date=DAY,
            start_time=dt.time(9, 0),
            end_time=DEFAULT_END,
            is_opener=True,
        )

    assert refusal.value.role is ShiftRole.OPENER
    assert refusal.value.shift_date == DAY
    assert refusal.value.taken_by == first.shift.id
    # The refusal happens before anything is written, and nothing is scheduled for a shift
    # that does not exist.
    assert len(stand.store.rows) == 1
    assert stand.notifier.calls == 0


async def test_a_second_closer_for_a_date_is_refused(stand: Stand) -> None:
    stand.add(is_closer=True)
    second = stand.add(user_id=MATE_ID)

    with pytest.raises(ShiftRoleTakenError) as refusal:
        await stand.service.set_closer(MANAGER, second.id)

    assert refusal.value.role is ShiftRole.CLOSER
    assert second.is_closer is False


async def test_the_opener_of_another_date_is_not_in_the_way(stand: Stand) -> None:
    stand.add(is_opener=True)

    saved = await stand.service.create(
        MANAGER,
        user_id=MATE_ID,
        shift_date=DAY + dt.timedelta(days=1),
        start_time=DEFAULT_START,
        end_time=DEFAULT_END,
        is_opener=True,
    )

    assert saved.shift.is_opener is True


async def test_marking_the_same_shift_opener_twice_is_not_a_conflict(stand: Stand) -> None:
    shift = stand.add(is_opener=True)

    saved = await stand.service.set_opener(MANAGER, shift.id)

    assert saved.shift.is_opener is True


async def test_the_mark_can_be_handed_over(stand: Stand) -> None:
    first = stand.add(is_opener=True)
    second = stand.add(user_id=MATE_ID)

    await stand.service.set_opener(MANAGER, first.id, assigned=False)
    saved = await stand.service.set_opener(MANAGER, second.id)

    assert first.is_opener is False
    assert saved.shift.is_opener is True


async def test_moving_a_shift_to_a_date_that_already_has_an_opener_is_refused(
    stand: Stand,
) -> None:
    stand.add(shift_date=DAY, is_opener=True)
    moving = stand.add(shift_date=DAY + dt.timedelta(days=1), user_id=MATE_ID, is_opener=True)

    with pytest.raises(ShiftRoleTakenError):
        await stand.service.update(MANAGER, moving.id, shift_date=DAY, is_opener=True)

    assert moving.shift_date == DAY + dt.timedelta(days=1)


# --------------------------------------------------------------------------------------
# A date without an opener: saved, with a warning (decision B4)
# --------------------------------------------------------------------------------------


async def test_a_shift_without_an_opener_is_saved_and_reported(stand: Stand) -> None:
    saved = await stand.service.create(
        MANAGER,
        user_id=STAFF_ID,
        shift_date=DAY,
        start_time=DEFAULT_START,
        end_time=DEFAULT_END,
    )

    # Decision B4: the row exists — the warning is a line on the same screen, not a refusal
    # and not a ninth notification type.
    assert saved.shift.id is not None
    assert len(stand.store.rows) == 1
    assert saved.warnings == (ShiftWarning.NO_OPENER, ShiftWarning.NO_CLOSER)
    assert saved.needs_opener is True


async def test_a_date_with_an_opener_only_misses_the_closer(stand: Stand) -> None:
    saved = await stand.service.create(
        MANAGER,
        user_id=STAFF_ID,
        shift_date=DAY,
        start_time=DEFAULT_START,
        end_time=DEFAULT_END,
        is_opener=True,
    )

    assert saved.warnings == (ShiftWarning.NO_CLOSER,)
    assert saved.needs_opener is False


async def test_a_full_roster_warns_about_nothing(stand: Stand) -> None:
    stand.add(is_opener=True)
    closing = stand.add(user_id=MATE_ID)

    saved = await stand.service.set_closer(MANAGER, closing.id)

    assert saved.warnings == ()


async def test_removing_the_opener_warns_about_the_date_that_is_left(stand: Stand) -> None:
    opener = stand.add(is_opener=True, is_closer=True)
    stand.add(user_id=MATE_ID)

    removed = await stand.service.delete(MANAGER, opener.id)

    assert removed.shift.shift_id == opener.id
    assert removed.shift.is_opener is True
    assert removed.warnings == (ShiftWarning.NO_OPENER, ShiftWarning.NO_CLOSER)
    assert removed.needs_opener is True


async def test_an_empty_date_has_no_roster_to_be_incomplete(stand: Stand) -> None:
    only = stand.add()

    removed = await stand.service.delete(MANAGER, only.id)

    assert removed.warnings == ()


async def test_moving_a_shift_away_warns_about_both_dates(stand: Stand) -> None:
    """The old date may have lost its only opener, and the new one may still lack one."""
    moving = stand.add(is_opener=True)
    stand.add(user_id=MATE_ID)

    saved = await stand.service.update(
        MANAGER,
        moving.id,
        shift_date=DAY + dt.timedelta(days=1),
    )

    assert saved.shift.shift_date == DAY + dt.timedelta(days=1)
    assert set(saved.warnings) == {ShiftWarning.NO_OPENER, ShiftWarning.NO_CLOSER}


# --------------------------------------------------------------------------------------
# Every change reaches the scheduler (plan, tasks 16 and 33)
# --------------------------------------------------------------------------------------


async def test_creating_a_shift_schedules_its_notifications(stand: Stand) -> None:
    saved = await stand.service.create(
        MANAGER,
        user_id=STAFF_ID,
        shift_date=DAY,
        start_time=DEFAULT_START,
        end_time=DEFAULT_END,
        is_opener=True,
    )

    assert [shift.id for shift in stand.notifier.saved] == [saved.shift.id]
    assert stand.notifier.removed == []


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param({"start_time": dt.time(9, 0)}, id="window"),
        pytest.param({"user_id": MATE_ID}, id="who works it"),
        pytest.param({"shift_date": dt.date(2026, 8, 20)}, id="date"),
        pytest.param({"is_opener": True}, id="opener"),
        pytest.param({"status": ShiftStatus.CANCELLED}, id="cancelled"),
        pytest.param({}, id="nothing in particular"),
    ],
)
async def test_any_edit_replans(stand: Stand, edit: dict[str, Any]) -> None:
    """Plan, task 16: the service decides nothing about *whether* a row has to move."""
    shift = stand.add()

    await stand.service.update(MANAGER, shift.id, **edit)

    assert [saved.id for saved in stand.notifier.saved] == [shift.id]


async def test_deleting_a_shift_cancels_what_it_had_planned(stand: Stand) -> None:
    shift = stand.add(is_opener=True)

    await stand.service.delete(MANAGER, shift.id)

    assert stand.notifier.removed == [shift.id]
    assert stand.notifier.saved == []
    assert stand.store.rows == []


# --------------------------------------------------------------------------------------
# Rights and venue isolation (TZ 2, 3.3, 9; acceptance 11.3)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    ["create", "update", "delete", "set_opener"],
    ids=["create", "update", "delete", "assign opener"],
)
async def test_staff_may_not_edit_the_schedule(stand: Stand, action: str) -> None:
    shift = stand.add()

    with pytest.raises(PermissionDeniedError) as refusal:
        if action == "create":
            await stand.service.create(
                STAFF,
                user_id=STAFF_ID,
                shift_date=DAY + dt.timedelta(days=1),
                start_time=DEFAULT_START,
                end_time=DEFAULT_END,
            )
        elif action == "update":
            await stand.service.update(STAFF, shift.id, status=ShiftStatus.WORKED)
        elif action == "delete":
            await stand.service.delete(STAFF, shift.id)
        else:
            await stand.service.set_opener(STAFF, shift.id)

    assert refusal.value.required is MemberRole.MANAGER
    assert stand.notifier.calls == 0


async def test_staff_reads_its_own_schedule_and_not_a_colleagues(stand: Stand) -> None:
    mine = stand.add(user_id=STAFF_ID)
    theirs = stand.add(user_id=MATE_ID)

    own = await stand.service.fortnight(STAFF, now=utc(2026, 8, 13, 9, 0))
    assert [view.shift_id for view in own] == [mine.id]

    with pytest.raises(PermissionDeniedError):
        await stand.service.fortnight(STAFF, now=utc(2026, 8, 13, 9, 0), user_id=MATE_ID)
    with pytest.raises(PermissionDeniedError):
        await stand.service.get(STAFF, theirs.id)


async def test_a_manager_reads_anybodys_schedule_in_the_venue(stand: Stand) -> None:
    theirs = stand.add(user_id=MATE_ID)

    views = await stand.service.fortnight(MANAGER, now=utc(2026, 8, 13, 9, 0), user_id=MATE_ID)

    assert [view.shift_id for view in views] == [theirs.id]
    assert await stand.service.get(MANAGER, theirs.id) is not None


async def test_the_roster_of_a_date_is_a_staff_screen(stand: Stand) -> None:
    """TZ 5.3 shows everyone who works today to everyone who works today."""
    mate = stand.add(user_id=MATE_ID)

    roster = await stand.service.roster(MATE, DAY)

    assert mate.id in [view.shift_id for view in roster]


async def test_an_actor_of_another_venue_is_refused_outright(stand: Stand) -> None:
    shift = stand.add()

    with pytest.raises(VenueMismatchError) as refusal:
        await stand.service.get(STRANGER, shift.id)

    assert refusal.value.actor_venue_id == OTHER_VENUE_ID
    assert refusal.value.venue_id == VENUE_ID


async def test_a_shift_of_another_venue_is_not_addressable(stand: Stand) -> None:
    """A forged `callback_data` dies here: the scoped repository never sees the row."""
    foreign = stand.add(venue_id=OTHER_VENUE_ID, user_id=OUTSIDER_ID)

    assert await stand.service.get(MANAGER, foreign.id) is None
    with pytest.raises(ShiftNotFoundError):
        await stand.service.update(MANAGER, foreign.id, status=ShiftStatus.WORKED)
    with pytest.raises(ShiftNotFoundError):
        await stand.service.delete(MANAGER, foreign.id)

    # Untouched, and still perfectly visible to its own venue.
    assert foreign.status is ShiftStatus.PLANNED
    assert await stand.other.get(STRANGER, foreign.id) is not None
    assert stand.notifier.calls == 0


async def test_a_missing_shift_is_a_refusal_not_a_crash(stand: Stand) -> None:
    assert await stand.service.get(MANAGER, 404) is None
    with pytest.raises(ShiftNotFoundError) as refusal:
        await stand.service.delete(MANAGER, 404)
    assert refusal.value.venue_id == VENUE_ID


# --------------------------------------------------------------------------------------
# Only this venue's own people go on this venue's schedule (TZ 3.3, 9; acceptance 11.3)
# --------------------------------------------------------------------------------------


async def test_an_employee_of_another_venue_cannot_be_put_on_the_schedule(stand: Stand) -> None:
    """The leak `shifts.user_id` alone would allow: a foreign key to the *global* `users`.

    Nothing in the schema says the person works here, so the check is the service's. Without
    it the manager of this venue would see a stranger's name on the roster and the opening
    checklist would be pushed to somebody who has never been here.
    """
    with pytest.raises(ShiftUserNotInVenueError) as refusal:
        await stand.service.create(
            MANAGER,
            user_id=OUTSIDER_ID,
            shift_date=DAY,
            start_time=DEFAULT_START,
            end_time=DEFAULT_END,
        )

    assert refusal.value.user_id == OUTSIDER_ID
    assert refusal.value.venue_id == VENUE_ID
    # Refused before the write, so nothing is scheduled for a shift that does not exist.
    assert stand.store.rows == []
    assert stand.notifier.calls == 0


async def test_a_shift_cannot_be_handed_over_to_another_venues_employee(stand: Stand) -> None:
    """The same door from the other side: the replacement scenario of TZ 5.3."""
    shift = stand.add(user_id=STAFF_ID)

    with pytest.raises(ShiftUserNotInVenueError):
        await stand.service.update(MANAGER, shift.id, user_id=OUTSIDER_ID)

    assert shift.user_id == STAFF_ID
    assert stand.notifier.calls == 0


async def test_a_dismissed_employee_is_not_scheduled_either(stand: Stand) -> None:
    """TZ 5.1 keeps their row so the history survives; the row is history, not membership."""
    with pytest.raises(ShiftUserNotInVenueError):
        await stand.service.create(
            MANAGER,
            user_id=DISMISSED_ID,
            shift_date=DAY,
            start_time=DEFAULT_START,
            end_time=DEFAULT_END,
        )

    shift = stand.add(user_id=STAFF_ID)
    with pytest.raises(ShiftUserNotInVenueError):
        await stand.service.update(MANAGER, shift.id, user_id=DISMISSED_ID)

    assert shift.user_id == STAFF_ID


async def test_somebody_who_works_nowhere_is_not_scheduled(stand: Stand) -> None:
    """A `users` row exists the moment a person presses /start; membership does not."""
    with pytest.raises(ShiftUserNotInVenueError):
        await stand.service.create(
            MANAGER,
            user_id=NOBODY_ID,
            shift_date=DAY,
            start_time=DEFAULT_START,
            end_time=DEFAULT_END,
        )


async def test_the_check_is_scoped_not_a_blanket_ban(stand: Stand) -> None:
    """The very person this venue may not roster is the one the other venue rosters daily."""
    saved = await stand.other.create(
        STRANGER,
        user_id=OUTSIDER_ID,
        shift_date=DAY,
        start_time=DEFAULT_START,
        end_time=DEFAULT_END,
    )

    assert saved.shift.user_id == OUTSIDER_ID
    assert saved.shift.venue_id == OTHER_VENUE_ID


async def test_an_edit_that_does_not_touch_the_person_needs_no_lookup(stand: Stand) -> None:
    """`user_id=None` means "leave as it is", so an ordinary edit asks nothing of the roster."""
    shift = stand.add(user_id=STAFF_ID)

    saved = await stand.service.update(MANAGER, shift.id, status=ShiftStatus.WORKED)

    assert saved.shift.status is ShiftStatus.WORKED
    assert saved.shift.user_id == STAFF_ID


async def test_the_roster_of_another_venue_does_not_leak_into_this_one(stand: Stand) -> None:
    stand.add(user_id=STAFF_ID)
    stand.add(venue_id=OTHER_VENUE_ID, user_id=OUTSIDER_ID)

    assert len(await stand.service.roster(MANAGER, DAY)) == 1
    assert len(await stand.other.roster(STRANGER, DAY)) == 1


# --------------------------------------------------------------------------------------
# The fakes themselves, held to the statements they stand for
# --------------------------------------------------------------------------------------
#
# Everything above is only as true as the fakes are: a fake that answers where the real
# `WHERE` matches nothing, or writes where the real `INSERT` refuses, turns a service test
# into a test of the fake — and the difference shows up in production and nowhere else.


async def test_every_read_of_the_fakes_carries_the_venue_predicate(stand: Stand) -> None:
    """Decision D9: `venue_id` is in every `WHERE`, so a foreign id addresses nothing."""
    foreign = stand.add(venue_id=OTHER_VENUE_ID, user_id=OUTSIDER_ID, is_opener=True)
    shifts = FakeShifts(stand.store, VENUE_ID)
    members = FakeMembers(stand.roster, VENUE_ID)

    assert await shifts.get(foreign.id) is None
    assert list(await shifts.list_for_date(DAY)) == []
    assert list(await shifts.list_between(date_from=DAY, date_to=DAY)) == []
    assert await shifts.get_opener(DAY) is None
    assert await shifts.update(foreign.id, status=ShiftStatus.WORKED) is None
    assert await shifts.delete(foreign.id) is False
    assert await members.get_for_user(OUTSIDER_ID) is None

    # Hidden by the predicate, not by an empty table: its own venue sees all of it.
    assert await FakeShifts(stand.store, OTHER_VENUE_ID).get(foreign.id) is foreign
    assert await FakeMembers(stand.roster, OTHER_VENUE_ID).get_for_user(OUTSIDER_ID) is not None
    assert foreign.status is ShiftStatus.PLANNED


async def test_the_fake_names_the_opener_the_database_would_name(stand: Stand) -> None:
    """`_role_holder` is `ORDER BY start_time, id` and takes the first row, not the oldest.

    Two marked rows on one date is a state decision D8 deliberately allows to exist inside
    the stage 1 replacement transaction, and it is the only state where the order shows.
    """
    late = stand.add(start_time=dt.time(18, 0), end_time=dt.time(2, 0), is_opener=True)
    early = stand.add(user_id=MATE_ID, start_time=dt.time(8, 0), is_opener=True)
    shifts = FakeShifts(stand.store, VENUE_ID)

    assert late.id < early.id, "insertion order and the answer really do differ here"
    assert await shifts.get_opener(DAY) is early
    assert [row.id for row in await shifts.list_for_date(DAY)] == [early.id, late.id]


async def test_the_fake_orders_a_range_by_date_before_time(stand: Stand) -> None:
    """`ORDER BY shift_date, start_time, id`, both in `list_for_user` and in `list_between`."""
    tomorrow_early = stand.add(shift_date=DAY + dt.timedelta(days=1), start_time=dt.time(8, 0))
    today_late = stand.add(start_time=dt.time(18, 0), end_time=dt.time(2, 0))
    shifts = FakeShifts(stand.store, VENUE_ID)
    window = {"date_from": DAY, "date_to": DAY + dt.timedelta(days=1)}

    assert [row.id for row in await shifts.list_between(**window)] == [
        today_late.id,
        tomorrow_early.id,
    ]
    assert [row.id for row in await shifts.list_for_user(STAFF_ID, **window)] == [
        today_late.id,
        tomorrow_early.id,
    ]


async def test_the_fake_refuses_to_write_a_shift_without_hours(stand: Stand) -> None:
    """`shifts.hours` is `NOT NULL` and `ShiftRepo.create` passes fields through as they are.

    Computing it here would make the test kinder than the schema: the service is the only
    producer of the column (TZ 4.2), and a service that stopped producing it has to fail.
    """
    shifts = FakeShifts(stand.store, VENUE_ID)

    with pytest.raises(TypeError):
        await shifts.create(
            user_id=STAFF_ID,
            shift_date=DAY,
            start_time=DEFAULT_START,
            end_time=DEFAULT_END,
        )

    assert stand.store.rows == []


async def test_the_fake_refuses_a_column_the_table_does_not_have(stand: Stand) -> None:
    """`ShiftRepo.update` goes through `_apply`: a misspelt field is a typo, not a no-op."""
    shift = stand.add()
    shifts = FakeShifts(stand.store, VENUE_ID)

    with pytest.raises(AttributeError):
        await shifts.update(shift.id, hourz=Decimal("1.00"))

    assert shift.hours == shift_hours(DEFAULT_START, DEFAULT_END)
    assert not hasattr(shift, "hourz")
