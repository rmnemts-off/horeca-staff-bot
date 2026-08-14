"""The dispatcher: what is registered, in what order, and what it does (plan, task 20).

Three things are checked here that no single middleware can be asked about on its own:

* **the assembly** — the middlewares are registered in the documented order. Order is the
  design of this package (a gate after the venue context would scope a stranger's update),
  so it is asserted rather than described;
* **the interruption of TZ 5.2**, through a real `Dispatcher` with a wizard behind it: a
  main-menu caption reaches the section that owns it, and the wizard's own state-filtered
  handler never sees the caption as its next answer. That last half is the whole reason
  the interception is a middleware rather than a router — see `src/bot/middlewares/menu.py`
  — and `test_a_menu_press_over_an_empty_scenario_asks_nothing` is where a router-shaped
  implementation fails;
* **`safe_edit`**, on all three answers Telegram gives, plus the one it does not: an
  unexpected error still closes the spinner (TZ 9).

The FSM store is asserted twice: its shape without a Redis (both TTLs set, one key prefix)
and its promise with one — a state written by one process is read by the next, which is
what "surviving a restart" means and the reason `MemoryStorage` is not an option. The live
half carries `@pytest.mark.db`: CI has Redis, a laptop may not.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Final

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, Update
from src.bot import handlers, routers, texts
from src.bot.callbacks import Callback, MenuAction, MenuKeep, OpenSection
from src.bot.dispatcher import (
    FSM_KEY_PREFIX,
    FSM_TTL,
    build_bot,
    build_dispatcher,
    build_storage,
)
from src.bot.middlewares.activity import LastSeenMiddleware
from src.bot.middlewares.auth import ACTOR_KEY, AuthMiddleware
from src.bot.middlewares.errors import ErrorsMiddleware
from src.bot.middlewares.menu import MenuInterceptMiddleware, interrupt_keyboard
from src.bot.middlewares.resolver import CallbackResolverMiddleware
from src.bot.middlewares.services import ServicesMiddleware
from src.bot.middlewares.throttling import ThrottlingMiddleware
from src.bot.middlewares.venue import VenueContextMiddleware
from src.bot.safe_edit import EditOutcome, safe_edit
from src.bot.states import VenueWizard
from src.config import settings

from tests.bot.test_middlewares import (
    CHAT_ID,
    STAFF,
    STAFF_TELEGRAM_ID,
    make_bot,
    make_callback,
    make_message,
    session_of,
)

MENU_CAPTION: Final = texts.MENU_SHIFT_BUTTON
HIDDEN_CAPTION: Final = texts.MENU_WRITEOFF_BUTTON


#: A step-by-step scenario, standing in for every wizard of TZ 5.8.
#:
#: The real `VenueWizard` and not a group declared here, because the interception of TZ 5.2
#: asks about a scenario only when `src/bot/states/` says that scenario holds unsaved input
#: (:func:`~src.bot.states.keeps_unsaved_input`). A stand-in declared in a test file is not
#: in that set, so it would exercise the *screen* branch while claiming to exercise the
#: wizard one — green, and about the wrong half of the rule.
Wizard: Final = VenueWizard


# --------------------------------------------------------------------------------------
# The assembly
# --------------------------------------------------------------------------------------


def test_the_middlewares_are_registered_in_the_documented_order() -> None:
    dispatcher = build_dispatcher(storage=MemoryStorage())
    ours = [
        type(middleware)
        for middleware in dispatcher.update.outer_middleware._middlewares
        if type(middleware).__module__.startswith("src.")
    ]
    assert ours == [
        ErrorsMiddleware,
        ThrottlingMiddleware,
        ServicesMiddleware,
        AuthMiddleware,
        VenueContextMiddleware,
        LastSeenMiddleware,
        MenuInterceptMiddleware,
    ], "the order is the design: see src/bot/middlewares/__init__.py"


def test_the_resolver_is_registered_on_callback_queries() -> None:
    dispatcher = build_dispatcher(storage=MemoryStorage())
    registered = [
        type(middleware) for middleware in dispatcher.callback_query.outer_middleware._middlewares
    ]
    assert CallbackResolverMiddleware in registered


def test_the_interception_does_not_depend_on_the_order_of_the_routers() -> None:
    """TZ 5.2: a main-menu press outranks the handler waiting for the next wizard step.

    It is a middleware, so the guarantee does not rest on any router being included first
    — which is what `test_a_menu_press_over_an_empty_scenario_asks_nothing` exercises with
    the wizard router deliberately in front of the sections.
    """
    dispatcher = build_dispatcher(storage=MemoryStorage())
    assert [router.name for router in dispatcher.sub_routers] == list(routers.router_names())
    # And the tree is the one `src/bot/handlers/__init__.py` describes: onboarding owns the
    # way in (TZ 5.1) and is therefore first, whatever else is added after it.
    assert dispatcher.sub_routers[0].name == "onboarding"


#: Factories that nothing in the router tree answers, and why. Anything not named here has
#: to have a handler — see :func:`test_every_callback_factory_is_answered_by_somebody`.
UNROUTED: Final = {
    # Answered by `src/bot/middlewares/menu.py`, above routing, so that "carry on" cannot be
    # outranked by a state-filtered handler. It is drawn by that middleware and nothing else.
    "MenuKeep": "handled by MenuInterceptMiddleware, not by a router",
    # The five `Editor*` factories left this list with plan task 28 and the five `Shift*`
    # ones with task 29: `admin_checklists.py` and `admin_schedule.py` now register a
    # handler for each of them.
}


def _routed_factories(router: Router) -> set[str]:
    """Every callback factory some handler of this tree filters on."""
    found: set[str] = set()
    for handler in router.callback_query.handlers:
        for filter_object in handler.filters or ():
            factory = getattr(filter_object.callback, "callback_data", None)
            if isinstance(factory, type):
                found.add(factory.__name__)
    for sub in router.sub_routers:
        found |= _routed_factories(sub)
    return found


def test_every_callback_factory_is_answered_by_somebody() -> None:
    """A factory with a rule and no handler is a button that spins and does nothing.

    `test_middlewares.py::test_every_callback_factory_has_a_rule` guards the other half —
    that the resolver knows how to check a payload. Both halves are needed and neither
    implies the other: `VenueCreate` had a rule from the day it was declared, was drawn on
    the first screen a new installation ever shows (decision A3, the bootstrap owner with no
    venue), and for the length of one wave no router filtered on it, because the module that
    drew the button and the module that owned the wizard were written by different people
    against different stand-ins. Every per-module test was green — a keyboard test asserts
    the payload it packs, a handler test presses the payload it registers, and neither can
    see that the two are not the same payload. Only the assembled tree can.
    """
    dispatcher = build_dispatcher(storage=MemoryStorage())
    routed = _routed_factories(dispatcher)
    dangling = {
        name: reason
        for factory in Callback.__subclasses__()
        if (name := factory.__name__) not in routed
        and (reason := UNROUTED.get(name, "nothing answers it")) == "nothing answers it"
    }
    assert not dangling, (
        f"drawn but unanswered: {sorted(dangling)} — either register a handler, or add the "
        "factory to UNROUTED with the task that owns it"
    )
    # And the allowlist does not outlive what it excuses: a factory that has since been
    # wired up must leave it, or the next dangling button hides behind a stale entry.
    assert not (stale := sorted(set(UNROUTED) & routed)), f"UNROUTED is stale for {stale}"


def test_the_bot_parses_html() -> None:
    """The parse mode the screens are written in, asserted because both directions break.

    Off, and `INVITE_READY_TEMPLATE`'s `<code>` arrives as four literal characters while the
    quoting in `src/bot/views/` turns «Rum & Cola» into «Rum &amp; Cola» on the screen. On,
    and every screen has to quote — which is what `tests/bot/test_views_html.py` checks, and
    what it would go on checking against nothing at all if this line were quietly dropped.
    """
    bot = build_bot("42:test-token")
    assert bot.default.parse_mode == ParseMode.HTML


# --------------------------------------------------------------------------------------
# The FSM store (TZ 3.1)
# --------------------------------------------------------------------------------------


def test_the_store_expires_both_keys() -> None:
    """One of the two expiring alone leaves a scenario with data and no step."""
    storage = build_storage("redis://localhost:6379/0")
    assert isinstance(storage, RedisStorage)
    assert storage.state_ttl == FSM_TTL
    assert storage.data_ttl == FSM_TTL
    key = StorageKey(bot_id=42, chat_id=CHAT_ID, user_id=STAFF_TELEGRAM_ID)
    assert storage.key_builder.build(key, "state").startswith(FSM_KEY_PREFIX)


def test_the_ttl_outlives_the_longest_shift() -> None:
    """Decision A2: the customer's venue works 08:00-23:00, which is fifteen hours."""
    assert dt.timedelta(hours=15) < FSM_TTL


