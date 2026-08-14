"""The main menu, the shift screen and the schedule (TZ 5.2, 5.3; plan task 23).

Three things are checked here, and they are three because they fail in three different
ways.

**The views, against a pinned clock and nothing else.** They are pure functions, so the
midnight case is an ordinary `assert` on a string: a shift running 18:00-02:00 seen at
00:30 has to read "you are in it" while its date still says yesterday. That is the whole
requirement of TZ 5.3 for this screen, and it is decided by the window rather than by the
calendar day - which is why the assertion is on the heading *and* on the date printed
under it.

**A note on the 14:00-00:00 example.** The window of this project is half-open
(`ShiftView.covers`, and `tests/services/test_shifts.py` asserts it in both directions), so
a shift ending at 00:00 is over at 00:30 by half an hour - at that moment the service hands
the screen tomorrow's shift, and the screen says so. The case where the date rolls over and
the shift does not is a window that actually crosses midnight, and that is the one the
snapshot below pins. Both are here, next to each other, because the difference between them
is exactly what a screen comparing dates would get wrong.

**The empty states, which are the first screen every venue sees** (TZ 8.1). Asserted with
their keyboards: an empty screen offering a button to a checklist that does not exist is
the failure the rule is about, not the wording.

**The handlers, through a real `Dispatcher`.** What is worth testing there is routing and
delivery, not the schedule: which of the three roads into a section reaches it, that a
button press edits the screen in place while a caption sends a new one (TZ 8.2), and that
the navigation buttons of TZ 5.2 and 8.2 both return to the menu and leave no half-open
scenario behind. The service is a fake with the signatures of `ShiftService`; the rules it
implements are its own and are tested in `tests/services/test_shifts.py`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any, Final

from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardMarkup, Update
from src.bot import handlers, texts
from src.bot.callbacks import ChecklistShow, MenuAction, Nav, NavTarget, OpenSection
from src.bot.keyboards.menu import home_button
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.services import SERVICES_KEY
from src.bot.views.shifts import schedule_screen, shift_screen
from src.db.models import Shift, ShiftSource, ShiftStatus
from src.services.access import AccessContext
from src.services.shifts import ShiftView, shift_hours
from src.services.timezones import FixedClock, shift_window, use_clock, venue_timezone

from tests.bot.test_middlewares import (
    CHAT_ID,
    OTHER_USER_ID,
    STAFF,
    STAFF_TELEGRAM_ID,
    STAFF_USER_ID,
    VENUE_ID,
    make_bot,
    make_callback,
    make_message,
    session_of,
)

#: `make_venue()` works in Moscow, so the wall clock of every shift below is UTC+3.
TZ: Final = venue_timezone("Europe/Moscow")

#: The day the schedule of these tests is written for.
DAY: Final = dt.date(2026, 8, 13)


def utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC)


#: 00:30 in Moscow on the 14th - the moment the whole of this screen exists for.
HALF_PAST_MIDNIGHT: Final = utc(2026, 8, 13, 21, 30)


# --------------------------------------------------------------------------------------
# Building what the service would have returned
# --------------------------------------------------------------------------------------


def make_shift(
    shift_id: int,
    *,
    user_id: int = STAFF_USER_ID,
    shift_date: dt.date = DAY,
    start_time: dt.time = dt.time(9, 0),
    end_time: dt.time = dt.time(21, 0),
    is_opener: bool = False,
    is_closer: bool = False,
) -> Shift:
    return Shift(
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
        source=ShiftSource.MANUAL,
    )


def view_of(shift: Shift, full_name: str = "user-10") -> ShiftView:
    """A `ShiftView` with the window the service would have computed.

    Through `shift_window` rather than by hand: the two instants are what decides whether
    the screen says "now" or "next", and a test that made them up would be asserting
    against its own arithmetic instead of against decision D12.
    """
    starts_at, ends_at = shift_window(shift.shift_date, shift.start_time, shift.end_time, TZ)
    return ShiftView(shift=shift, full_name=full_name, starts_at=starts_at, ends_at=ends_at)


def lines_of(text: str) -> list[str]:
    return text.split("\n")


# --------------------------------------------------------------------------------------
# «Моя смена»: the empty state (TZ 8.1)
# --------------------------------------------------------------------------------------


def test_a_venue_with_no_schedule_gets_a_screen_and_not_an_error() -> None:
    screen = shift_screen(None, now=HALF_PAST_MIDNIGHT)

    assert screen.text == texts.SHIFT_NONE
    assert screen.markup == InlineKeyboardMarkup(inline_keyboard=[[home_button()]])


def test_the_empty_state_offers_no_checklist_to_open() -> None:
    """TZ 8.1: a button leading nowhere is worse than no button."""
    screen = shift_screen(None, now=HALF_PAST_MIDNIGHT)

    assert _payloads(screen.markup) == [Nav(target=NavTarget.HOME).pack()]


# --------------------------------------------------------------------------------------
# «Моя смена»: current beats next, by window and not by date (TZ 5.3)
# --------------------------------------------------------------------------------------


def test_a_shift_across_midnight_is_still_the_current_one_at_half_past() -> None:
    """18:00-02:00, read at 00:30: the date rolled over and the shift did not.

    The heading and the date are asserted together on purpose. A screen that decided "is
    this mine right now" from `shift_date` would print yesterday's date under the "next
    shift" heading, and the bartender standing behind the bar would be told their shift
    starts in eighteen hours.
    """
    night = view_of(make_shift(1, start_time=dt.time(18, 0), end_time=dt.time(2, 0)))

    screen = shift_screen(night, now=HALF_PAST_MIDNIGHT)

    assert lines_of(screen.text)[0] == texts.SHIFT_NOW_TITLE
    assert lines_of(screen.text)[1] == texts.SHIFT_WHEN_TEMPLATE.format(
        date="13.08",
        window=texts.TIME_RANGE_TEMPLATE.format(start="18:00", end="02:00"),
    )


def test_the_whole_screen_of_a_running_night_shift() -> None:
    """The snapshot: heading, window, the two marks, and who else is on."""
    night = view_of(
        make_shift(1, start_time=dt.time(18, 0), end_time=dt.time(2, 0), is_opener=True)
    )
    colleague = view_of(
        make_shift(2, user_id=OTHER_USER_ID, start_time=dt.time(20, 0), end_time=dt.time(2, 0)),
        full_name="user-12",
    )

    screen = shift_screen(night, now=HALF_PAST_MIDNIGHT, roster=(night, colleague))

    assert screen.text == "\n".join(
        [
            texts.SHIFT_NOW_TITLE,
            texts.SHIFT_WHEN_TEMPLATE.format(
                date="13.08",
                window=texts.TIME_RANGE_TEMPLATE.format(start="18:00", end="02:00"),
            ),
            texts.SHIFT_YOU_OPEN,
            "",
            texts.SHIFT_TEAM_TITLE,
            texts.SHIFT_TEAM_LINE_TEMPLATE.format(
                full_name="user-12",
                window=texts.TIME_RANGE_TEMPLATE.format(start="20:00", end="02:00"),
            ),
        ]
    )


def test_a_shift_ending_at_midnight_is_current_until_it_ends_and_not_after() -> None:
    """The half-open window, seen from the screen (`ShiftView.covers`).

    14:00-00:00 is the example the task brief names, and it is the one case where the
    window and the calendar day end together: at 23:45 the shift is on, at 00:30 it is over
    by half an hour and the service hands the screen the *next* one. The screen follows the
    window in both directions and invents no grace period of its own.
    """
    evening = view_of(make_shift(1, start_time=dt.time(14, 0), end_time=dt.time(0, 0)))

    quarter_to = shift_screen(evening, now=utc(2026, 8, 13, 20, 45))
    assert lines_of(quarter_to.text)[0] == texts.SHIFT_NOW_TITLE

    tomorrow = view_of(make_shift(2, shift_date=DAY + dt.timedelta(days=1)))
    later = shift_screen(tomorrow, now=HALF_PAST_MIDNIGHT)
    assert lines_of(later.text)[0] == texts.SHIFT_NEXT_TITLE
    assert lines_of(later.text)[1] == texts.SHIFT_WHEN_TEMPLATE.format(
        date="14.08",
        window=texts.TIME_RANGE_TEMPLATE.format(start="09:00", end="21:00"),
    )


def test_both_marks_can_land_on_one_shift() -> None:
    """TZ 4.2: the same person may open and close a date."""
    alone = view_of(make_shift(1, is_opener=True, is_closer=True))

    screen = shift_screen(alone, now=utc(2026, 8, 13, 9, 0))

    assert texts.SHIFT_YOU_OPEN in lines_of(screen.text)
    assert texts.SHIFT_YOU_CLOSE in lines_of(screen.text)


def test_working_alone_is_said_in_words_rather_than_left_blank() -> None:
    """TZ 8.1: a heading with nothing under it is not an empty state."""
    alone = view_of(make_shift(1))

    screen = shift_screen(alone, now=utc(2026, 8, 13, 9, 0), roster=(alone,))

    assert lines_of(screen.text)[-1] == texts.SHIFT_TEAM_ALONE
    assert texts.SHIFT_TEAM_TITLE not in lines_of(screen.text)


def test_somebody_rostered_twice_is_not_their_own_colleague() -> None:
    """The roster is filtered by person, not by row."""
    morning = view_of(make_shift(1, start_time=dt.time(9, 0), end_time=dt.time(14, 0)))
    evening = view_of(make_shift(2, start_time=dt.time(18, 0), end_time=dt.time(2, 0)))

    screen = shift_screen(morning, now=utc(2026, 8, 13, 9, 0), roster=(morning, evening))

    assert lines_of(screen.text)[-1] == texts.SHIFT_TEAM_ALONE


# --------------------------------------------------------------------------------------
# «Моя смена»: the checklist button (TZ 5.4)
# --------------------------------------------------------------------------------------


def test_the_checklist_button_carries_the_run_it_reopens() -> None:
    shift = view_of(make_shift(1))

    screen = shift_screen(shift, now=utc(2026, 8, 13, 9, 0), checklist_run_id=77)

    assert _payloads(screen.markup) == [
        ChecklistShow(run_id=77).pack(),
        Nav(target=NavTarget.HOME).pack(),
    ]


def test_a_shift_with_no_run_yet_shows_no_checklist_button() -> None:
    shift = view_of(make_shift(1))

    screen = shift_screen(shift, now=utc(2026, 8, 13, 9, 0))

    assert _payloads(screen.markup) == [Nav(target=NavTarget.HOME).pack()]


# --------------------------------------------------------------------------------------
# «График» (TZ 5.3)
# --------------------------------------------------------------------------------------


def test_the_fortnight_of_a_venue_that_just_started_is_a_screen() -> None:
    screen = schedule_screen(())

    assert screen.text == texts.SCHEDULE_EMPTY
    assert screen.markup == InlineKeyboardMarkup(inline_keyboard=[[home_button()]])


def test_the_fortnight_prints_one_line_per_shift_with_its_marks() -> None:
    shifts = (
        view_of(make_shift(1, start_time=dt.time(18, 0), end_time=dt.time(2, 0), is_opener=True)),
        view_of(make_shift(2, shift_date=DAY + dt.timedelta(days=1))),
        view_of(
            make_shift(
                3,
                shift_date=DAY + dt.timedelta(days=2),
                start_time=dt.time(14, 0),
                end_time=dt.time(0, 0),
                is_opener=True,
                is_closer=True,
            )
        ),
    )

    screen = schedule_screen(shifts)

    assert screen.text == "\n".join(
        [
            texts.SCHEDULE_TITLE,
            "",
            texts.SCHEDULE_LINE_TEMPLATE.format(
                date="13.08",
                window=texts.TIME_RANGE_TEMPLATE.format(start="18:00", end="02:00"),
                mark=texts.SCHEDULE_OPENER_MARK,
            ),
            texts.SCHEDULE_LINE_TEMPLATE.format(
                date="14.08",
                window=texts.TIME_RANGE_TEMPLATE.format(start="09:00", end="21:00"),
                mark="",
            ),
            texts.SCHEDULE_LINE_TEMPLATE.format(
                date="15.08",
                window=texts.TIME_RANGE_TEMPLATE.format(start="14:00", end="00:00"),
                mark=texts.SCHEDULE_OPENER_MARK + texts.SCHEDULE_CLOSER_MARK,
            ),
        ]
    )


def test_the_fortnight_offers_nothing_to_press_but_the_way_home() -> None:
    """TZ 5.3 gives the employee a read-only fortnight; editing is the manager's screen."""
    screen = schedule_screen((view_of(make_shift(1)),))

    assert _payloads(screen.markup) == [Nav(target=NavTarget.HOME).pack()]


