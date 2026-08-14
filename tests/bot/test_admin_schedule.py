"""«📅 График» as the manager sees it (TZ 5.3, 5.8; plan task 29).

Three kinds of test, split the way `tests/bot/test_admin_staff.py` splits them.

* **The screens are checked as values.** `src/bot/views/schedule.py` is a pure function of
  what the service returned, so the two empty states of TZ 8.1 — a venue with no shifts and
  a venue with nobody to roster — are ordinary asserts, including the property that matters
  most about them: every button on an empty screen goes somewhere real.
* **The handlers are exercised with fake services.** The fakes implement the protocols
  `src/bot/handlers/admin_schedule.py` declares, so mypy compares them against the real
  `ShiftService`, `MemberService` and `VenueService` through
  :func:`_the_real_services_satisfy_the_protocols` — a fake that promised more than the
  service does would fail the type check rather than make a green test about behaviour that
  does not exist (CLAUDE.md).
* **The rights are checked where they are decided.** Nothing in this module hides a button
  from `staff`: the resolver refuses the payloads that carry an id (TZ 9), and the one press
  that travels on a factory the resolver cannot scope — «Добавить смену», see
  `src/bot/keyboards/schedule.py` — is refused by the handler itself. Both are asserted.

The one thing the fakes copy from the real service on purpose is decision B4:
`ShiftService.create` **saves** a shift whose date has nobody opening it and reports
`ShiftWarning.NO_OPENER` in `warnings`. A fake that refused instead would make
`test_a_shift_without_an_opener_is_saved_and_says_so` green about the opposite rule.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup
from src.bot import texts
from src.bot.callbacks import (
    AdminSection,
    OpenAdmin,
    ShiftCloser,
    ShiftDelete,
    ShiftMember,
    ShiftOpener,
    ShiftShow,
    TimezoneChoice,
    parse,
)
from src.bot.handlers.admin_schedule import (
    Assignable,
    Schedule,
    ScheduleServices,
    Settings,
    date_in,
    delete_shift,
    open_schedule,
    pick_person,
    router,
    set_opener,
    show_shift,
    stale_wizard_step,
    start_wizard,
    take_date,
    take_window,
    use_default_window,
)
from src.bot.keyboards.schedule import ScheduleCommand
from src.bot.middlewares.resolver import RULES, Refusal, resolve
from src.bot.middlewares.services import VenueServices
from src.bot.states import ShiftWizard
from src.bot.views.schedule import person_step, schedule_screen, shift_screen
from src.db.models import (
    MemberRole,
    Shift,
    ShiftStatus,
    User,
    Venue,
    VenueMember,
    VenueSettings,
)
from src.services.access import AccessContext
from src.services.members import MemberService, RosterEntry
from src.services.shifts import (
    ShiftRemoved,
    ShiftRole,
    ShiftRoleTakenError,
    ShiftSaved,
    ShiftService,
    ShiftSnapshot,
    ShiftUserNotInVenueError,
    ShiftView,
    ShiftWarning,
    shift_hours,
)
from src.services.timezones import FixedClock, shift_window, use_clock
from src.services.venues import VenueConfiguration, VenueService

from tests.bot.test_middlewares import (
    CHAT_ID,
    MANAGER,
    MANAGER_TELEGRAM_ID,
    STAFF,
    VENUE_ID,
    FakeSubjects,
    make_bot,
    make_callback,
    make_message,
    session_of,
)

BOT_ID: Final = 42
TIMEZONE: Final = ZoneInfo("Europe/Moscow")
#: 2026-08-14 06:00Z is 09:00 in Moscow, so the venue's «today» is the fourteenth.
NOW: Final = dt.datetime(2026, 8, 14, 6, 0, tzinfo=dt.UTC)
TODAY: Final = dt.date(2026, 8, 14)
DEFAULT_START: Final = dt.time(8, 0)
DEFAULT_END: Final = dt.time(23, 0)


# --------------------------------------------------------------------------------------
# The harness
# --------------------------------------------------------------------------------------


def make_state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=MANAGER_TELEGRAM_ID),
    )


def make_entry(
    member_id: int = 7,
    *,
    full_name: str = "Ivan Ivanov",
    is_active: bool = True,
) -> RosterEntry:
    """One roster line, in memory: nothing here reaches a database."""
    return RosterEntry(
        member=VenueMember(
            id=member_id,
            venue_id=VENUE_ID,
            user_id=100 + member_id,
            role=MemberRole.STAFF,
            position=None,
            is_active=is_active,
        ),
        user=User(
            id=100 + member_id,
            telegram_id=900 + member_id,
            full_name=full_name,
            is_active=True,
        ),
    )


def make_view(
    shift_id: int = 1,
    *,
    user_id: int = 107,
    full_name: str = "Ivan Ivanov",
    shift_date: dt.date = TODAY,
    start_time: dt.time = DEFAULT_START,
    end_time: dt.time = DEFAULT_END,
    is_opener: bool = False,
    is_closer: bool = False,
) -> ShiftView:
    """A `ShiftView` exactly as `ShiftService` builds one, window included (D12)."""
    shift = Shift(
        id=shift_id,
        venue_id=VENUE_ID,
        user_id=user_id,
        shift_date=shift_date,
        start_time=start_time,
        end_time=end_time,
        hours=shift_hours(start_time, end_time),
        is_opener=is_opener,
        is_closer=is_closer,
        status=ShiftStatus.PLANNED,
    )
    starts_at, ends_at = shift_window(shift_date, start_time, end_time, TIMEZONE)
    return ShiftView(shift=shift, full_name=full_name, starts_at=starts_at, ends_at=ends_at)


class FakeShifts:
    """`ShiftService` as this module uses it, with every call written down.

    Decision B4 is copied from the real service and not simplified away: a shift is saved
    even when the date it lands on has nobody opening it, and the fact comes back in
    `warnings`. `taken` makes the next mark of a role raise the way TZ 4.2 makes the service
    raise, so the handler's "say whose it already is" branch is exercised against the real
    exception and not against a stub of it.
    """

    def __init__(self, views: Sequence[ShiftView] = (), *, opener_of: int | None = None) -> None:
        self.table = list(views)
        self.calls: list[tuple[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.deleted: list[int] = []
        #: The shift that already opens its date, if any (TZ 4.2: one per date).
        self.opener_of = opener_of

    @property
    def timezone(self) -> ZoneInfo:
        return TIMEZONE

    async def get(self, actor: AccessContext, shift_id: int) -> ShiftView | None:
        self.calls.append(("get", shift_id))
        return next((view for view in self.table if view.shift_id == shift_id), None)

    async def roster(self, actor: AccessContext, shift_date: dt.date) -> tuple[ShiftView, ...]:
        self.calls.append(("roster", shift_date))
        return tuple(view for view in self.table if view.shift_date == shift_date)

    async def create(
        self,
        actor: AccessContext,
        *,
        user_id: int,
        shift_date: dt.date,
        start_time: dt.time,
        end_time: dt.time,
    ) -> ShiftSaved:
        self.created.append(
            {
                "user_id": user_id,
                "shift_date": shift_date,
                "start_time": start_time,
                "end_time": end_time,
            }
        )
        view = make_view(
            shift_id=100 + len(self.created),
            user_id=user_id,
            shift_date=shift_date,
            start_time=start_time,
            end_time=end_time,
        )
        self.table.append(view)
        return ShiftSaved(view=view, warnings=self._warnings(shift_date))

    async def set_opener(
        self, actor: AccessContext, shift_id: int, *, assigned: bool = True
    ) -> ShiftSaved:
        self.calls.append(("set_opener", shift_id))
        if assigned and self.opener_of is not None and self.opener_of != shift_id:
            raise ShiftRoleTakenError(ShiftRole.OPENER, TODAY, self.opener_of)
        return self._marked(shift_id, is_opener=assigned)

    async def set_closer(
        self, actor: AccessContext, shift_id: int, *, assigned: bool = True
    ) -> ShiftSaved:
        self.calls.append(("set_closer", shift_id))
        return self._marked(shift_id, is_closer=assigned)

    async def delete(self, actor: AccessContext, shift_id: int) -> ShiftRemoved:
        self.calls.append(("delete", shift_id))
        self.deleted.append(shift_id)
        view = next(view for view in self.table if view.shift_id == shift_id)
        self.table.remove(view)
        return ShiftRemoved(
            shift=ShiftSnapshot.of(view.shift),
            warnings=self._warnings(view.shift_date),
        )

    # -- internals ----------------------------------------------------------------------

    def _marked(self, shift_id: int, **flags: bool) -> ShiftSaved:
        view = next(v for v in self.table if v.shift_id == shift_id)
        for name, value in flags.items():
            setattr(view.shift, name, value)
        if flags.get("is_opener"):
            self.opener_of = shift_id
        return ShiftSaved(view=view, warnings=self._warnings(view.shift_date))

    def _warnings(self, shift_date: dt.date) -> tuple[ShiftWarning, ...]:
        """The real rule of `ShiftService._warnings`: an empty date warns about nothing."""
        roster = [view for view in self.table if view.shift_date == shift_date]
        if not roster:
            return ()
        found: list[ShiftWarning] = []
        if not any(view.is_opener for view in roster):
            found.append(ShiftWarning.NO_OPENER)
        if not any(view.is_closer for view in roster):
            found.append(ShiftWarning.NO_CLOSER)
        return tuple(found)


class FakeMembers:
    """The half of `MemberService` this block reads (TZ 5.8, 3.3)."""

    def __init__(self, entries: Sequence[RosterEntry] = ()) -> None:
        self.table = list(entries)
        self.calls: list[tuple[str, int]] = []

    async def list_assignable(self, actor: AccessContext) -> tuple[RosterEntry, ...]:
        self.calls.append(("list_assignable", 0))
        # The real service lists the **active** members only (`VenueMemberRepo.list_active`).
        return tuple(entry for entry in self.table if entry.is_active)

    async def get(self, actor: AccessContext, member_id: int) -> RosterEntry | None:
        self.calls.append(("get", member_id))
        return next((entry for entry in self.table if entry.member_id == member_id), None)


class FakeSchedule:
    """`data["services"]`, narrowed to what these screens read."""

    def __init__(self, shifts: FakeShifts, members: FakeMembers) -> None:
        self.shifts = shifts
        self.members = members


class FakeSettings:
    """`VenueService.update_settings(actor)` with nothing to update — that is the read."""

    def __init__(self, *, start: dt.time = DEFAULT_START, end: dt.time = DEFAULT_END) -> None:
        self.reads = 0
        self.settings = VenueSettings(
            venue_id=VENUE_ID,
            opening_checklist_lead_minutes=30,
            default_shift_start=start,
            default_shift_end=end,
        )

    async def update_settings(self, actor: AccessContext, **fields: Any) -> VenueConfiguration:
        assert not fields, "this block reads the settings, it never writes them"
        self.reads += 1
        return VenueConfiguration(
            venue=Venue(id=VENUE_ID, name="venue", city="City", timezone=str(TIMEZONE)),
            settings=self.settings,
        )


def _the_real_services_satisfy_the_protocols(
    services: VenueServices,
    shifts: ShiftService,
    members: MemberService,
    venue_wizard: VenueService,
) -> tuple[ScheduleServices, Schedule, Assignable, Settings]:
    """mypy is the assertion here, and it is the point of the protocols.

    The handlers are typed against `ScheduleServices`, `Schedule` and `Settings` so a fake
    can be built without a session; that only stays honest while the real services still
    satisfy them. Nothing calls this function — it fails at type-check time or not at all.
    """
    return services, shifts, members, venue_wizard


def schedule_services(
    views: Sequence[ShiftView] = (),
    entries: Sequence[RosterEntry] = (),
    *,
    opener_of: int | None = None,
) -> tuple[FakeSchedule, FakeShifts, FakeMembers]:
    shifts = FakeShifts(views, opener_of=opener_of)
    members = FakeMembers(entries)
    return FakeSchedule(shifts, members), shifts, members


def edits_of(bot: Bot) -> list[EditMessageText]:
    return [call for call in session_of(bot).calls if isinstance(call, EditMessageText)]


def sends_of(bot: Bot) -> list[SendMessage]:
    return [call for call in session_of(bot).calls if isinstance(call, SendMessage)]


def alerts_of(bot: Bot) -> list[str]:
    return [
        str(call.text)
        for call in session_of(bot).calls
        if isinstance(call, AnswerCallbackQuery) and call.text
    ]


def captions(markup: InlineKeyboardMarkup | None) -> list[str]:
    assert markup is not None
    return [button.text for row in markup.inline_keyboard for button in row]


def payloads(markup: InlineKeyboardMarkup | None) -> list[str]:
    assert markup is not None
    return [button.callback_data or "" for row in markup.inline_keyboard for button in row]


def markup_of(call: EditMessageText | SendMessage) -> InlineKeyboardMarkup:
    markup = call.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return markup


# --------------------------------------------------------------------------------------
# The empty states (TZ 8.1) — both of them, because a venue starts inside both
# --------------------------------------------------------------------------------------


def test_the_empty_schedule_says_so_and_offers_the_button_that_ends_it() -> None:
    screen = schedule_screen(())
    assert texts.SCHEDULE_ADMIN_EMPTY in screen.text
    assert texts.SCHEDULE_ADD_BUTTON in captions(screen.markup)


def test_no_button_of_the_empty_schedule_leads_nowhere() -> None:
    """TZ 8.1: «ни одной кнопки в никуда» — every payload parses and has a rule.

    A rule in the resolver is what makes a payload reachable at all: a factory without one
    is refused before any handler sees it, so a button carrying it is decoration.
    """
    for payload in payloads(schedule_screen(()).markup):
        assert type(parse(payload)) in RULES, payload


async def test_the_add_button_of_the_empty_schedule_actually_starts_the_wizard() -> None:
    """The empty state is only done if its one button works — pressed, and not read."""
    bot = make_bot()
    state = make_state()
    empty = schedule_screen(())
    pressed = next(
        payload
        for payload, caption in zip(payloads(empty.markup), captions(empty.markup), strict=True)
        if caption == texts.SCHEDULE_ADD_BUTTON
    )
    resolution = await resolve(pressed, actor=MANAGER, repositories=FakeSubjects(VENUE_ID))
    assert resolution.is_allowed, "a manager may press the button their own empty screen drew"

    services, _, members = schedule_services(entries=[make_entry(7)])
    await start_wizard(
        make_callback(pressed, bot=bot),
        bot=bot,
        state=state,
        actor=MANAGER,
        services=services,
    )
    assert await state.get_state() == ShiftWizard.person.state
    assert str(edits_of(bot)[-1].text) == texts.SCHEDULE_PICK_PERSON
    assert members.calls == [("list_assignable", 0)]


def test_a_wizard_with_nobody_to_roster_points_at_the_employees_section() -> None:
    """TZ 8.1: choosing from an empty list is the broken button the rule is about."""
    screen = person_step(())
    assert screen.text == texts.SCHEDULE_NO_STAFF
    assert OpenAdmin(section=AdminSection.STAFF).pack() in payloads(screen.markup)
    assert texts.ADMIN_STAFF_BUTTON in captions(screen.markup)
    for payload in payloads(screen.markup):
        assert type(parse(payload)) in RULES, payload


async def test_pressing_add_without_employees_does_not_start_a_scenario() -> None:
    """A step nobody can answer is not a step: no state is set, so the next line the manager
    types is not read as an answer to a question that was never asked (TZ 8.2)."""
    bot = make_bot()
    state = make_state()
    services, _, _ = schedule_services()
    await start_wizard(
        make_callback(TimezoneChoice(index=ScheduleCommand.ADD_SHIFT.value).pack(), bot=bot),
        bot=bot,
        state=state,
        actor=MANAGER,
        services=services,
    )
    assert await state.get_state() is None
    assert str(edits_of(bot)[-1].text) == texts.SCHEDULE_NO_STAFF


# --------------------------------------------------------------------------------------
# The fortnight itself (TZ 5.3, 5.8)
# --------------------------------------------------------------------------------------


async def test_the_schedule_lists_the_venues_two_weeks_by_day() -> None:
    bot = make_bot()
    services, shifts, _ = schedule_services(
        [
            make_view(1, full_name="Ivan Ivanov", shift_date=TODAY, is_opener=True),
            make_view(2, full_name="Petr Petrov", shift_date=TODAY + dt.timedelta(days=3)),
        ]
    )
    with use_clock(FixedClock(NOW)):
        await open_schedule(
            make_callback(OpenAdmin(section=AdminSection.SCHEDULE).pack(), bot=bot),
            bot=bot,
            state=make_state(),
            actor=MANAGER,
            services=services,
        )
    screen = edits_of(bot)[-1]
    assert "Ivan Ivanov" in str(screen.text)
    assert "Petr Petrov" in str(screen.text)
    assert texts.SCHEDULE_OPENER_MARK.strip() in str(screen.text)
    opened = [
        parse(payload)
        for payload in payloads(markup_of(screen))
        if isinstance(parse(payload), ShiftShow)
    ]
    assert opened == [ShiftShow(shift_id=1), ShiftShow(shift_id=2)]
    # The horizon is the service's, and the days are asked for in order, starting today.
    asked = [day for name, day in shifts.calls if name == "roster"]
    assert asked[0] == TODAY
    assert asked == [TODAY + dt.timedelta(days=offset) for offset in range(len(asked))]


async def test_opening_the_section_ends_a_half_filled_wizard() -> None:
    """Arriving at the board is leaving the scenario; otherwise the next line typed in the
    chat would be read as somebody's date (TZ 8.2)."""
    bot = make_bot()
    state = make_state()
    await state.set_state(ShiftWizard.date)
    services, _, _ = schedule_services()
    with use_clock(FixedClock(NOW)):
        await open_schedule(
            make_callback(OpenAdmin(section=AdminSection.SCHEDULE).pack(), bot=bot),
            bot=bot,
            state=state,
            actor=MANAGER,
            services=services,
        )
    assert await state.get_state() is None