async def _live_storage() -> RedisStorage:
    """A store on the configured Redis, or a skip.

    The marker is `db` because that is the one CI runs with the infrastructure up; the
    probe is here because a laptop can have PostgreSQL and no Redis, and a missing server
    is a skip, not a failure (CLAUDE.md).
    """
    url = settings.redis_url.strip()
    if not url:
        pytest.skip("REDIS_URL is not set")
    storage = build_storage(url)
    try:
        await storage.redis.ping()
    except Exception:
        # Any connection failure means there is no Redis here, which is a skip.
        await storage.close()
        pytest.skip(f"Redis at {url!r} is not reachable")
    return storage


@pytest.mark.db
async def test_the_fsm_state_survives_a_restart_of_the_bot() -> None:
    """A deploy in the middle of a shift must not drop a half-filled wizard (TZ 3.1)."""
    key = StorageKey(bot_id=42, chat_id=abs(hash(uuid.uuid4())) % 10**9, user_id=STAFF_TELEGRAM_ID)

    before = await _live_storage()
    try:
        await before.set_state(key, Wizard.name)
        await before.set_data(key, {"full_name": "Иван"})
    finally:
        # The process is gone: connections closed, nothing kept in memory.
        await before.close()

    after = build_storage(settings.redis_url.strip())
    try:
        assert await after.get_state(key) == Wizard.name.state
        assert await after.get_data(key) == {"full_name": "Иван"}
        # ... and the state is not immortal: both keys carry the TTL.
        for part in ("state", "data"):
            ttl = await after.redis.ttl(after.key_builder.build(key, part))
            assert 0 < ttl <= FSM_TTL.total_seconds()
        await after.set_state(key, None)
        await after.set_data(key, {})
    finally:
        await after.close()


