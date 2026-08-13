"""The schedule of one venue (plan, task 16; TZ 4.2, 5.3).

Everything the bot knows about "who works when" is decided here: a handler builds no shift
of its own, computes no duration and talks to no scheduler (TZ 3.2).

Four rules shape the module, and each of them is the reason a piece of it exists.

**A shift belongs to the date it starts on (TZ 5.3).** ``18:00-02:00`` is one shift dated
the day it opened, not two halves; ``14:00-00:00`` ends at midnight, which is already the
next calendar day. Neither the service nor its callers do that arithmetic by hand: the pair
of instants a shift spans comes from :func:`src.services.timezones.shift_window`, the single
module allowed to convert between the venue's wall clock (``TIME`` columns) and UTC
(``timestamptz`` columns) — decision D12. Nothing here calls ``datetime.now()``; every
method that needs the current moment takes it as an aware UTC argument, which is also what
makes the midnight cases testable without a clock.

**``hours`` is computed, never typed in (TZ 4.2, plan task 16).** :func:`shift_hours` is the
only producer of that column on the manual path, and it measures the venue's wall clock:
``14:00-00:00`` is ten hours on the night the clocks change too, because ``hours`` is the
number the schedule sheet of the venue holds (TZ 4.2 describes the column as exactly that
cell). The *real* elapsed time of such a night differs, and that is what
:meth:`ShiftService.window` reports — the notification math uses the window, never ``hours``.

**At most one opener and one closer per date, checked here (decision D8).** TZ 4.2 wants
exactly one of each; the schema deliberately carries no unique index, because the shift
replacement transaction of stage 1 would not pass one. So a second opener for a date is
refused by :class:`ShiftRoleTakenError`, and a date left *without* one is saved and reported
back as a :class:`ShiftWarning` — decision B4: the screen says the opening checklist will not
arrive, and no new notification type is invented for it (section 6 of the TZ is a closed
list of eight).

**Only this venue's own people can be put on this venue's schedule (TZ 3.3, TZ 9,
acceptance 11.3).** ``shifts.user_id`` points at the global ``users`` table, so the schema
alone would happily let a manager of one venue roster a bartender of another — and then show
their name and send them a checklist. The membership is therefore checked here, on every
write that names a person: :meth:`ShiftService._require_member` asks the venue-scoped
``venue_members`` repository, and a stranger — or somebody dismissed (TZ 5.1 keeps the row
and drops ``is_active``) — is refused with :class:`ShiftUserNotInVenueError` before anything
is written.

**Every change reschedules the notifications (TZ 6, plan tasks 16 and 33).** Create, update
and delete each end in a call to :class:`ShiftNotifier`, which is the seam to
``notification_service`` (plan, task 19): it owns the queue, the ``dedup_key`` and the
cancellation of what a moved shift left behind. The interface is declared here, deliberately
narrow — this service knows that a shift was saved or removed, and nothing about queues,
lead times or chat ids. Whether a particular edit actually moves a row is the notifier's
judgement: it re-keys by ``shift_id``, so a saved shift replans and a shift that stopped
wanting a checklist has its live rows cancelled.
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Protocol
from zoneinfo import ZoneInfo

from src.db.models import Shift, ShiftSource, ShiftStatus
from src.db.repositories.protocols import (
    ShiftRepository,
    UserRepository,
    VenueMemberRepository,
)
from src.services.access import (
    AccessContext,
    AccessError,
    require_manager,
    require_self_or_manager,
    require_venue,
)
from src.services.audit import SILENT, AuditEntity, AuditTrail, snapshot
from src.services.timezones import ensure_utc, local_date, shift_window, venue_timezone

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: The columns an audit record of a whole shift carries (TZ 2). Spelled out rather than
#: dumped off the row: `created_at`, `created_by` and the primary key are not changes, and
#: a diff that repeats them buries the two fields a manager actually looks for.
_AUDITED_SHIFT_FIELDS: Final = (
    "user_id",
    "shift_date",
    "start_time",
    "end_time",
    "is_opener",
    "is_closer",
    "status",
)

#: TZ 5.3: the schedule screen lists the employee's own shifts two weeks ahead, today
#: included.
SCHEDULE_HORIZON_DAYS = 14

#: A shift that is still running started at most one calendar day ago: the longest window
#: this schema can express is 24 hours (`end_time <= start_time` means "the next day").
_RUNNING_LOOKBACK = dt.timedelta(days=1)

#: `hours` is NUMERIC(5, 2).
_HOURS_STEP = Decimal("0.01")

#: Any date works: :func:`shift_hours` measures a wall clock difference, not an instant.
_WALL_CLOCK_ANCHOR = dt.date(2000, 1, 1)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ShiftError(AccessError):
    """Base class for the refusals of this module.

    Rooted in :class:`src.services.access.AccessError` on purpose, like the roster service:
    a handler catches one family for "the service said no" and renders a text from
    ``src/bot/texts/``, while the error middleware keeps everything else as a bug.
    """


class ShiftNotFoundError(ShiftError):
    """No such shift in the actor's venue.

    A shift of another venue lands here too, and on purpose: the repository is scoped, so a
    forged `callback_data` resolves to nothing rather than to somebody else's schedule
    (TZ 9, acceptance 11.3).
    """

    def __init__(self, shift_id: int, venue_id: int) -> None:
        super().__init__(f"shift {shift_id} does not belong to venue {venue_id}")
        self.shift_id = shift_id
        self.venue_id = venue_id


class ShiftUserNotInVenueError(ShiftError):
    """The person named in a write does not work here (TZ 3.3, TZ 9, acceptance 11.3).

    Raised for somebody who has no ``venue_members`` row in this venue at all, and for
    somebody whose row is there but inactive: TZ 5.1 keeps the row of a dismissed employee
    so their history survives, and that row is history, not membership. Either way the venue
    boundary holds — a schedule of one venue never names a person from another one, and no
    checklist is ever pushed to an outsider.
    """

    def __init__(self, user_id: int, venue_id: int) -> None:
        super().__init__(f"user {user_id} is not an active member of venue {venue_id}")
        self.user_id = user_id
        self.venue_id = venue_id


class ShiftRoleTakenError(ShiftError):
    """A second opener (or closer) for a date, refused in the service (decision D8).

    TZ 4.2: exactly one member of a date's roster opens and exactly one closes. The check
    lives here rather than in a unique index because stage 1 replaces a shift inside one
    transaction, where an index would fire on a state that is legal in the middle of it.
    """

    def __init__(self, role: ShiftRole, shift_date: dt.date, taken_by: int) -> None:
        super().__init__(f"{role.value} of {shift_date.isoformat()} is already shift {taken_by}")
        self.role = role
        self.shift_date = shift_date
        self.taken_by = taken_by


# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------


class ShiftRole(enum.StrEnum):
    """The two marks a date's roster carries (TZ 4.2)."""

    OPENER = "opener"
    CLOSER = "closer"