# --------------------------------------------------------------------------------------
# Adding a shift (TZ 5.3, 5.8)
# --------------------------------------------------------------------------------------


async def test_the_wizard_saves_one_shift_and_calls_the_service_once() -> None:
    """«Добавить» → кто → дата → окно, and exactly one write at the end of it."""
    bot = make_bot()
    state = make_state()
    services, shifts, _ = schedule_services(entries=[make_entry(7, full_name="Ivan Ivanov")])
    settings = FakeSettings()

    await start_wizard(
        make_callback(TimezoneChoice(index=ScheduleCommand.ADD_SHIFT.value).pack(), bot=bot),
        bot=bot,
        state=state,
        actor=MANAGER,
        services=services,
    )
    await pick_person(
        make_callback(ShiftMember(member_id=7).pack(), bot=bot),
        bot=bot,
        state=state,
        actor=MANAGER,
        services=services,
        callback_payload=ShiftMember(member_id=7),
    )
    assert await state.get_state() == ShiftWizard.date.state
    assert str(edits_of(bot)[-1].text) == texts.SCHEDULE_PICK_DATE

    with use_clock(FixedClock(NOW)):
        await take_date(
            make_message("16.08", bot=bot),
            bot=bot,
            state=state,
            actor=MANAGER,
            services=services,
            venue_wizard=settings,
        )
    assert await state.get_state() == ShiftWizard.window.state
    assert str(sends_of(bot)[-1].text) == texts.SCHEDULE_PICK_WINDOW
    # The default offered is the venue's own, read from `venue_settings` (TZ 4.2).
    assert "08:00" in " ".join(captions(markup_of(sends_of(bot)[-1])))
    assert settings.reads == 1

    await take_window(
        make_message("10:00 22:00", bot=bot),
        bot=bot,
        state=state,
        actor=MANAGER,
        services=services,
        venue_wizard=settings,
    )

    assert shifts.created == [
        {
            "user_id": 107,
            "shift_date": dt.date(2026, 8, 16),
            "start_time": dt.time(10, 0),
            "end_time": dt.time(22, 0),
        }
    ], "one shift, one call — the wizard writes at its last step and nowhere else"
    assert await state.get_state() is None, "the wizard is over the moment the shift exists"


