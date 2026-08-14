"""The manager's schedule: shifts, opener, closer (TZ 5.3, 5.8; plan task 29).

Owner: plan task 29. Four screens and one wizard. The venue's next two weeks by day, with a
button per shift; the wizard that adds one — who, when, how long; the card of a single
shift, where the two marks of TZ 4.2 are set and where a shift is removed; and the empty
state that starts all of it.

Every handler below is the shape TZ 3.2 requires — take the update, call a service, render
the screen. The schedule itself comes from `src/services/shifts.py`, the list of who may be
put on it from `src/services/members.py`, the venue's default shift window from
`src/services/venues.py`, the wording from `src/bot/texts/admin.py` and the screens from
`src/bot/views/schedule.py`. **The scheduler is not touched here at all**: moving a shift
moves the notifications that hang off it, and `ShiftService` is what calls
`NotificationService` (TZ 6, plan tasks 16 and 33). A handler that rescheduled anything
would be a second copy of that rule, and the copy that fell behind would be the one the
bartender's phone obeys.

Four decisions are this module's own.

**Decision B4 is rendered, never enforced.** A shift on a date nobody opens is *saved*, and
`ShiftSaved.warnings` is what says so; this module prints those warnings and invents none of
its own. Whether a date is complete is a question about the whole roster of that date and it
is answered in the service, which is the only place that can see the roster.

**A refusal of TZ 4.2 is a sentence, not a traceback.** A second opener for a date raises
:class:`~src.services.shifts.ShiftRoleTakenError`, which carries the *shift* that already
holds the mark; the name printed next to `SCHEDULE_OPENER_TAKEN_TEMPLATE` is read back
through `ShiftService.get`, so the venue predicate that fetched it is the same one every
other read goes through (TZ 3.3, 9). Every other `ShiftError` is the flat refusal of TZ 9 —
a manager who forged an id learns nothing from it.

**"Now" comes from `src/services/timezones.py` and the fortnight is walked day by day.**
`ShiftService.fortnight` answers for *one person* (TZ 5.3 is the employee's screen); the
manager's screen is the whole venue, and the only venue-wide reader the service offers is
`roster(date)`. So :func:`_fortnight` asks it once per day of `SCHEDULE_HORIZON_DAYS`,
starting from the venue's own today. That is fourteen queries where one would do, and it is
reported as a gap in the service layer rather than fixed here with SQL.

**The wizard's steps are told apart by the FSM state, not by the payload.** The two presses
of the wizard both travel on :class:`~src.bot.callbacks.ShiftMember` — the person step
because that is what the factory means, the "use the venue's default window" button because
what it has to say is *whose* shift is about to be saved. `src/bot/keyboards/schedule.py`
documents why a new factory was not declared, and the entry button's stopgap payload with
it. A press that arrives in neither state is answered as the stale screen it is (TZ 9).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot.callbacks import (
    AdminSection,
    OpenAdmin,
    ShiftCloser,
    ShiftDelete,
    ShiftMember,
    ShiftOpener,
    ShiftShow,
    TimezoneChoice,
)
from src.bot.handlers.admin import Event, deliver, refuse
from src.bot.handlers.admin_venue import window_in
from src.bot.keyboards.schedule import ScheduleCommand
from src.bot.states import ShiftWizard
from src.bot.views import Screen
from src.bot.views import schedule as screens
from src.services.access import AccessContext
from src.services.members import RosterEntry
from src.services.shifts import (
    SCHEDULE_HORIZON_DAYS,
    ShiftError,
    ShiftRemoved,
    ShiftRole,
    ShiftRoleTakenError,
    ShiftSaved,
    ShiftView,
    ShiftWarning,
)
from src.services.timezones import local_date, utc_now
from src.services.venues import VenueConfiguration

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "admin_schedule"

#: Keys of `state.get_data()`, named after the steps of `ShiftWizard` that fill them.
MEMBER_FIELD: Final = "member_id"
DATE_FIELD: Final = "shift_date"

#: A date as a manager types it: `14.08`, `14.8.2026`, `14/08`. The year is optional — the
#: schedule is two weeks long and nobody writes it (TZ 5.3).
_DATE: Final = re.compile(r"(\d{1,2})\s*[.\-/]\s*(\d{1,2})(?:\s*[.\-/]\s*(\d{2,4}))?")

#: A two-digit year is this century. `26` in a shift date is 2026 and never 1926.
_CENTURY: Final = 2000


class Schedule(Protocol):
    """What this module asks of `ShiftService` (TZ 5.3, 5.8), and no more.

    Narrower than the service on purpose, the way `admin_staff.Roster` is: the schedule of a
    venue is what these screens are about, and a test that had to build a real `ShiftService`
    would have to build a session to build four repositories to build it.
    `tests/bot/test_admin_schedule.py` checks with mypy that the real service still satisfies
    this.
    """

    @property
    def timezone(self) -> ZoneInfo: ...

    async def get(self, actor: AccessContext, shift_id: int) -> ShiftView | None: ...

    async def roster(self, actor: AccessContext, shift_date: dt.date) -> tuple[ShiftView, ...]: ...

    async def create(
        self,
        actor: AccessContext,
        *,
        user_id: int,
        shift_date: dt.date,
        start_time: dt.time,
        end_time: dt.time,
    ) -> ShiftSaved: ...

    async def set_opener(
        self, actor: AccessContext, shift_id: int, *, assigned: bool = True
    ) -> ShiftSaved: ...

    async def set_closer(
        self, actor: AccessContext, shift_id: int, *, assigned: bool = True
    ) -> ShiftSaved: ...

    async def delete(self, actor: AccessContext, shift_id: int) -> ShiftRemoved: ...


class Assignable(Protocol):
    """The half of `MemberService` that says who may be put on a shift (TZ 5.8, 3.3)."""

    async def list_assignable(self, actor: AccessContext) -> tuple[RosterEntry, ...]: ...

    async def get(self, actor: AccessContext, member_id: int) -> RosterEntry | None: ...


class ScheduleServices(Protocol):
    """The two services of `data["services"]` these screens use."""

    @property
    def shifts(self) -> Schedule: ...

    @property
    def members(self) -> Assignable: ...


class Settings(Protocol):
    """`VenueService`, for the one row this block reads: the default shift window.

    `update_settings` with nothing to update **is** the read — it checks the rights, fetches
    both rows and writes neither a column nor an `audit_log` record, because the trail is
    built from a diff and there is none. `src/bot/handlers/admin_venue.py` documents the same
    trick and reports the same gap: `VenueService` wants a `configuration(actor)` of its own,
    and until it has one this is the only way to read `venue_settings` without reaching for a
    repository (TZ 3.2).
    """

    async def update_settings(self, actor: AccessContext, **fields: Any) -> VenueConfiguration: ...


# --------------------------------------------------------------------------------------
# Reading what a manager typed
# --------------------------------------------------------------------------------------


def date_in(typed: str | None, today: dt.date) -> dt.date | None:
    """A shift date as a manager writes it, or `None` when it is not a date.

    The year is worked out rather than demanded: `SCHEDULE_PICK_DATE` asks for a date and the
    screen it fills is two weeks long, so `31.12` typed on the second of January means this
    year and `02.01` typed on the thirty-first means the next one. The rule is "the nearest
    such date that is not in the past", which is the only reading under which a manager
    planning tomorrow never has to write a year.

    `29.02` of a year that has no twenty-ninth of February is refused rather than rounded:
    the manager meant a day, and the bot guessing which one is how somebody ends up rostered
    on the wrong one (TZ 8.2).
    """
    found = _DATE.search(typed or "")
    if found is None:
        return None
    day, month, year = found.groups()
    if year is not None:
        return _date_or_none(_full_year(int(year)), int(month), int(day))
    this_year = _date_or_none(today.year, int(month), int(day))
    if this_year is not None and this_year >= today:
        return this_year
    return _date_or_none(today.year + 1, int(month), int(day))


def _full_year(typed: int) -> int:
    return typed + _CENTURY if typed < 100 else typed


def _date_or_none(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        # `32.13`, or the twenty-ninth of a February that does not have one.
        return None


# --------------------------------------------------------------------------------------
# The fortnight (TZ 5.3, 5.8)
# --------------------------------------------------------------------------------------


async def open_schedule(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    services: ScheduleServices,
) -> None:
    """The venue's two weeks, empty state included (TZ 5.8, 8.1).

    The state is dropped first: this screen is the section's board, and arriving at it from
    a shift that was just saved — or from a menu press that interrupted the wizard — must not
    leave a half-filled scenario waiting for the manager's next line.
    """
    await state.clear()
    await deliver(callback, await _board(actor, services), bot=bot)


async def show_shift(
    callback: CallbackQuery,
    bot: Bot,
    actor: AccessContext,
    services: ScheduleServices,
    callback_payload: ShiftShow,
) -> None:
    """One shift's card.

    The resolver has already fetched the row through the venue's repository and refused a
    foreign id (TZ 9); the service is asked again because the card shows the person's *name*,
    which lives on the global `users` table and is not on that row.
    """
    view = await services.shifts.get(actor, callback_payload.shift_id)
    if view is None:
        await refuse(callback)
        return
    await deliver(callback, screens.shift_screen(view), bot=bot)


# --------------------------------------------------------------------------------------
# The wizard (TZ 5.3, 5.8)
# --------------------------------------------------------------------------------------


async def start_wizard(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    services: ScheduleServices,
) -> None:
    """`SCHEDULE_ADD_BUTTON`: step 1, who works it (TZ 5.8).

    The role is checked here because it has to be. This press travels on `TimezoneChoice`
    (see `src/bot/keyboards/schedule.py`), whose resolver rule is `needs_actor=False` — it
    has to be, since the create-venue wizard uses the same factory before any membership
    exists — so nothing upstream has asked whether the presser may manage this venue (TZ 9).

    Nobody to roster is not an error and does not start a scenario: the screen says so and
    offers the employees section, and no state is set, so the next line typed is not read
    as an answer to a question that was never asked (TZ 8.1).
    """
    if not actor.is_manager:
        await refuse(callback)
        return
    entries = await services.members.list_assignable(actor)
    if entries:
        await state.set_state(ShiftWizard.person)
        await state.set_data({})
    await deliver(callback, screens.person_step(entries), bot=bot)


async def pick_person(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    services: ScheduleServices,
    callback_payload: ShiftMember,
) -> None:
    """Step 1 answered: remember who, and ask for the date.

    The membership is read back rather than trusted: the resolver proved the row belongs to
    this venue, and the service adds the half a payload cannot carry — the `users.id` a shift
    is written against, and whether the person still works here at all (TZ 5.1 keeps the row
    of somebody dismissed, and that row is history, not a candidate).
    """
    entry = await services.members.get(actor, callback_payload.member_id)
    if entry is None or not entry.is_active:
        await refuse(callback)
        return
    await state.update_data({MEMBER_FIELD: entry.member_id})
    await state.set_state(ShiftWizard.date)
    await deliver(callback, screens.date_step(), bot=bot)


async def take_date(
    message: Message,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    services: ScheduleServices,
    venue_wizard: Settings,
) -> None:
    """Step 2: the date, typed. Anything unreadable re-asks instead of guessing.

    "Today" is the venue's own calendar day and comes from `src/services/timezones.py`, never
    from `datetime.now()`: at half past midnight in Vladivostok the UTC date is still
    yesterday, and a bot that inferred the year off it would put a New Year's Eve shift a
    year out (TZ 3.4, decision D12).
    """
    if not actor.is_manager:
        await state.clear()
        await refuse(message)
        return
    member_id = _remembered_member(await state.get_data())
    if member_id is None:
        await _restart(message, bot, state, actor, services)
        return
    chosen = date_in(message.text, local_date(utc_now(), services.shifts.timezone))
    if chosen is None:
        await deliver(message, screens.date_step(), bot=bot)
        return
    await state.update_data({DATE_FIELD: chosen.isoformat()})
    await state.set_state(ShiftWizard.window)
    start, end = await _default_window(venue_wizard, actor)
    await deliver(
        message,
        screens.window_step(member_id=member_id, start=start, end=end),
        bot=bot,
    )


async def take_window(
    message: Message,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    services: ScheduleServices,
    venue_wizard: Settings,
) -> None:
    """Step 3 typed: two wall clock readings, and the shift is written.

    Half a window is not a window, and neither is a line with one reading on it —
    `window_in` wants two and re-asks otherwise, with the venue's default still one tap away
    rather than a bare apology (TZ 8.2).
    """
    if not actor.is_manager:
        await state.clear()
        await refuse(message)
        return
    window = window_in(message.text)
    member_id = _remembered_member(await state.get_data())
    if member_id is None:
        await _restart(message, bot, state, actor, services)
        return
    if window is None:
        start, end = await _default_window(venue_wizard, actor)
        await deliver(
            message,
            screens.window_step(member_id=member_id, start=start, end=end),
            bot=bot,
        )
        return
    await _create(message, bot, state, actor, services, member_id=member_id, window=window)


async def use_default_window(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    services: ScheduleServices,
    venue_wizard: Settings,
    callback_payload: ShiftMember,
) -> None:
    """Step 3 pressed: the venue's own default window, which is one tap instead of five.

    The member comes off the payload and not off the state, because the resolver has just
    checked *that* id against this venue's `venue_members` (TZ 9) — the state is a copy of the
    same number and is only consulted when the payload cannot be trusted, which is never
    here.
    """
    start, end = await _default_window(venue_wizard, actor)
    await _create(
        callback,
        bot,
        state,
        actor,
        services,
        member_id=callback_payload.member_id,
        window=(start, end),
    )


async def stale_wizard_step(callback: CallbackQuery) -> None:
    """A wizard button pressed in a state it does not belong to (TZ 9: always answer).

    The screens of this wizard are edited in place, so the only way to reach this is a
    message that was superseded — a second copy of the person step left open while a scenario
    was started elsewhere. Nothing is written, and the spinner is closed with the truth.
    """
    await refuse(callback)


# --------------------------------------------------------------------------------------
# The card: the two marks of TZ 4.2, and removing a shift
# --------------------------------------------------------------------------------------


async def set_opener(
    callback: CallbackQuery,
    bot: Bot,
    actor: AccessContext,
    services: ScheduleServices,
    callback_payload: ShiftOpener,
) -> None:
    """Who opens this date (TZ 4.2: exactly one, checked in the service — decision D8)."""
    await _set_role(
        callback,
        bot,
        actor,
        services,
        shift_id=callback_payload.shift_id,
        role=ShiftRole.OPENER,
        assigned=callback_payload.is_set,
    )


async def set_closer(
    callback: CallbackQuery,
    bot: Bot,
    actor: AccessContext,
    services: ScheduleServices,
    callback_payload: ShiftCloser,
) -> None:
    """Who closes this date (TZ 4.2)."""
    await _set_role(
        callback,
        bot,
        actor,
        services,
        shift_id=callback_payload.shift_id,
        role=ShiftRole.CLOSER,
        assigned=callback_payload.is_set,
    )


async def delete_shift(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    services: ScheduleServices,
    callback_payload: ShiftDelete,
) -> None:
    """Remove a shift, and land back on the schedule (TZ 5.8).

    The notifications that hung off it are cancelled by `ShiftService.delete`, which calls
    the notification service; nothing here knows that a queue exists (TZ 6, plan task 33).
    The date the shift left may now have nobody opening it, and `ShiftRemoved.warnings` says
    so on the screen the manager is looking at — decision B4 applies to a delete exactly as
    it does to a save.
    """
    await state.clear()
    try:
        removed = await services.shifts.delete(actor, callback_payload.shift_id)
    except ShiftError:
        await refuse(callback)
        return
    await deliver(callback, await _board(actor, services, warnings=removed.warnings), bot=bot)


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


async def _board(
    actor: AccessContext,
    services: ScheduleServices,
    *,
    warnings: Sequence[ShiftWarning] = (),
) -> Screen:
    """The fortnight screen, built from one `roster()` call per day (see the docstring)."""
    return screens.schedule_screen(await _fortnight(actor, services), warnings=warnings)


async def _fortnight(
    actor: AccessContext,
    services: ScheduleServices,
) -> tuple[ShiftView, ...]:
    """Every shift of the venue for `SCHEDULE_HORIZON_DAYS`, today first (TZ 5.3).

    The horizon is the service's constant and not a number of this module: TZ 5.3 fixes two
    weeks for the employee's screen and the manager's is the same two weeks seen from above.
    """
    today = local_date(utc_now(), services.shifts.timezone)
    found: list[ShiftView] = []
    for offset in range(SCHEDULE_HORIZON_DAYS):
        found.extend(await services.shifts.roster(actor, today + dt.timedelta(days=offset)))
    return tuple(found)


async def _default_window(venue_wizard: Settings, actor: AccessContext) -> tuple[dt.time, dt.time]:
    """`venue_settings.default_shift_*` — the venue's own working hours (TZ 4.2, 5.8)."""
    configuration = await venue_wizard.update_settings(actor)
    return (
        configuration.settings.default_shift_start,
        configuration.settings.default_shift_end,
    )