class ShiftWarning(enum.StrEnum):
    """What the manager is told on the same screen after saving (decision B4).

    Not a notification: section 6 of the TZ lists eight types and neither of these is one of
    them. The wording lives in ``src/bot/texts/``; this is the fact, not the sentence.
    """

    #: Nobody opens this date — the opening checklist will not be sent (TZ 5.4, B4).
    NO_OPENER = "no_opener"
    #: Nobody closes this date (TZ 4.2 asks for exactly one).
    NO_CLOSER = "no_closer"


# --------------------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShiftSnapshot:
    """An immutable copy of the fields a screen or a rule may need after the row is gone.

    A copy rather than the row itself, for two reasons: an update mutates the ORM object in
    place, so "what the shift looked like before" has to be taken off it beforehand, and a
    delete leaves no row at all — yet the screen still has to say which shift went away and
    which date lost its opener (decision B4).
    """

    shift_id: int
    venue_id: int
    user_id: int
    shift_date: dt.date
    start_time: dt.time
    end_time: dt.time
    is_opener: bool
    is_closer: bool
    status: ShiftStatus

    @classmethod
    def of(cls, shift: Shift) -> ShiftSnapshot:
        return cls(
            shift_id=shift.id,
            venue_id=shift.venue_id,
            user_id=shift.user_id,
            shift_date=shift.shift_date,
            start_time=shift.start_time,
            end_time=shift.end_time,
            is_opener=bool(shift.is_opener),
            is_closer=bool(shift.is_closer),
            status=shift.status,
        )