def _payloads(markup: InlineKeyboardMarkup | None) -> list[str]:
    assert markup is not None
    return [str(button.callback_data) for row in markup.inline_keyboard for button in row]


# --------------------------------------------------------------------------------------
# The handlers
# --------------------------------------------------------------------------------------


class Scenario(StatesGroup):
    """Some wizard of TZ 5.8, half filled in when a navigation button is pressed."""

    step = State()


class FakeShiftService:
    """`ShiftService` as this module calls it, and with its signatures.

    A fake rather than the real service over fake repositories: what the handler is
    responsible for is asking the right question and drawing the answer, and the rules
    behind `nearest_shift` are exercised where they live
    (`tests/services/test_shifts.py`). What it *does* record is which date the roster was
    asked for, because choosing that date is the handler's own decision.
    """

    def __init__(
        self,
        *,
        nearest: ShiftView | None = None,
        roster: Sequence[ShiftView] = (),
        fortnight: Sequence[ShiftView] = (),
    ) -> None:
        self._nearest = nearest
        self._roster = tuple(roster)
        self._fortnight = tuple(fortnight)
        self.asked_at: list[dt.datetime] = []
        self.roster_dates: list[dt.date] = []

    async def nearest_shift(
        self,
        actor: AccessContext,
        *,
        now: dt.datetime,
        user_id: int | None = None,
    ) -> ShiftView | None:
        self.asked_at.append(now)
        return self._nearest

    async def roster(self, actor: AccessContext, shift_date: dt.date) -> tuple[ShiftView, ...]:
        self.roster_dates.append(shift_date)
        return self._roster

    async def fortnight(
        self,
        actor: AccessContext,
        *,
        now: dt.datetime,
        user_id: int | None = None,
    ) -> tuple[ShiftView, ...]:
        self.asked_at.append(now)
        return self._fortnight