async def test_the_default_window_button_saves_the_venues_own_hours() -> None:
    """Principle 1.4#5 and 1.4#4 in one button: pressed, not typed, and read from the venue.

    The times come from `venue_settings.default_shift_*`, so a bar that opens at eleven gets
    eleven without a line of code changing.
    """
    bot = make_bot()
    state = make_state()
    services, shifts, _ = schedule_services(entries=[make_entry(7)])
    settings = FakeSettings(start=dt.time(11, 0), end=dt.time(2, 0))
    await state.set_state(ShiftWizard.window)
    await state.update_data({"member_id": 7, "shift_date": "2026-08-16"})

    await use_default_window(
        make_callback(ShiftMember(member_id=7).pack(), bot=bot),
        bot=bot,
        state=state,
        actor=MANAGER,
        services=services,
        venue_wizard=settings,
        callback_payload=ShiftMember(member_id=7),
    )
    assert shifts.created == [
        {
            "user_id": 107,
            "shift_date": dt.date(2026, 8, 16),
            "start_time": dt.time(11, 0),
            "end_time": dt.time(2, 0),
        }
    ]


async def test_a_shift_without_an_opener_is_saved_and_says_so() -> None:
    """Decision B4: nothing is refused and nothing is queued — the screen says it.

    Section 6 of the TZ is a closed list of eight notification types and this is not one of
    them, so the manager reads it where they caused it.
    """
    bot = make_bot()
    state = make_state()
    services, shifts, _ = schedule_services(entries=[make_entry(7)])
    settings = FakeSettings()
    await state.set_state(ShiftWizard.window)
    await state.update_data({"member_id": 7, "shift_date": TODAY.isoformat()})

    await use_default_window(
        make_callback(ShiftMember(member_id=7).pack(), bot=bot),
        bot=bot,
        state=state,
        actor=MANAGER,
        services=services,
        venue_wizard=settings,
        callback_payload=ShiftMember(member_id=7),
    )
    assert len(shifts.created) == 1, "the shift is saved, not refused"
    assert len(shifts.table) == 1
    assert texts.SCHEDULE_NO_OPENER_WARNING in str(edits_of(bot)[-1].text)


