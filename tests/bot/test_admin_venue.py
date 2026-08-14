"""The management board, the create-venue wizard and the venue settings (tasks 26 and 30).

Three things are worth testing here and each of them is tested in the cheapest way that can
still fail:

* **the screens**, as pure functions. `src/bot/views/admin.py` returns a `Screen` and sends
  nothing, so the empty state of TZ 8.1 and the "no button leads nowhere" rule are ordinary
  asserts with no bot involved at all;
* **the wizard**, through a real `Dispatcher` with the real callback resolver on it. The
  resolver is included on purpose rather than stubbed: every payload of this block travels
  through it in production, and a button whose factory has no rule would be refused there
  and nowhere else. `VenueService` is a fake with the same signatures — the transaction it
  promises is `tests/services/test_venues.py`'s business, and what this file checks is that
  the handler calls it **once, with what the person typed, and never before the last step**;
* **the settings**, the same way, plus the rule that a press with no rights writes nothing
  (TZ 9). The three edit buttons ride on `TimezoneChoice` today (see
  `src/bot/keyboards/admin.VenueCommand`), which is a factory the resolver lets through
  without an actor — so the check the handler makes for itself is exactly the check that
  cannot be dropped.

Criterion 11.6 is the assertion in `test_the_wizard_raises_a_venue_from_nothing`: the venue
comes up through the interface, and the two templates it comes up with hold zero items —
which is enforced here by the fake having no way to add one at all (decision B1).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import (
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from src.bot import handlers, texts
from src.bot.callbacks import (
    AdminSection,
    MenuAction,
    OpenAdmin,
    OpenSection,
    TimezoneChoice,
    parse,
)
from src.bot.keyboards import admin as keyboards
from src.bot.middlewares.auth import ACTOR_KEY, IDENTITY_KEY
from src.bot.middlewares.resolver import RULES, CallbackResolverMiddleware
from src.bot.middlewares.services import VENUE_WIZARD_KEY
from src.bot.states import SettingsEdit, VenueWizard
from src.bot.views import Screen
from src.bot.views import admin as views
from src.db.models import (
    ChecklistTemplate,
    MemberRole,
    User,
    Venue,
    VenueMember,
    VenueSettings,
)
from src.services.access import AccessContext, Identity
from src.services.venues import (
    FIRST_TEMPLATE_VERSION,
    TIMEZONE_FIELD,
    WIZARD_TEMPLATE_TYPES,
    VenueConfiguration,
    VenueCreation,
    wizard_timezones,
)

from tests.bot.test_middlewares import (
    CHAT_ID,
    MANAGER,
    STAFF,
    STAFF_TELEGRAM_ID,
    VENUE_ID,
    make_bot,
    make_callback,
    make_message,
    make_user,
    session_of,
)

#: Decision A2, the customer's own venue — used as what somebody types, not as delivered data.
VENUE_NAME: Final = "PIMS"
VENUE_CITY: Final = "Moscow"
VENUE_TIMEZONE: Final = "Europe/Moscow"

DEFAULT_LEAD_MINUTES: Final = 10
DEFAULT_START: Final = dt.time(8, 0)
DEFAULT_END: Final = dt.time(23, 0)

OWNER_USER_ID: Final = 42
OWNER_TELEGRAM_ID: Final = STAFF_TELEGRAM_ID


# --------------------------------------------------------------------------------------
# The service this block calls, faked at its own signatures
# --------------------------------------------------------------------------------------


def make_settings(
    *,
    lead_minutes: int = DEFAULT_LEAD_MINUTES,
    start: dt.time = DEFAULT_START,
    end: dt.time = DEFAULT_END,
) -> VenueSettings:
    return VenueSettings(
        venue_id=VENUE_ID,
        opening_checklist_lead_minutes=lead_minutes,
        default_shift_start=start,
        default_shift_end=end,
    )


def make_configuration(**kwargs: Any) -> VenueConfiguration:
    venue = Venue(
        id=VENUE_ID,
        name=VENUE_NAME,
        city=VENUE_CITY,
        timezone=VENUE_TIMEZONE,
        is_active=True,
    )
    return VenueConfiguration(venue=venue, settings=make_settings(**kwargs))


class FakeVenueService:
    """`VenueService` with the same two signatures and nothing behind them.

    It keeps the two rows the settings screen reads, so that a save is visible on the very
    next render — which is how the screen behaves in production, where `update_settings`
    returns the rows it has just written.

    There is deliberately **no way to add a checklist item**: decision B1 says the wizard
    creates both templates empty, and a fake that could not express the opposite is the
    strongest form that assertion takes at this layer.
    """

    def __init__(self, configuration: VenueConfiguration | None = None) -> None:
        current = configuration if configuration is not None else make_configuration()
        self.venue = current.venue
        self.settings = current.settings
        self.creations: list[VenueCreation] = []
        self.created: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def create(
        self,
        owner: User | int,
        *,
        name: str,
        city: str,
        timezone: str,
        default_shift_start: dt.time,
        default_shift_end: dt.time,
    ) -> VenueCreation:
        assert isinstance(owner, User), "the wizard holds the users row it created on /start"
        self.created.append(
            {
                "owner": owner.id,
                "name": name,
                "city": city,
                "timezone": timezone,
                "default_shift_start": default_shift_start,
                "default_shift_end": default_shift_end,
            }
        )
        self.venue = Venue(
            id=VENUE_ID,
            name=name,
            city=city,
            timezone=timezone,
            is_active=True,
        )
        self.settings = make_settings(start=default_shift_start, end=default_shift_end)
        member = VenueMember(
            id=1,
            venue_id=VENUE_ID,
            user_id=owner.id,
            role=MemberRole.OWNER,
            is_active=True,
        )
        templates = tuple(
            ChecklistTemplate(
                id=index,
                venue_id=VENUE_ID,
                type=checklist_type,
                name=str(checklist_type),
                version=FIRST_TEMPLATE_VERSION,
                is_active=True,
            )
            for index, checklist_type in enumerate(WIZARD_TEMPLATE_TYPES, start=1)
        )
        creation = VenueCreation(
            venue=self.venue,
            settings=self.settings,
            member=member,
            templates=templates,
            owner=AccessContext(
                user_id=owner.id,
                telegram_id=owner.telegram_id,
                venue_id=VENUE_ID,
                member_id=member.id,
                role=MemberRole.OWNER,
                full_name=owner.full_name,
            ),
        )
        self.creations.append(creation)
        return creation

    async def update_settings(self, actor: AccessContext, **fields: Any) -> VenueConfiguration:
        assert actor.is_manager, "the real service refuses anybody below manager (TZ 2)"
        if fields:
            self.updates.append(dict(fields))
        for field, value in fields.items():
            if field == TIMEZONE_FIELD:
                self.venue.timezone = value
            else:
                setattr(self.settings, field, value)
        return VenueConfiguration(venue=self.venue, settings=self.settings)


# --------------------------------------------------------------------------------------
# The stand
# --------------------------------------------------------------------------------------


def bootstrap_owner(*, may_create_venue: bool = True) -> Identity:
    """Decision A3: `/start` made the `users` row, and no membership exists yet."""
    return Identity(
        user=make_user(OWNER_USER_ID, telegram_id=OWNER_TELEGRAM_ID),
        contexts=(),
        active=None,
        may_create_venue=may_create_venue,
    )


class Stand:
    """A dispatcher holding the two routers of this block, and the FSM behind them."""

    def __init__(
        self,
        *,
        actor: AccessContext | None = None,
        identity: Identity | None = None,
        service: FakeVenueService | None = None,
    ) -> None:
        self.bot: Bot = make_bot()
        storage = MemoryStorage()
        self.dispatcher = Dispatcher(storage=storage)
        # The real resolver: every payload of this block is parsed and vetted by it in
        # production, and a factory with no rule is refused there rather than here.
        self.dispatcher.callback_query.outer_middleware(CallbackResolverMiddleware())
        self.dispatcher.include_router(handlers.admin.router())
        self.dispatcher.include_router(handlers.admin_venue.router())
        self.service = service if service is not None else FakeVenueService()
        self.context: dict[str, Any] = {
            ACTOR_KEY: actor,
            IDENTITY_KEY: identity if identity is not None else bootstrap_owner(),
            VENUE_WIZARD_KEY: self.service,
        }
        self.state = FSMContext(
            storage=storage,
            key=StorageKey(bot_id=self.bot.id, chat_id=CHAT_ID, user_id=STAFF_TELEGRAM_ID),
        )

    async def types(self, text: str) -> None:
        await self.dispatcher.feed_update(
            self.bot,
            Update(update_id=1, message=make_message(text)),
            **self.context,
        )

    async def presses(self, payload: str) -> None:
        await self.dispatcher.feed_update(
            self.bot,
            Update(update_id=1, callback_query=make_callback(payload)),
            **self.context,
        )

    # -- reading the bot back -----------------------------------------------------------

    def sent(self) -> list[SendMessage]:
        return [call for call in session_of(self.bot).calls if isinstance(call, SendMessage)]

    def alerts(self) -> list[str | None]:
        return [
            call.text
            for call in session_of(self.bot).calls
            if isinstance(call, AnswerCallbackQuery)
        ]

    def _last_screen(self) -> SendMessage | EditMessageText:
        """The last screen, whether it was sent or edited into place.

        Both shapes appear in one flow on purpose: a button press edits the message it was
        pressed on, and an answer that was typed cannot edit anything (see `deliver()` in
        `src/bot/handlers/admin.py`).
        """
        shown = [
            call
            for call in session_of(self.bot).calls
            if isinstance(call, SendMessage | EditMessageText)
        ]
        assert shown, "nothing was shown at all"
        return shown[-1]

    def screen(self) -> str:
        return str(self._last_screen().text)

    def keyboard(self) -> InlineKeyboardMarkup:
        markup = self._last_screen().reply_markup
        assert isinstance(markup, InlineKeyboardMarkup)
        return markup


def zone_index(name: str) -> int:
    return wizard_timezones().index(name)


async def run_wizard_to_the_window(stand: Stand) -> None:
    """Everything up to the last step, which is the only one that writes."""
    await stand.presses(keyboards.command(keyboards.VenueCommand.CREATE))
    await stand.types(VENUE_NAME)
    await stand.types(VENUE_CITY)
    await stand.presses(TimezoneChoice(index=zone_index(VENUE_TIMEZONE)).pack())


async def open_settings(stand: Stand) -> None:
    await stand.presses(OpenAdmin(section=AdminSection.SETTINGS).pack())


# --------------------------------------------------------------------------------------
# The board (TZ 5.8)
# --------------------------------------------------------------------------------------


def payloads_of(screen: Screen) -> list[str]:
    assert isinstance(screen.markup, InlineKeyboardMarkup)
    return [
        str(button.callback_data)
        for row in screen.markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


def test_the_board_draws_the_five_blocks_of_stage_zero() -> None:
    screen = views.board()
    assert screen.text == texts.ADMIN_TITLE
    blocks = [parse(payload) for payload in payloads_of(screen)]
    opened = [entry.section for entry in blocks if isinstance(entry, OpenAdmin)]
    assert opened == list(AdminSection), "every block of TZ 5.8 that stage 0 built, in order"
    assert len(opened) == len(keyboards.BLOCKS) == 5


def test_the_board_sits_one_step_from_the_menu() -> None:
    """Two ways home on the same screen is the confusion `submenu(back=False)` prevents."""
    assert isinstance(views.board().markup, InlineKeyboardMarkup)
    captions = [
        button.text
        for row in views.board().markup.inline_keyboard  # type: ignore[union-attr]
        for button in row
    ]
    assert captions[-1] == texts.HOME_BUTTON
    assert texts.BACK_BUTTON not in captions


@pytest.mark.parametrize(
    "screen",
    [
        views.board(),
        views.no_venue_yet(),
        views.name_step(),
        views.city_step(),
        views.timezone_step(wizard_timezones()),
        views.window_step(),
        views.settings(make_configuration()),
        views.settings_timezone(wizard_timezones()),
        views.settings_lead_minutes(),
        views.settings_window(),
    ],
    ids=[
        "board",
        "empty state",
        "name",
        "city",
        "timezone",
        "window",
        "settings",
        "settings timezone",
        "settings lead",
        "settings window",
    ],
)
def test_no_screen_of_this_block_has_a_button_leading_nowhere(screen: Screen) -> None:
    """TZ 8.1: a caption with nothing behind it is worse than no caption at all.

    "Somewhere" is checked twice over, because a payload can be well-formed and still reach
    nothing: it has to parse into a factory of `src/bot/callbacks.py`, and that factory has
    to carry a rule in the resolver — without one the press is refused before any router.
    """
    for payload in payloads_of(screen):
        assert type(parse(payload)) in RULES, f"{payload!r} would be refused by the resolver"


async def test_the_board_answers_the_caption_of_the_reply_keyboard() -> None:
    stand = Stand(actor=MANAGER)
    await stand.types(texts.MENU_MANAGEMENT_BUTTON)
    assert [str(call.text) for call in stand.sent()] == [texts.ADMIN_TITLE]


async def test_the_board_answers_its_own_inline_button() -> None:
    stand = Stand(actor=MANAGER)
    await stand.presses(OpenSection(section=MenuAction.MANAGEMENT).pack())
    assert stand.screen() == texts.ADMIN_TITLE


async def test_a_bartender_who_types_the_caption_is_refused() -> None:
    """TZ 9: rights are checked on the server; the menu simply does not draw the caption."""
    stand = Stand(actor=STAFF)
    await stand.types(texts.MENU_MANAGEMENT_BUTTON)
    assert [str(call.text) for call in stand.sent()] == [texts.ERROR_NOT_ALLOWED]


# --------------------------------------------------------------------------------------
# The wizard (task 26, criterion 11.6)
# --------------------------------------------------------------------------------------


def test_the_empty_state_is_the_offer_to_create_a_venue() -> None:
    """TZ 8.1 for the whole product: nothing exists, and one button ends that."""
    screen = views.no_venue_yet()
    assert screen.text == texts.ONBOARDING_CREATE_VENUE_OFFER
    assert payloads_of(screen) == [keyboards.command(keyboards.VenueCommand.CREATE)]
    assert isinstance(screen.markup, InlineKeyboardMarkup)
    captions = [button.text for row in screen.markup.inline_keyboard for button in row]
    assert captions == [texts.ONBOARDING_CREATE_VENUE_BUTTON], (
        "no home and no back: whoever sees this screen belongs to no venue (decision A3)"
    )


async def test_the_wizard_raises_a_venue_from_nothing() -> None:
    """Criterion 11.6, end to end: name, city, zone, window — one call, and zero items."""
    stand = Stand()
    await run_wizard_to_the_window(stand)
    assert stand.service.created == [], "nothing is written before the last step"

    await stand.types("08:00-23:00")

    assert stand.service.created == [
        {
            "owner": OWNER_USER_ID,
            "name": VENUE_NAME,
            "city": VENUE_CITY,
            "timezone": VENUE_TIMEZONE,
            "default_shift_start": DEFAULT_START,
            "default_shift_end": DEFAULT_END,
        }
    ]
    creation = stand.service.creations[0]
    assert creation.member.role is MemberRole.OWNER
    assert creation.owner.role is MemberRole.OWNER, "decision D3: a real membership, not a flag"
    assert [template.type for template in creation.templates] == list(WIZARD_TEMPLATE_TYPES)
    assert all(template.version == FIRST_TEMPLATE_VERSION for template in creation.templates)
    assert await stand.state.get_state() is None, "the scenario is over, not abandoned"


async def test_the_finished_wizard_hands_over_the_owner_s_main_menu() -> None:
    stand = Stand()
    await run_wizard_to_the_window(stand)
    await stand.types("08:00-23:00")

    last = stand.sent()[-1]
    assert str(last.text) == texts.VENUE_CREATED_TEMPLATE.format(venue=VENUE_NAME)
    assert isinstance(last.reply_markup, ReplyKeyboardMarkup)
    captions = [button.text for row in last.reply_markup.keyboard for button in row]
    assert texts.MENU_MANAGEMENT_BUTTON in captions, "TZ 5.2: an owner sees the section"


async def test_the_zone_is_pressed_and_never_typed() -> None:
    stand = Stand()
    await stand.presses(keyboards.command(keyboards.VenueCommand.CREATE))
    await stand.types(VENUE_NAME)
    await stand.types(VENUE_CITY)

    assert await stand.state.get_state() == VenueWizard.timezone.state
    offered = [
        parse(str(button.callback_data))
        for row in stand.keyboard().inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    zones = [entry for entry in offered if isinstance(entry, TimezoneChoice) and entry.index >= 0]
    assert len(zones) == len(wizard_timezones())


@pytest.mark.parametrize(
    "typed",
    ["", "08:00", "вечером", "25:00-26:00"],
    ids=["nothing", "one time", "words", "not a clock"],
)
async def test_a_window_the_wizard_cannot_read_asks_again(typed: str) -> None:
    """TZ 8.2: a wizard that cannot read an answer re-asks; it never half-writes a venue."""
    stand = Stand()
    await run_wizard_to_the_window(stand)

    await stand.types(typed)

    assert stand.service.created == []
    assert await stand.state.get_state() == VenueWizard.window.state
    assert stand.screen().endswith(texts.VENUE_WINDOW_PROMPT)


async def test_a_zone_button_that_names_nothing_re_draws_the_step() -> None:
    """A retyped `callback_data` is an ordinary event, not a traceback (TZ 9)."""
    stand = Stand()
    await stand.presses(keyboards.command(keyboards.VenueCommand.CREATE))
    await stand.types(VENUE_NAME)
    await stand.types(VENUE_CITY)

    await stand.presses(TimezoneChoice(index=10_000).pack())

    assert await stand.state.get_state() == VenueWizard.timezone.state
    assert stand.screen().endswith(texts.VENUE_TIMEZONE_PROMPT)
    assert stand.service.created == []


async def test_a_blank_answer_does_not_become_the_name_of_a_venue() -> None:
    stand = Stand()
    await stand.presses(keyboards.command(keyboards.VenueCommand.CREATE))
    await stand.types("   ")
    assert await stand.state.get_state() == VenueWizard.name.state
    assert stand.screen().endswith(texts.VENUE_NAME_PROMPT)


async def test_the_wizard_restarts_when_its_answers_are_gone() -> None:
    """The FSM data outlived its TTL, or was cleared underneath. Re-ask, never guess."""
    stand = Stand()
    await stand.state.set_state(VenueWizard.window)
    await stand.types("08:00-23:00")
    assert stand.service.created == []
    assert await stand.state.get_state() == VenueWizard.name.state


async def test_nobody_but_the_bootstrap_owner_may_start_the_wizard() -> None:
    """Decision A3 is a one-off trigger, and TZ 9 checks it on the server."""
    stand = Stand(actor=MANAGER, identity=bootstrap_owner(may_create_venue=False))
    await stand.presses(keyboards.command(keyboards.VenueCommand.CREATE))
    assert await stand.state.get_state() is None
    assert stand.alerts() == [texts.ERROR_NOT_ALLOWED]


# --------------------------------------------------------------------------------------
# The settings (task 30)
# --------------------------------------------------------------------------------------


def test_the_settings_screen_shows_the_three_values_of_the_section() -> None:
    screen = views.settings(make_configuration(lead_minutes=15))
    assert texts.SETTINGS_TITLE in screen.text
    assert VENUE_TIMEZONE in screen.text
    assert texts.SETTINGS_MINUTES_TEMPLATE.format(value=15) in screen.text
    assert texts.TIME_RANGE_TEMPLATE.format(start="08:00", end="23:00") in screen.text


async def test_the_settings_screen_opens_from_the_board() -> None:
    stand = Stand(actor=MANAGER)
    await open_settings(stand)
    assert stand.screen() == views.settings(make_configuration()).text
    assert stand.service.updates == [], "opening a screen writes nothing"


async def test_the_lead_time_is_typed_and_saved() -> None:
    """Task 30, and the number test 37 later moves the push by."""
    stand = Stand(actor=MANAGER)
    await open_settings(stand)

    await stand.presses(keyboards.command(keyboards.VenueCommand.EDIT_LEAD_MINUTES))
    assert await stand.state.get_state() == SettingsEdit.lead_minutes.state
    assert stand.screen().endswith(texts.SETTINGS_CHECKLIST_LEAD_PROMPT)

    await stand.types("25")

    assert stand.service.updates == [{"opening_checklist_lead_minutes": 25}]
    assert await stand.state.get_state() is None
    assert texts.SETTINGS_MINUTES_TEMPLATE.format(value=25) in stand.screen()


@pytest.mark.parametrize(
    "typed", ["", "потом", "-5", "10 минут"], ids=["nothing", "a word", "minus", "with a unit"]
)
async def test_a_lead_time_that_is_not_a_number_asks_again(typed: str) -> None:
    stand = Stand(actor=MANAGER)
    await stand.presses(keyboards.command(keyboards.VenueCommand.EDIT_LEAD_MINUTES))
    await stand.types(typed)
    assert stand.service.updates == []
    assert await stand.state.get_state() == SettingsEdit.lead_minutes.state
    assert stand.screen().endswith(texts.SETTINGS_CHECKLIST_LEAD_PROMPT)


async def test_the_default_window_moves_as_a_pair() -> None:
    stand = Stand(actor=MANAGER)
    await stand.presses(keyboards.command(keyboards.VenueCommand.EDIT_WINDOW))
    assert await stand.state.get_state() == SettingsEdit.window.state

    await stand.types("09:00 - 22:30")

    assert stand.service.updates == [
        {"default_shift_start": dt.time(9, 0), "default_shift_end": dt.time(22, 30)}
    ]
    assert texts.TIME_RANGE_TEMPLATE.format(start="09:00", end="22:30") in stand.screen()


async def test_an_unreadable_window_leaves_the_settings_alone() -> None:
    stand = Stand(actor=MANAGER)
    await stand.presses(keyboards.command(keyboards.VenueCommand.EDIT_WINDOW))
    await stand.types("когда как")
    assert stand.service.updates == []
    assert await stand.state.get_state() == SettingsEdit.window.state


async def test_the_timezone_is_picked_from_the_same_list_the_wizard_offers() -> None:
    stand = Stand(actor=MANAGER)
    await open_settings(stand)
    await stand.presses(keyboards.command(keyboards.VenueCommand.EDIT_TIMEZONE))
    assert stand.screen().endswith(texts.VENUE_TIMEZONE_PROMPT)

    chosen = wizard_timezones()[3]
    await stand.presses(TimezoneChoice(index=3).pack())

    assert stand.service.updates == [{TIMEZONE_FIELD: chosen}]
    assert chosen in stand.screen()
    assert await stand.state.get_state() is None, "the zone needs no step of its own"


async def test_a_zone_index_that_names_nothing_changes_no_setting() -> None:
    stand = Stand(actor=MANAGER)
    await stand.presses(TimezoneChoice(index=10_000).pack())
    assert stand.service.updates == []
    assert stand.screen().endswith(texts.VENUE_TIMEZONE_PROMPT)


@pytest.mark.parametrize(
    "command",
    [
        keyboards.VenueCommand.EDIT_TIMEZONE,
        keyboards.VenueCommand.EDIT_LEAD_MINUTES,
        keyboards.VenueCommand.EDIT_WINDOW,
    ],
    ids=lambda value: str(value.name).lower(),
)
async def test_a_bartender_cannot_edit_the_settings_of_the_venue(
    command: keyboards.VenueCommand,
) -> None:
    """TZ 9. These three payloads ride on a factory the resolver waves through with no
    actor (`TimezoneChoice`), so this handler's own check is the only one there is."""
    stand = Stand(actor=STAFF)
    await stand.presses(keyboards.command(command))
    assert stand.service.updates == []
    assert await stand.state.get_state() is None
    assert stand.alerts() == [texts.ERROR_NOT_ALLOWED]