class FakeServices:
    """The venue bundle, cut down to what this module reads off it."""

    def __init__(self, shifts: FakeShiftService) -> None:
        self.shifts = shifts


def build_stand() -> tuple[Dispatcher, Bot, FSMContext]:
    bot = make_bot()
    storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(handlers.menu.router())
    state = FSMContext(
        storage=storage,
        key=StorageKey(bot_id=bot.id, chat_id=CHAT_ID, user_id=STAFF_TELEGRAM_ID),
    )
    return dispatcher, bot, state


async def feed(
    dispatcher: Dispatcher,
    bot: Bot,
    update: Update,
    services: FakeServices | None,
) -> Any:
    return await dispatcher.feed_update(
        bot,
        update,
        **{ACTOR_KEY: STAFF, SERVICES_KEY: services},
    )


def caption_update(caption: str) -> Update:
    return Update(update_id=1, message=make_message(caption))


def button_update(payload: str) -> Update:
    return Update(update_id=1, callback_query=make_callback(payload))


def edits(bot: Bot) -> list[EditMessageText]:
    return [call for call in session_of(bot).calls if isinstance(call, EditMessageText)]


async def test_the_caption_sends_the_shift_screen() -> None:
    """TZ 5.2: the reply keyboard is the road in, and it arrives as an ordinary message."""
    shift = view_of(make_shift(1, start_time=dt.time(18, 0), end_time=dt.time(2, 0)))
    services = FakeServices(FakeShiftService(nearest=shift, roster=(shift,)))
    dispatcher, bot, _ = build_stand()

    with use_clock(FixedClock(HALF_PAST_MIDNIGHT)):
        await feed(dispatcher, bot, caption_update(texts.MENU_SHIFT_BUTTON), services)

    expected = shift_screen(shift, now=HALF_PAST_MIDNIGHT, roster=(shift,))
    assert session_of(bot).sent_texts() == [expected.text]
    assert not edits(bot), "a caption is a new message, there is nothing to edit"