def test_the_warning_is_only_drawn_when_the_service_reported_it() -> None:
    """The screen never re-derives decision B4: it prints what `warnings` carried."""
    view = make_view(1)
    assert texts.SCHEDULE_NO_OPENER_WARNING not in shift_screen(view).text
    warned = shift_screen(view, warnings=(ShiftWarning.NO_OPENER,))
    assert texts.SCHEDULE_NO_OPENER_WARNING in warned.text


async def test_an_unreadable_date_is_asked_for_again_and_nothing_is_remembered() -> None:
    bot = make_bot()
    state = make_state()
    services, shifts, _ = schedule_services(entries=[make_entry(7)])
    await state.set_state(ShiftWizard.date)
    await state.update_data({"member_id": 7})

    with use_clock(FixedClock(NOW)):
        await take_date(
            make_message("послезавтра", bot=bot),
            bot=bot,
            state=state,
            actor=MANAGER,
            services=services,
            venue_wizard=FakeSettings(),
        )
    assert await state.get_state() == ShiftWizard.date.state
    assert str(sends_of(bot)[-1].text) == texts.SCHEDULE_PICK_DATE
    assert shifts.created == []


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("16.08", dt.date(2026, 8, 16)),
        ("16.8.2027", dt.date(2027, 8, 16)),
        ("16/08/27", dt.date(2027, 8, 16)),
        # Typed in August for January: the nearest such date that is not in the past.
        ("02.01", dt.date(2027, 1, 2)),
        # Today itself is not in the past.
        ("14.08", TODAY),
        ("29.02", None),
        ("", None),
        ("завтра", None),
    ],
    ids=repr,
)
def test_a_date_is_read_without_demanding_a_year(typed: str, expected: dt.date | None) -> None:
    assert date_in(typed, TODAY) == expected