# --------------------------------------------------------------------------------------
# The interception of a main-menu press (TZ 5.2)
# --------------------------------------------------------------------------------------


class Sections:
    """The routers of TZ 5.3-5.8, standing in for handlers that do not exist yet."""

    def __init__(self) -> None:
        self.router = Router(name="sections")
        self.wizard = Router(name="wizard")
        self.opened: list[str] = []
        self.typed: list[str] = []

        @self.wizard.message(Wizard.name)
        async def wizard_step(message: Message, **data: Any) -> None:
            self.typed.append(message.text or "")

        @self.router.message()
        async def section_by_caption(message: Message, **data: Any) -> None:
            self.opened.append(message.text or "")

        @self.router.callback_query(OpenSection.filter())
        async def section_by_button(callback: CallbackQuery, **data: Any) -> None:
            self.opened.append(str(callback.data))
            await callback.answer()


def build_stand() -> tuple[Dispatcher, Bot, Sections, FSMContext]:
    """A dispatcher with the interrupting router in front of a wizard and the sections."""
    bot = make_bot()
    storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    interception = MenuInterceptMiddleware()
    dispatcher.update.outer_middleware(interception)
    sections = Sections()
    # The wizard goes in *first*, which is the arrangement a router-shaped interceptor
    # cannot survive: its state filter is checked against a `raw_state` resolved before
    # any router ran.
    dispatcher.include_router(sections.wizard)
    dispatcher.include_router(sections.router)
    state = FSMContext(
        storage=storage,
        key=StorageKey(bot_id=bot.id, chat_id=CHAT_ID, user_id=STAFF_TELEGRAM_ID),
    )
    return dispatcher, bot, sections, state


def message_update(text: str) -> Update:
    return Update(update_id=1, message=make_message(text))


def callback_update(payload: str, *, screen_markup: InlineKeyboardMarkup | None = None) -> Update:
    """A button press. `screen_markup` is the keyboard of the message it was pressed on:
    the confirmation of TZ 5.2 is recognised by being pressed on the question itself."""
    return Update(update_id=1, callback_query=make_callback(payload, screen_markup=screen_markup))