async def _restart(
    event: Event,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    services: ScheduleServices,
) -> None:
    """The state lost what the wizard had collected: ask again from the first step.

    Reachable after a deploy past the state's TTL, or when the storage was cleared
    underneath. Nothing has been written at that point — the one call that writes is at the
    end of the last step — so there is no half-built shift to roll back.
    """
    entries = await services.members.list_assignable(actor)
    if entries:
        await state.set_state(ShiftWizard.person)
        await state.set_data({})
    else:
        await state.clear()
    await deliver(event, screens.person_step(entries), bot=bot)


async def _create(
    event: Event,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    services: ScheduleServices,
    *,
    member_id: int,
    window: tuple[dt.time, dt.time],
) -> None:
    """The end of the wizard: one call to the service, then the card of what it saved.

    The state is cleared before the write rather than after: the wizard is over the moment
    the shift exists, and a failure while rendering must not leave the manager in a step that
    would create a second one.

    `hours` is not passed and cannot be: TZ 4.2 has it computed from the window, and
    `ShiftService` is the only producer of that column (plan task 16).
    """
    remembered = await state.get_data()
    chosen = _remembered_date(remembered)
    entry = await services.members.get(actor, member_id)
    if chosen is None or entry is None or not entry.is_active:
        await _restart(event, bot, state, actor, services)
        return
    await state.clear()
    start, end = window
    try:
        saved = await services.shifts.create(
            actor,
            user_id=entry.user_id,
            shift_date=chosen,
            start_time=start,
            end_time=end,
        )
    except ShiftError:
        # The person stopped working here between the first step and this one, or the actor
        # stopped being a manager. Same flat refusal as everywhere else (TZ 9).
        await refuse(event)
        return
    # Decision B4: a date with nobody opening it is saved and says so, on this screen.
    await deliver(event, screens.shift_screen(saved.view, warnings=saved.warnings), bot=bot)