# --------------------------------------------------------------------------------------
# The card: the two marks of TZ 4.2
# --------------------------------------------------------------------------------------


async def test_the_card_shows_the_shift_and_what_can_be_done_to_it() -> None:
    bot = make_bot()
    services, _, _ = schedule_services([make_view(1, full_name="Ivan Ivanov")])
    await show_shift(
        make_callback(ShiftShow(shift_id=1).pack(), bot=bot),
        bot=bot,
        actor=MANAGER,
        services=services,
        callback_payload=ShiftShow(shift_id=1),
    )
    screen = edits_of(bot)[-1]
    assert "Ivan Ivanov" in str(screen.text)
    assert texts.SCHEDULE_OPENER_BUTTON in captions(markup_of(screen))
    assert texts.SCHEDULE_CLOSER_BUTTON in captions(markup_of(screen))
    assert texts.SCHEDULE_DELETE_BUTTON in captions(markup_of(screen))


def test_the_mark_buttons_ask_for_the_state_they_want() -> None:
    """A press says «make this one the opener», not «toggle whatever you find»: two managers
    on the same card must not undo each other."""
    plain = payloads(shift_screen(make_view(1)).markup)
    assert ShiftOpener(shift_id=1, is_set=True).pack() in plain
    marked = payloads(shift_screen(make_view(1, is_opener=True)).markup)
    assert ShiftOpener(shift_id=1, is_set=False).pack() in marked