async def test_a_press_with_no_membership_at_all_is_refused() -> None:
    stand = Stand(actor=None, identity=bootstrap_owner(may_create_venue=False))
    await stand.presses(keyboards.command(keyboards.VenueCommand.EDIT_LEAD_MINUTES))
    assert stand.service.updates == []
    assert stand.alerts() == [texts.ERROR_NOT_ALLOWED]


# --------------------------------------------------------------------------------------
# Reading what a manager typed
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("08:00-23:00", (dt.time(8, 0), dt.time(23, 0))),
        ("08:00 – 23:00", (dt.time(8, 0), dt.time(23, 0))),  # noqa: RUF001 - the dash is the case
        ("8:00 и 23:00", (dt.time(8, 0), dt.time(23, 0))),
        ("18.00 02.00", (dt.time(18, 0), dt.time(2, 0))),
        ("08:00", None),
        ("08:00-23:00-01:00", None),
        ("24:00-25:00", None),
        ("", None),
        (None, None),
    ],
    ids=[
        "hyphen",
        "en dash",
        "a word between",
        "dots",
        "one reading",
        "three readings",
        "no such hour",
        "empty",
        "no text at all",
    ],
)
def test_a_shift_window_is_read_whatever_separates_the_two_times(
    typed: str | None, expected: tuple[dt.time, dt.time] | None
) -> None:
    """The prompt's own example puts a word between the times, so the parser must not care."""
    assert handlers.admin_venue.window_in(typed) == expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("25", 25), (" 0 ", 0), ("-5", None), ("25 минут", None), ("", None), (None, None)],
    ids=["a number", "zero", "negative", "with a unit", "empty", "no text at all"],
)
def test_a_lead_time_is_a_bare_count_of_minutes(typed: str | None, expected: int | None) -> None:
    assert handlers.admin_venue.minutes_in(typed) == expected