async def _set_role(
    callback: CallbackQuery,
    bot: Bot,
    actor: AccessContext,
    services: ScheduleServices,
    *,
    shift_id: int,
    role: ShiftRole,
    assigned: bool,
) -> None:
    """Set or clear one of the two marks of a date, and say what happened (TZ 4.2, B4)."""
    try:
        if role is ShiftRole.OPENER:
            saved = await services.shifts.set_opener(actor, shift_id, assigned=assigned)
        else:
            saved = await services.shifts.set_closer(actor, shift_id, assigned=assigned)
    except ShiftRoleTakenError as taken:
        await _say_taken(callback, bot, actor, services, shift_id=shift_id, taken=taken)
        return
    except ShiftError:
        await refuse(callback)
        return
    await deliver(callback, screens.shift_screen(saved.view, warnings=saved.warnings), bot=bot)


async def _say_taken(
    callback: CallbackQuery,
    bot: Bot,
    actor: AccessContext,
    services: ScheduleServices,
    *,
    shift_id: int,
    taken: ShiftRoleTakenError,
) -> None:
    """TZ 4.2 refused the mark: name whoever already carries it, on the same card.

    The name is read through `ShiftService.get`, which is scoped to the actor's venue — the
    error carries a `shift_id` and nothing else, and a handler that resolved it any other way
    would be a handler reading a row without the venue predicate (TZ 3.3, 9). A shift that
    cannot be read back leaves the sentence without a name rather than without a screen.
    """
    view = await services.shifts.get(actor, shift_id)
    if view is None:
        await refuse(callback)
        return
    holder = await services.shifts.get(actor, taken.taken_by)
    await deliver(
        callback,
        screens.shift_screen(
            view,
            taken=taken.role,
            taken_by="" if holder is None else holder.full_name,
        ),
        bot=bot,
    )