async def test_a_taken_opener_is_a_sentence_with_a_name_in_it() -> None:
    """TZ 4.2 and decision D8: one opener per date, and the refusal names who holds it."""
    bot = make_bot()
    services, shifts, _ = schedule_services(
        [
            make_view(1, full_name="Ivan Ivanov"),
            make_view(2, full_name="Petr Petrov", is_opener=True),
        ],
        opener_of=2,
    )
    await set_opener(
        make_callback(ShiftOpener(shift_id=1, is_set=True).pack(), bot=bot),
        bot=bot,
        actor=MANAGER,
        services=services,
        callback_payload=ShiftOpener(shift_id=1, is_set=True),
    )
    shown = str(edits_of(bot)[-1].text)
    assert texts.SCHEDULE_OPENER_TAKEN_TEMPLATE.format(full_name="Petr Petrov") in shown
    assert alerts_of(bot) != [texts.ERROR_SOMETHING_WENT_WRONG], "a refusal, not a traceback"
    assert ("set_opener", 1) in shifts.calls


async def test_setting_the_opener_goes_through_the_service() -> None:
    bot = make_bot()
    services, shifts, _ = schedule_services([make_view(1)])
    await set_opener(
        make_callback(ShiftOpener(shift_id=1, is_set=True).pack(), bot=bot),
        bot=bot,
        actor=MANAGER,
        services=services,
        callback_payload=ShiftOpener(shift_id=1, is_set=True),
    )
    assert ("set_opener", 1) in shifts.calls
    assert texts.SCHEDULE_NO_OPENER_WARNING not in str(edits_of(bot)[-1].text)