async def feed(dispatcher: Dispatcher, bot: Bot, update: Update) -> Any:
    return await dispatcher.feed_update(bot, update, **{ACTOR_KEY: STAFF})


async def test_a_menu_press_outside_a_scenario_reaches_its_section() -> None:
    dispatcher, bot, sections, _ = build_stand()
    await feed(dispatcher, bot, message_update(MENU_CAPTION))
    assert sections.opened == [MENU_CAPTION]


async def test_a_menu_press_over_an_empty_scenario_asks_nothing() -> None:
    """TZ 5.2: the question is «only if there is unsaved data»."""
    dispatcher, bot, sections, state = build_stand()
    await state.set_state(Wizard.name)

    await feed(dispatcher, bot, message_update(MENU_CAPTION))

    assert sections.opened == [MENU_CAPTION]
    assert await state.get_state() is None, "the abandoned scenario is gone"
    assert not session_of(bot).sent_texts()


async def test_a_menu_press_over_unsaved_data_asks_first() -> None:
    dispatcher, bot, sections, state = build_stand()
    await state.set_state(Wizard.name)
    await state.update_data(full_name="Иван")

    await feed(dispatcher, bot, message_update(MENU_CAPTION))

    assert sections.opened == [], "the section must not open until the question is answered"
    assert sections.typed == [], "and the wizard must not read the caption as an answer"
    assert session_of(bot).sent_texts() == [texts.INTERRUPT_QUESTION]
    assert await state.get_state() == Wizard.name.state
    assert await state.get_data() == {"full_name": "Иван"}


async def test_the_question_offers_the_section_itself_and_a_way_back() -> None:
    dispatcher, bot, _, state = build_stand()
    await state.set_state(Wizard.name)
    await state.update_data(full_name="Иван")

    await feed(dispatcher, bot, message_update(MENU_CAPTION))

    sent = [call for call in session_of(bot).calls if isinstance(call, SendMessage)]
    markup = sent[0].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    payloads = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert payloads == [
        OpenSection(section=MenuAction.MY_SHIFT).pack(),
        MenuKeep().pack(),
    ]


async def test_confirming_drops_the_scenario_and_opens_the_section() -> None:
    dispatcher, bot, sections, state = build_stand()
    await state.set_state(Wizard.name)
    await state.update_data(full_name="Иван")
    payload = OpenSection(section=MenuAction.MY_SHIFT).pack()

    # Pressed on the question itself, which is what makes it the answer to it rather than
    # a fresh menu press — the case `tests/bot/test_middlewares.py` covers on its own.
    await feed(
        dispatcher,
        bot,
        callback_update(payload, screen_markup=interrupt_keyboard(MenuAction.MY_SHIFT)),
    )

    assert sections.opened == [payload]
    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert "DeleteMessage" in session_of(bot).method_names(), "TZ 5.2: the question is gone"


async def test_carrying_on_leaves_the_scenario_alone() -> None:
    dispatcher, bot, sections, state = build_stand()
    await state.set_state(Wizard.name)
    await state.update_data(full_name="Иван")

    await feed(
        dispatcher,
        bot,
        callback_update(MenuKeep().pack(), screen_markup=interrupt_keyboard(MenuAction.MY_SHIFT)),
    )

    assert sections.opened == []
    assert await state.get_state() == Wizard.name.state
    assert await state.get_data() == {"full_name": "Иван"}
    assert "DeleteMessage" in session_of(bot).method_names()
    assert session_of(bot).answers(), "TZ 9: the spinner is always closed"


async def test_ordinary_input_still_reaches_the_scenario() -> None:
    """The interceptor recognises captions, not everything typed while a wizard is open."""
    dispatcher, bot, sections, state = build_stand()
    await state.set_state(Wizard.name)
    await state.update_data(full_name="Иван")

    await feed(dispatcher, bot, message_update("Пётр Смирнов"))

    assert sections.typed == ["Пётр Смирнов"]
    assert await state.get_state() == Wizard.name.state