async def test_the_roster_is_asked_for_the_date_of_the_shift_not_for_today() -> None:
    """At 00:30 those are two different days, and today's would show an empty team."""
    night = view_of(make_shift(1, start_time=dt.time(18, 0), end_time=dt.time(2, 0)))
    shifts = FakeShiftService(nearest=night)
    dispatcher, bot, _ = build_stand()

    with use_clock(FixedClock(HALF_PAST_MIDNIGHT)):
        await feed(dispatcher, bot, caption_update(texts.MENU_SHIFT_BUTTON), FakeServices(shifts))

    assert shifts.roster_dates == [DAY]
    assert shifts.asked_at == [HALF_PAST_MIDNIGHT], "the clock of the project, passed down"


async def test_an_empty_schedule_asks_the_service_for_no_roster_at_all() -> None:
    shifts = FakeShiftService()
    dispatcher, bot, _ = build_stand()

    await feed(dispatcher, bot, caption_update(texts.MENU_SHIFT_BUTTON), FakeServices(shifts))

    assert session_of(bot).sent_texts() == [texts.SHIFT_NONE]
    assert shifts.roster_dates == []


async def test_the_inline_button_edits_the_screen_in_place() -> None:
    """TZ 8.2: one screen, not a new message per tap."""
    shift = view_of(make_shift(1))
    services = FakeServices(FakeShiftService(nearest=shift, roster=(shift,)))
    dispatcher, bot, _ = build_stand()

    with use_clock(FixedClock(utc(2026, 8, 13, 9, 0))):
        await feed(
            dispatcher,
            bot,
            button_update(OpenSection(section=MenuAction.MY_SHIFT).pack()),
            services,
        )

    assert [str(call.text) for call in edits(bot)] == [
        shift_screen(shift, now=utc(2026, 8, 13, 9, 0), roster=(shift,)).text
    ]
    assert not session_of(bot).sent_texts()
    assert session_of(bot).answers(), "TZ 9: the spinner is always closed"