def _remembered_member(remembered: dict[str, Any]) -> int | None:
    stored = remembered.get(MEMBER_FIELD)
    return stored if isinstance(stored, int) else None


def _remembered_date(remembered: dict[str, Any]) -> dt.date | None:
    """The date step's answer, as it survived a round trip through Redis JSON."""
    stored = remembered.get(DATE_FIELD)
    if not isinstance(stored, str):
        return None
    try:
        return dt.date.fromisoformat(stored)
    except ValueError:
        return None


def router() -> Router:
    """The screens of this block (see the module docstring).

    Order matters twice. The two `ShiftMember` handlers are told apart by state and the
    catch-all sits after them, so a press in a known step never reaches the apology; and the
    message steps are filtered by state, so nothing typed outside the wizard is read as a
    date (a main-menu press is intercepted before any of this by
    `src/bot/middlewares/menu.py`).

    The `SCHEDULE_ADD_BUTTON` registration filters `TimezoneChoice` on an index no zone has
    and no other block claims — `src/bot/keyboards/schedule.py` explains why it is on that
    factory at all. `src/bot/handlers/admin_venue.py` is included before this router and
    filters the same factory on `index >= 0` and on three exact negative values, so the two
    sets cannot overlap.
    """
    instance = Router(name=ROUTER_NAME)
    instance.callback_query.register(
        open_schedule, OpenAdmin.filter(F.section == AdminSection.SCHEDULE)
    )
    instance.callback_query.register(
        start_wizard, TimezoneChoice.filter(F.index == ScheduleCommand.ADD_SHIFT.value)
    )
    instance.callback_query.register(
        pick_person, ShiftMember.filter(), StateFilter(ShiftWizard.person)
    )
    instance.callback_query.register(
        use_default_window, ShiftMember.filter(), StateFilter(ShiftWizard.window)
    )
    instance.callback_query.register(stale_wizard_step, ShiftMember.filter())
    instance.callback_query.register(show_shift, ShiftShow.filter())
    instance.callback_query.register(set_opener, ShiftOpener.filter())
    instance.callback_query.register(set_closer, ShiftCloser.filter())
    instance.callback_query.register(delete_shift, ShiftDelete.filter())
    instance.message.register(take_date, StateFilter(ShiftWizard.date))
    instance.message.register(take_window, StateFilter(ShiftWizard.window))
    return instance


__all__ = [
    "DATE_FIELD",
    "MEMBER_FIELD",
    "ROUTER_NAME",
    "Assignable",
    "Schedule",
    "ScheduleServices",
    "Settings",
    "date_in",
    "delete_shift",
    "open_schedule",
    "pick_person",
    "router",
    "set_closer",
    "set_opener",
    "show_shift",
    "stale_wizard_step",
    "start_wizard",
    "take_date",
    "take_window",
    "use_default_window",
]