@dataclass(frozen=True, slots=True)
class ShiftView:
    """One line of a schedule screen: the row, who works it, and when it really runs.

    ``starts_at`` and ``ends_at`` are instants in UTC; the ``TIME`` columns behind them are
    venue-local wall clock readings and mean nothing without the venue's zone (D12).
    """

    shift: Shift
    full_name: str
    starts_at: dt.datetime
    ends_at: dt.datetime

    @property
    def shift_id(self) -> int:
        return self.shift.id

    @property
    def user_id(self) -> int:
        return self.shift.user_id

    @property
    def shift_date(self) -> dt.date:
        """The date the shift starts on — a night shift is dated by it (TZ 5.3)."""
        return self.shift.shift_date

    @property
    def start_time(self) -> dt.time:
        return self.shift.start_time

    @property
    def end_time(self) -> dt.time:
        return self.shift.end_time

    @property
    def hours(self) -> Decimal:
        return self.shift.hours

    @property
    def is_opener(self) -> bool:
        return bool(self.shift.is_opener)

    @property
    def is_closer(self) -> bool:
        return bool(self.shift.is_closer)

    @property
    def status(self) -> ShiftStatus:
        return self.shift.status

    @property
    def crosses_midnight(self) -> bool:
        """True when the shift ends on the next local day, midnight included."""
        return self.end_time <= self.start_time

    def covers(self, moment: dt.datetime) -> bool:
        """Is the shift running at `moment`? Half-open: the end instant is already outside."""
        return self.starts_at <= ensure_utc(moment) < self.ends_at

    def starts_after(self, moment: dt.datetime) -> bool:
        return self.starts_at > ensure_utc(moment)


@dataclass(frozen=True, slots=True)
class ShiftSaved:
    """Outcome of a create or an update: the row, plus what the screen has to mention."""

    view: ShiftView
    warnings: tuple[ShiftWarning, ...] = ()

    @property
    def shift(self) -> Shift:
        return self.view.shift

    @property
    def needs_opener(self) -> bool:
        """Decision B4: saved anyway, and the caller says the checklist will not arrive."""
        return ShiftWarning.NO_OPENER in self.warnings


@dataclass(frozen=True, slots=True)
class ShiftRemoved:
    """Outcome of a delete: what went away, and what the date is now missing."""

    shift: ShiftSnapshot
    warnings: tuple[ShiftWarning, ...] = ()

    @property
    def needs_opener(self) -> bool:
        return ShiftWarning.NO_OPENER in self.warnings


# --------------------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------------------


class ShiftNotifier(Protocol):
    """The scheduling side of the world, as this service needs it (TZ 6, plan task 33).

    Implemented by ``NotificationService`` (plan, task 19), which owns the type registry,
    the ``dedup_key`` and the worker. Every mutation below calls exactly one of these two
    methods, so "a shift changed but its notifications did not" is not a state this module
    can produce.

    Two methods rather than create/update/delete on purpose. Planning is keyed by
    ``shift_id``: an upsert on the ``dedup_key`` moves the row a create would have inserted,
    so "saved" is one case, and the notifier needs no *before* picture to clean up after an
    edit. The return values are ignored here — they are typed ``object`` so that an
    implementation may report what it did (``NotificationService`` returns a
    ``ScheduleResult`` and a count) without this module knowing what a queue row is.
    """

    async def shift_saved(self, shift: Shift) -> object: ...

    async def shift_removed(self, shift_id: int) -> object: ...


# --------------------------------------------------------------------------------------
# Duration
# --------------------------------------------------------------------------------------