# --------------------------------------------------------------------------------------
# Removing a shift (TZ 5.8, TZ 6)
# --------------------------------------------------------------------------------------


async def test_deleting_a_shift_calls_the_service_and_lands_on_the_schedule() -> None:
    """The scheduler is never touched here: `ShiftService.delete` cancels what hung off the
    shift, and this module has no way of doing it itself (TZ 6, plan task 33)."""
    bot = make_bot()
    services, shifts, _ = schedule_services(
        [make_view(1, full_name="Ivan Ivanov"), make_view(2, full_name="Petr Petrov")]
    )
    with use_clock(FixedClock(NOW)):
        await delete_shift(
            make_callback(ShiftDelete(shift_id=1).pack(), bot=bot),
            bot=bot,
            state=make_state(),
            actor=MANAGER,
            services=services,
            callback_payload=ShiftDelete(shift_id=1),
        )
    assert shifts.deleted == [1]
    shown = str(edits_of(bot)[-1].text)
    assert "Ivan Ivanov" not in shown
    assert "Petr Petrov" in shown


async def test_a_delete_that_leaves_a_date_without_an_opener_says_so() -> None:
    """Decision B4 applies to a delete as much as to a save: the date is still short."""
    bot = make_bot()
    services, _, _ = schedule_services([make_view(1), make_view(2)])
    with use_clock(FixedClock(NOW)):
        await delete_shift(
            make_callback(ShiftDelete(shift_id=1).pack(), bot=bot),
            bot=bot,
            state=make_state(),
            actor=MANAGER,
            services=services,
            callback_payload=ShiftDelete(shift_id=1),
        )
    assert texts.SCHEDULE_NO_OPENER_WARNING in str(edits_of(bot)[-1].text)


# --------------------------------------------------------------------------------------
# Refusals (TZ 9)
# --------------------------------------------------------------------------------------


async def test_a_person_who_stopped_working_here_is_refused() -> None:
    """TZ 5.1 keeps the row of a dismissed employee; that row is history, not a candidate."""
    bot = make_bot()
    services, _, _ = schedule_services(entries=[make_entry(7, is_active=False)])
    await pick_person(
        make_callback(ShiftMember(member_id=7).pack(), bot=bot),
        bot=bot,
        state=make_state(),
        actor=MANAGER,
        services=services,
        callback_payload=ShiftMember(member_id=7),
    )
    assert alerts_of(bot) == [texts.ERROR_NOT_ALLOWED]
    assert edits_of(bot) == []