async def test_back_from_a_screen_below_returns_to_the_shift() -> None:
    """The checklist of TZ 5.4 hangs off this screen and comes back to it."""
    shift = view_of(make_shift(1))
    services = FakeServices(FakeShiftService(nearest=shift, roster=(shift,)))
    dispatcher, bot, _ = build_stand()

    payload = Nav(target=NavTarget.BACK, section=MenuAction.MY_SHIFT).pack()
    with use_clock(FixedClock(utc(2026, 8, 13, 9, 0))):
        await feed(dispatcher, bot, button_update(payload), services)

    assert [str(call.text) for call in edits(bot)] == [
        shift_screen(shift, now=utc(2026, 8, 13, 9, 0), roster=(shift,)).text
    ]


async def test_the_schedule_caption_sends_the_fortnight() -> None:
    shifts = (view_of(make_shift(1)), view_of(make_shift(2, shift_date=DAY + dt.timedelta(days=1))))
    services = FakeServices(FakeShiftService(fortnight=shifts))
    dispatcher, bot, _ = build_stand()

    await feed(dispatcher, bot, caption_update(texts.MENU_SCHEDULE_BUTTON), services)

    assert session_of(bot).sent_texts() == [schedule_screen(shifts).text]


async def test_the_schedule_is_reached_by_its_inline_button_too() -> None:
    services = FakeServices(FakeShiftService())
    dispatcher, bot, _ = build_stand()

    for payload in (
        OpenSection(section=MenuAction.SCHEDULE).pack(),
        Nav(target=NavTarget.BACK, section=MenuAction.SCHEDULE).pack(),
    ):
        await feed(dispatcher, bot, button_update(payload), services)

    assert [str(call.text) for call in edits(bot)] == [texts.SCHEDULE_EMPTY, texts.SCHEDULE_EMPTY]