async def test_a_caption_stage_zero_does_not_draw_is_not_a_menu_press() -> None:
    """Decision B7: the hidden sections are not rendered, so they are not recognised."""
    dispatcher, bot, sections, state = build_stand()
    await state.set_state(Wizard.name)
    await state.update_data(full_name="Иван")

    await feed(dispatcher, bot, message_update(HIDDEN_CAPTION))

    assert sections.typed == [HIDDEN_CAPTION]
    assert not session_of(bot).sent_texts()


async def test_start_answers_a_member_with_the_menu() -> None:
    bot = make_bot()
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(handlers.onboarding.router())
    await feed(dispatcher, bot, message_update("/start"))
    assert session_of(bot).sent_texts() == [texts.MENU_PROMPT]


# --------------------------------------------------------------------------------------
# safe_edit (TZ 8.2, 9)
# --------------------------------------------------------------------------------------


def bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SendMessage(chat_id=CHAT_ID, text="x"), message=text)


class Spinner:
    """Stands in for `callback.answer`, and counts how often it was closed."""

    def __init__(self) -> None:
        self.texts: list[str | None] = []

    async def __call__(self, text: str | None = None) -> bool:
        self.texts.append(text)
        return True


class Remembered:
    """Stands in for the service call that writes the new `message_id` down."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def __call__(self, chat_id: int, message_id: int) -> None:
        self.calls.append((chat_id, message_id))


async def test_an_edit_that_changes_the_screen() -> None:
    bot = make_bot()
    spinner = Spinner()
    result = await safe_edit(
        bot,
        chat_id=CHAT_ID,
        message_id=7,
        text="new",
        answer=spinner,
    )
    assert result.outcome is EditOutcome.EDITED
    assert result.message_id == 7
    assert spinner.texts == [None]


async def test_an_identical_edit_is_a_success() -> None:
    """A double tap on a ticked line is not an error (TZ 5.4)."""
    bot = make_bot()
    session_of(bot).fail_with["EditMessageText"] = bad_request(
        "Bad Request: message is not modified"
    )
    spinner = Spinner()
    result = await safe_edit(
        bot, chat_id=CHAT_ID, message_id=7, text="same", answer=spinner, notice="ok"
    )
    assert result.outcome is EditOutcome.UNCHANGED
    assert result.message_id == 7
    assert spinner.texts == ["ok"]
    assert session_of(bot).method_names() == ["EditMessageText"], "nothing new was sent"


async def test_a_swiped_message_is_sent_again_and_written_down() -> None:
    """TZ 5.4: the employee swiped the checklist away, and the run keeps a `message_id`."""
    bot = make_bot()
    session_of(bot).fail_with["EditMessageText"] = bad_request(
        "Bad Request: message to edit not found"
    )
    spinner = Spinner()
    remember = Remembered()
    result = await safe_edit(
        bot,
        chat_id=CHAT_ID,
        message_id=7,
        text="the checklist",
        answer=spinner,
        remember=remember,
    )
    assert result.outcome is EditOutcome.RESENT
    assert result.is_resent
    assert result.message_id != 7
    assert remember.calls == [(CHAT_ID, result.message_id)]
    assert session_of(bot).method_names() == ["EditMessageText", "SendMessage"]
    assert spinner.texts == [None]


async def test_an_unexpected_failure_still_closes_the_spinner() -> None:
    """TZ 9: a button answers within a second, whatever happened behind it."""
    bot = make_bot()
    session_of(bot).fail_with["EditMessageText"] = bad_request("Bad Request: chat not found")
    spinner = Spinner()
    with pytest.raises(TelegramBadRequest):
        await safe_edit(bot, chat_id=CHAT_ID, message_id=7, text="new", answer=spinner)
    assert spinner.texts == [None], "the error middleware apologises; the spinner stops here"


async def test_a_spinner_that_cannot_be_closed_does_not_hide_the_failure() -> None:
    bot = make_bot()
    session_of(bot).fail_with["EditMessageText"] = bad_request("Bad Request: chat not found")

    async def broken(text: str | None = None) -> bool:
        raise TimeoutError

    with pytest.raises(TelegramBadRequest):
        await safe_edit(bot, chat_id=CHAT_ID, message_id=7, text="new", answer=broken)


async def test_an_edit_without_a_callback_needs_no_spinner() -> None:
    bot = make_bot()
    result = await safe_edit(bot, chat_id=CHAT_ID, message_id=7, text="new")
    assert result.outcome is EditOutcome.EDITED