def shift_hours(start_time: dt.time, end_time: dt.time) -> Decimal:
    """Length of a shift on the venue's wall clock, in hours (TZ 4.2).

    The same convention as :func:`src.services.timezones.shift_window`: an end that is not
    later than the start belongs to the next day, so ``14:00-00:00`` is ten hours and
    ``18:00-02:00`` is eight. A shift whose start equals its end is therefore a full day,
    which is the only reading consistent with the window.

    Wall clock, not elapsed time: on the night a venue's clocks move, the sheet the manager
    keeps still says ten, and TZ 4.2 defines this column as that cell. The hours actually
    spanned are :meth:`ShiftService.window`, and that is what the scheduler uses.
    """
    start = dt.datetime.combine(_WALL_CLOCK_ANCHOR, start_time)
    end = dt.datetime.combine(_WALL_CLOCK_ANCHOR, end_time)
    if end <= start:
        end += dt.timedelta(days=1)
    seconds = Decimal((end - start).total_seconds())
    return (seconds / Decimal(3600)).quantize(_HOURS_STEP, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------


class ShiftService:
    """Business logic of TZ 4.2 and 5.3. Holds no Telegram types and no session of its own.

    Scoped to one venue, like the repositories it is built on (TZ 3.3): `venue_id` is part
    of the object, and every entry point checks that the actor belongs to that venue before
    anything else happens (TZ 9 — rights are verified on the server on every action).
    """

    def __init__(
        self,
        *,
        venue_id: int,
        timezone: str | ZoneInfo,
        shifts: ShiftRepository,
        users: UserRepository,
        members: VenueMemberRepository,
        notifier: ShiftNotifier,
        audit: AuditTrail = SILENT,
    ) -> None:
        self._venue_id = venue_id
        self._tz = timezone if isinstance(timezone, ZoneInfo) else venue_timezone(timezone)
        self._shifts = shifts
        self._users = users
        # Venue-scoped, like `shifts`: "does this person work here" is a question about
        # `self._venue_id` and about nothing else (TZ 3.3). `users` stays global because
        # `users.full_name` is a global column and a person may work in several venues.
        self._members = members
        self._notifier = notifier
        # TZ 2: who put whom on which shift. Defaults to silence so a test may build the
        # service with nothing behind it; the composition root always hands in a real one.
        self._audit = audit

    @property
    def venue_id(self) -> int:
        return self._venue_id

    @property
    def timezone(self) -> ZoneInfo:
        """The venue's own zone; every conversion below goes through it (TZ 3.4)."""
        return self._tz

    def window(self, shift: Shift) -> tuple[dt.datetime, dt.datetime]:
        """The two instants a shift spans, in UTC (TZ 5.3, D12).

        This is the only correct way to ask when a shift runs: ``14:00-00:00`` ends at
        midnight of the *next* day, and a shift crossing a daylight saving transition spans
        the hours the clock actually ran, not the ones the arithmetic suggests.
        """
        return shift_window(shift.shift_date, shift.start_time, shift.end_time, self._tz)

    # -- reading ------------------------------------------------------------------------

    async def get(self, actor: AccessContext, shift_id: int) -> ShiftView | None:
        """One shift, or `None` when the id belongs to another venue (TZ 9)."""
        require_venue(actor, self._venue_id)
        shift = await self._shifts.get(shift_id)
        if shift is None:
            return None
        require_venue(actor, shift.venue_id)
        require_self_or_manager(actor, shift.user_id)
        return await self._view(shift)

    async def roster(self, actor: AccessContext, shift_date: dt.date) -> tuple[ShiftView, ...]:
        """Who works a given date, in the order the day runs.

        Readable by `staff`: TZ 5.3 shows the composition of the shift and the team schedule
        to everyone, names and times only. Editing it is another matter and needs a manager.
        """
        require_venue(actor, self._venue_id)
        return await self._views(await self._shifts.list_for_date(shift_date))

    async def fortnight(
        self,
        actor: AccessContext,
        *,
        now: dt.datetime,
        user_id: int | None = None,
    ) -> tuple[ShiftView, ...]:
        """The next two weeks of one person's schedule, today included (TZ 5.3)."""
        target = self._target_user(actor, user_id)
        today = local_date(ensure_utc(now), self._tz)
        return await self._between(
            target,
            today,
            today + dt.timedelta(days=SCHEDULE_HORIZON_DAYS - 1),
        )

    async def current_shift(
        self,
        actor: AccessContext,
        *,
        now: dt.datetime,
        user_id: int | None = None,
    ) -> ShiftView | None:
        """The shift running at `now`, by window rather than by date (plan, task 23).

        The lookback of one day is what makes a night shift work: at 00:30 the shift a
        bartender is standing in started yesterday, and a query on today's date would miss
        it and offer them tomorrow instead.
        """
        moment = ensure_utc(now)
        target = self._target_user(actor, user_id)
        today = local_date(moment, self._tz)
        views = await self._between(target, today - _RUNNING_LOOKBACK, today)
        return next((view for view in views if view.covers(moment)), None)

    async def next_shift(
        self,
        actor: AccessContext,
        *,
        now: dt.datetime,
        user_id: int | None = None,
    ) -> ShiftView | None:
        """The nearest shift that has not started yet (TZ 5.3: the shift screen).

        Unbounded ahead on purpose. TZ 5.3 asks for "the nearest shift" and names no horizon;
        a cut-off would be a rule the venue never agreed to, and the day it bit — a schedule
        imported early, a single shift planned for next season — the screen would say "nothing
        planned" while the row sits in the table. ``date.max`` is the end of the ``DATE``
        domain, not a business constant: it says "no upper bound" in the vocabulary the
        repository protocol offers.
        """
        moment = ensure_utc(now)
        target = self._target_user(actor, user_id)
        today = local_date(moment, self._tz)
        views = await self._between(target, today, dt.date.max)
        return next((view for view in views if view.starts_after(moment)), None)

    async def nearest_shift(
        self,
        actor: AccessContext,
        *,
        now: dt.datetime,
        user_id: int | None = None,
    ) -> ShiftView | None:
        """What the "my shift" screen shows: the one being worked, else the next (task 23).

        Deliberately in this order. A person who opens the bot in the middle of their shift
        is asking about *this* one — that is where the checklist of the running shift is
        reopened from (TZ 5.4) — and only someone off duty is asking about the next.
        """
        running = await self.current_shift(actor, now=now, user_id=user_id)
        if running is not None:
            return running
        return await self.next_shift(actor, now=now, user_id=user_id)

    # -- writing ------------------------------------------------------------------------

    async def create(
        self,
        actor: AccessContext,
        *,
        user_id: int,
        shift_date: dt.date,
        start_time: dt.time,
        end_time: dt.time,
        is_opener: bool = False,
        is_closer: bool = False,
        status: ShiftStatus = ShiftStatus.PLANNED,
        source: ShiftSource = ShiftSource.MANUAL,
    ) -> ShiftSaved:
        """Put one shift on the schedule (TZ 5.3, plan task 29).

        `hours` is not a parameter: it is computed from the window (TZ 4.2). A date that
        ends up without an opener is still saved, and says so in `warnings` (decision B4).
        The person has to work here: `user_id` arrives from a keyboard and means nothing to
        the schema, which only knows it is some row of the global `users` (TZ 3.3, 11.3).
        """
        require_venue(actor, self._venue_id)
        require_manager(actor)
        await self._require_member(user_id)
        if is_opener:
            await self._require_role_free(ShiftRole.OPENER, shift_date, keeping=None)
        if is_closer:
            await self._require_role_free(ShiftRole.CLOSER, shift_date, keeping=None)

        shift = await self._shifts.create(
            user_id=user_id,
            shift_date=shift_date,
            start_time=start_time,
            end_time=end_time,
            hours=shift_hours(start_time, end_time),
            is_opener=is_opener,
            is_closer=is_closer,
            status=status,
            source=source,
            created_by=actor.user_id,
        )
        await self._notifier.shift_saved(shift)
        await self._audit.created(
            actor,
            AuditEntity.SHIFT,
            shift.id,
            after=snapshot(shift, *_AUDITED_SHIFT_FIELDS),
        )
        return ShiftSaved(
            view=await self._view(shift),
            warnings=await self._warnings(shift.shift_date),
        )

    async def update(
        self,
        actor: AccessContext,
        shift_id: int,
        *,
        user_id: int | None = None,
        shift_date: dt.date | None = None,
        start_time: dt.time | None = None,
        end_time: dt.time | None = None,
        is_opener: bool | None = None,
        is_closer: bool | None = None,
        status: ShiftStatus | None = None,
    ) -> ShiftSaved:
        """Change a shift. `None` means "leave as it is" — none of these columns is nullable.

        `hours` is absent from the signature by design and recomputed whenever the window
        moves, so the two can never disagree (TZ 4.2, plan task 16).

        Handing the shift to somebody else is the replacement scenario of TZ 5.3, and it is
        checked exactly like a create: the new `user_id` must belong to this venue, or the
        row would carry a stranger away with its notifications (TZ 3.3, 11.3).
        """
        require_venue(actor, self._venue_id)
        require_manager(actor)
        current = await self._require_shift(actor, shift_id)
        before = ShiftSnapshot.of(current)
        if user_id is not None:
            await self._require_member(user_id)

        target_date = shift_date if shift_date is not None else before.shift_date
        if is_opener:
            await self._require_role_free(ShiftRole.OPENER, target_date, keeping=shift_id)
        if is_closer:
            await self._require_role_free(ShiftRole.CLOSER, target_date, keeping=shift_id)

        fields: dict[str, object] = {}
        if user_id is not None:
            fields["user_id"] = user_id
        if shift_date is not None:
            fields["shift_date"] = shift_date
        if start_time is not None:
            fields["start_time"] = start_time
        if end_time is not None:
            fields["end_time"] = end_time
        if is_opener is not None:
            fields["is_opener"] = is_opener
        if is_closer is not None:
            fields["is_closer"] = is_closer
        if status is not None:
            fields["status"] = status
        if start_time is not None or end_time is not None:
            fields["hours"] = shift_hours(
                start_time if start_time is not None else before.start_time,
                end_time if end_time is not None else before.end_time,
            )

        updated = await self._shifts.update(shift_id, **fields)
        if updated is None:
            raise ShiftNotFoundError(shift_id, self._venue_id)

        # Plan, task 16: *any* edit goes through the scheduler. Working out whether this
        # particular one moves a notification is the notifier's job, not the caller's.
        await self._notifier.shift_saved(updated)
        await self._audit.updated(
            actor,
            AuditEntity.SHIFT,
            updated.id,
            before=snapshot(current, *fields),
            after=dict(fields),
        )

        warned_date = updated.shift_date
        warnings = await self._warnings(warned_date)
        if before.shift_date != warned_date:
            # The shift moved away from its old date, which may now have no opener either.
            warnings = _merge(warnings, await self._warnings(before.shift_date))
        return ShiftSaved(view=await self._view(updated), warnings=warnings)

    async def set_opener(
        self,
        actor: AccessContext,
        shift_id: int,
        *,
        assigned: bool = True,
    ) -> ShiftSaved:
        """Mark (or unmark) who opens the date of this shift (TZ 4.2, 5.3)."""
        return await self.update(actor, shift_id, is_opener=assigned)

    async def set_closer(
        self,
        actor: AccessContext,
        shift_id: int,
        *,
        assigned: bool = True,
    ) -> ShiftSaved:
        """Mark (or unmark) who closes the date of this shift (TZ 4.2, 5.3)."""
        return await self.update(actor, shift_id, is_closer=assigned)

    async def delete(self, actor: AccessContext, shift_id: int) -> ShiftRemoved:
        """Remove a shift and let the scheduler drop what it had planned (TZ 6, task 33)."""
        require_venue(actor, self._venue_id)
        require_manager(actor)
        current = await self._require_shift(actor, shift_id)
        removed = ShiftSnapshot.of(current)

        if not await self._shifts.delete(shift_id):
            raise ShiftNotFoundError(shift_id, self._venue_id)
        await self._notifier.shift_removed(removed.shift_id)
        await self._audit.deleted(
            actor,
            AuditEntity.SHIFT,
            removed.shift_id,
            before=snapshot(current, *_AUDITED_SHIFT_FIELDS),
        )
        return ShiftRemoved(shift=removed, warnings=await self._warnings(removed.shift_date))

    # -- internals ----------------------------------------------------------------------

    def _target_user(self, actor: AccessContext, user_id: int | None) -> int:
        """Whose schedule is being read: your own by default, anyone else's needs a manager.

        TZ 2: `staff` sees only its own data. The composition of a date is the exception the
        TZ makes itself (5.3), and it goes through :meth:`roster`, not through here.
        """
        require_venue(actor, self._venue_id)
        target = actor.user_id if user_id is None else user_id
        require_self_or_manager(actor, target)
        return target

    async def _require_member(self, user_id: int) -> None:
        """Refuse a person who does not work here (TZ 3.3, TZ 9, acceptance 11.3).

        The repository is scoped to `self._venue_id`, so a member of another venue comes back
        as `None` exactly like somebody who exists nowhere — the same shape
        :meth:`src.services.access.AccessService.select_venue` relies on. An inactive row is
        refused too: a dismissed employee keeps their history (TZ 5.1) but not their place in
        next week's schedule.
        """
        member = await self._members.get_for_user(user_id)
        if member is None or not member.is_active:
            raise ShiftUserNotInVenueError(user_id, self._venue_id)

    async def _require_shift(self, actor: AccessContext, shift_id: int) -> Shift:
        shift = await self._shifts.get(shift_id)
        if shift is None:
            raise ShiftNotFoundError(shift_id, self._venue_id)
        # The repository is venue-scoped, so this cannot normally fire; it is the second
        # lock TZ 9 asks for on an id that arrived from `callback_data`.
        require_venue(actor, shift.venue_id)
        return shift

    async def _require_role_free(
        self,
        role: ShiftRole,
        shift_date: dt.date,
        *,
        keeping: int | None,
    ) -> None:
        """Decision D8: at most one opener and one closer per date, refused here."""
        if role is ShiftRole.OPENER:
            taken = await self._shifts.get_opener(shift_date)
        else:
            taken = await self._shifts.get_closer(shift_date)
        if taken is not None and taken.id != keeping:
            raise ShiftRoleTakenError(role, shift_date, taken.id)

    async def _warnings(self, shift_date: dt.date) -> tuple[ShiftWarning, ...]:
        """What the date is missing after the write (decision B4).

        The question is asked of the date, not of the saved row: a shift without the opener
        mark is perfectly normal as long as somebody else on that date carries it. An empty
        date warns about nothing — there is no roster left to be incomplete.
        """
        roster = await self._shifts.list_for_date(shift_date)
        if not roster:
            return ()
        warnings: list[ShiftWarning] = []
        if not any(shift.is_opener for shift in roster):
            warnings.append(ShiftWarning.NO_OPENER)
        if not any(shift.is_closer for shift in roster):
            warnings.append(ShiftWarning.NO_CLOSER)
        return tuple(warnings)

    async def _between(
        self,
        user_id: int,
        date_from: dt.date,
        date_to: dt.date,
    ) -> tuple[ShiftView, ...]:
        return await self._views(
            await self._shifts.list_for_user(user_id, date_from=date_from, date_to=date_to)
        )

    async def _view(self, shift: Shift) -> ShiftView:
        return (await self._views((shift,)))[0]

    async def _views(self, shifts: Iterable[Shift]) -> tuple[ShiftView, ...]:
        """Attach the person and the real window to every row, in the order a day runs.

        Names come from the repository rather than from `shift.user`: the relationship is
        lazy, and touching it inside an async session is a `MissingGreenlet` waiting to
        happen. One lookup per person, cached across the list.
        """
        names: dict[int, str] = {}
        views: list[ShiftView] = []
        for shift in shifts:
            if shift.user_id not in names:
                user = await self._users.get(shift.user_id)
                names[shift.user_id] = user.full_name if user is not None else ""
            starts_at, ends_at = self.window(shift)
            views.append(
                ShiftView(
                    shift=shift,
                    full_name=names[shift.user_id],
                    starts_at=starts_at,
                    ends_at=ends_at,
                )
            )
        views.sort(key=_view_order)
        return tuple(views)


def _view_order(view: ShiftView) -> tuple[dt.datetime, str, int]:
    return (view.starts_at, view.full_name.casefold(), view.shift_id)


def _merge(
    first: Sequence[ShiftWarning],
    second: Sequence[ShiftWarning],
) -> tuple[ShiftWarning, ...]:
    """Union of two warning sets, keeping the declaration order of the enum."""
    seen = set(first) | set(second)
    return tuple(warning for warning in ShiftWarning if warning in seen)


__all__ = [
    "SCHEDULE_HORIZON_DAYS",
    "ShiftError",
    "ShiftNotFoundError",
    "ShiftNotifier",
    "ShiftRemoved",
    "ShiftRole",
    "ShiftRoleTakenError",
    "ShiftSaved",
    "ShiftService",
    "ShiftSnapshot",
    "ShiftUserNotInVenueError",
    "ShiftView",
    "ShiftWarning",
    "shift_hours",
]