async def test_a_service_refusal_at_the_last_step_is_a_flat_no() -> None:
    """A stranger smuggled into the last step: the service refuses and the manager is told
    nothing about whose id it was (TZ 9)."""

    class Refusing(FakeShifts):
        async def create(
            self,
            actor: AccessContext,
            *,
            user_id: int,
            shift_date: dt.date,
            start_time: dt.time,
            end_time: dt.time,
        ) -> ShiftSaved:
            raise ShiftUserNotInVenueError(user_id, VENUE_ID)

    bot = make_bot()
    state = make_state()
    members = FakeMembers([make_entry(7)])
    services = FakeSchedule(Refusing(), members)
    await state.set_state(ShiftWizard.window)
    await state.update_data({"member_id": 7, "shift_date": TODAY.isoformat()})
    await use_default_window(
        make_callback(ShiftMember(member_id=7).pack(), bot=bot),
        bot=bot,
        state=state,
        actor=MANAGER,
        services=services,
        venue_wizard=FakeSettings(),
        callback_payload=ShiftMember(member_id=7),
    )
    assert alerts_of(bot) == [texts.ERROR_NOT_ALLOWED]


async def test_a_wizard_button_pressed_in_a_step_it_does_not_belong_to_is_answered() -> None:
    """TZ 9: an action answers in a second, even when the answer is «this is not available»."""
    bot = make_bot()
    await stale_wizard_step(make_callback(ShiftMember(member_id=7).pack(), bot=bot))
    assert alerts_of(bot) == [texts.ERROR_NOT_ALLOWED]
    assert edits_of(bot) == []


async def test_staff_cannot_start_the_wizard_even_though_the_resolver_lets_the_press() -> None:
    """The one press of this block the resolver cannot scope, checked in the handler.

    «Добавить смену» rides on `TimezoneChoice`, whose rule is `needs_actor=False` because
    the create-venue wizard uses the same factory before any membership exists. So the role
    is checked here, on the server, and not by leaving the button undrawn (TZ 9, CLAUDE.md).
    """
    bot = make_bot()
    services, _, members = schedule_services(entries=[make_entry(7)])
    state = make_state()
    await start_wizard(
        make_callback(TimezoneChoice(index=ScheduleCommand.ADD_SHIFT.value).pack(), bot=bot),
        bot=bot,
        state=state,
        actor=STAFF,
        services=services,
    )
    assert alerts_of(bot) == [texts.ERROR_NOT_ALLOWED]
    assert members.calls == [], "nothing about this venue is read for somebody refused"
    assert await state.get_state() is None


@pytest.mark.parametrize(
    "payload",
    [
        OpenAdmin(section=AdminSection.SCHEDULE),
        ShiftShow(shift_id=1),
        ShiftOpener(shift_id=1, is_set=True),
        ShiftCloser(shift_id=1, is_set=True),
        ShiftDelete(shift_id=1),
        ShiftMember(member_id=7),
    ],
    ids=lambda payload: type(payload).__name__,
)
async def test_staff_is_refused_every_payload_of_this_section(payload: Any) -> None:
    resolution = await resolve(
        payload.pack(),
        actor=STAFF,
        repositories=FakeSubjects(VENUE_ID),
    )
    assert resolution.refusal is Refusal.FORBIDDEN
    assert resolution.payload is None


async def test_a_manager_gets_through_to_the_section() -> None:
    """The other half of the check above: the refusal is about the role and not about the
    section being unreachable for everybody."""
    resolution = await resolve(
        OpenAdmin(section=AdminSection.SCHEDULE).pack(),
        actor=MANAGER,
        repositories=FakeSubjects(VENUE_ID),
    )
    assert resolution.is_allowed


def test_hours_are_never_typed_in() -> None:
    """TZ 4.2: `hours` is computed from the window, and this block has no way to pass one.

    Asserted on the protocol rather than on a screen: `Schedule.create` is the whole of what
    this module may ask of the service, and it has no `hours` parameter to fill in.
    """
    assert "hours" not in Schedule.create.__annotations__
    assert shift_hours(dt.time(14, 0), dt.time(0, 0)) == Decimal("10.00")


def test_the_router_is_named_and_registers_the_screens() -> None:
    instance = router()
    assert instance.name == "admin_schedule"
    assert len(instance.callback_query.handlers) == 9
    assert len(instance.message.handlers) == 2