async def test_a_caption_of_another_section_is_left_to_its_own_module() -> None:
    """One module owns one section (`src/bot/handlers/__init__.py`)."""
    services = FakeServices(FakeShiftService())
    dispatcher, bot, _ = build_stand()

    await feed(dispatcher, bot, caption_update(texts.MENU_CATALOGUE_BUTTON), services)

    assert not session_of(bot).sent_texts()


# --------------------------------------------------------------------------------------
# Home, cancel and back to the menu (TZ 5.2, 8.2)
# --------------------------------------------------------------------------------------


async def test_home_returns_to_the_menu_and_leaves_no_scenario_open() -> None:
    dispatcher, bot, state = build_stand()
    await state.set_state(Scenario.step)
    await state.update_data(full_name="X")

    await feed(dispatcher, bot, button_update(Nav(target=NavTarget.HOME).pack()), None)

    assert [str(call.text) for call in edits(bot)] == [texts.MENU_PROMPT]
    assert await state.get_state() is None
    assert await state.get_data() == {}


async def test_cancel_says_that_nothing_was_saved() -> None:
    """TZ 8.2: the wording arrives as the answer to the press, within the second of TZ 9."""
    dispatcher, bot, state = build_stand()
    await state.set_state(Scenario.step)
    await state.update_data(full_name="X")

    await feed(dispatcher, bot, button_update(Nav(target=NavTarget.CANCEL).pack()), None)

    assert [str(call.text) for call in edits(bot)] == [texts.MENU_PROMPT]
    assert [call.text for call in session_of(bot).answers()] == [texts.CANCELLED]
    assert await state.get_state() is None
    assert await state.get_data() == {}


async def test_back_with_no_section_is_the_main_menu() -> None:
    """Decision D10: from a first-level screen there is one way up, and it is the menu."""
    dispatcher, bot, state = build_stand()
    await state.set_state(Scenario.step)

    await feed(dispatcher, bot, button_update(Nav(target=NavTarget.BACK).pack()), None)

    assert [str(call.text) for call in edits(bot)] == [texts.MENU_PROMPT]
    assert await state.get_state() is None


async def test_the_menu_screen_carries_no_inline_keyboard() -> None:
    """The menu is the reply keyboard at the bottom of the chat, not a message (TZ 5.2)."""
    dispatcher, bot, _ = build_stand()

    await feed(dispatcher, bot, button_update(Nav(target=NavTarget.HOME).pack()), None)

    assert [call.reply_markup for call in edits(bot)] == [None]


async def test_somebody_with_no_venue_yet_is_told_nothing_about_one() -> None:
    """TZ 5.1: the venue choice and the create-a-venue wizard are onboarding's screens."""
    dispatcher, bot, _ = build_stand()

    payload = OpenSection(section=MenuAction.SCHEDULE).pack()
    await feed(dispatcher, bot, button_update(payload), None)

    assert not edits(bot)
    assert not session_of(bot).sent_texts()
    assert [call.text for call in session_of(bot).answers()] == [texts.ERROR_NOT_ALLOWED]
