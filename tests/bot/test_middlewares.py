"""The framework every handler will sit on (plan, task 20).

Four properties are asserted here, and each of them is a rule that stops being true the
moment somebody has to remember it in nine handler modules:

* **an unknown `telegram_id` reaches no handler** (TZ 5.1). Not "is answered politely" —
  the handler is never called at all, and the only thing the stranger is told is that they
  need a code;
* **the venue comes from the membership**, never from the update, and every repository of
  the request is built around it (TZ 3.3);
* **two rate budgets**, because the one number that fits a command does not fit forty
  checklist markers (TZ 9, and the plan spells the numbers out);
* **a callback is an object before it is a handler argument** (TZ 9): parsed once,
  fetched through the venue's repository, checked against the presser's role — and a row
  of the bar next door is refused in exactly the words a deleted row is.

The fake repositories follow the rule of CLAUDE.md: the table is *shared* between venues,
because `checklist_runs` is one table for every venue, and the scope is a `WHERE`. Their
venue predicate is what `test_a_run_of_another_venue_is_invisible` is about; without that
test the predicate would be dead code that could be deleted with everything staying green.

A child table has no predicate of its own to carry, and that case is worth spelling out.
`checklist_items` has no `venue_id` column at all (decision D9), so the real repository
joins the template on every query, a lookup by the child's own id included. The fake does
the same (`FakeChildRows`): a fake that answered a bare id would be one in which a line of
the bar next door is visible, no test could tell the two apart, and a resolver reading
`venue_id` off a row that has none would stay green — which is exactly what happened.

The bot never talks to Telegram: `RecordingSession` is a `BaseSession` that answers every
method locally and keeps what it was asked to send, so a refusal can be read back as "one
`SendMessage` with this text and nothing else".
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, TelegramObject
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession
from src.bot import texts
from src.bot.callbacks import (
    AdminSection,
    ChecklistToggle,
    EditorLine,
    InviteConfirm,
    MemberShow,
    MenuAction,
    MenuKeep,
    Nav,
    NavTarget,
    OpenAdmin,
    OpenSection,
    RecipeShow,
    registered,
)
from src.bot.middlewares.activity import LastSeenMiddleware, is_stale
from src.bot.middlewares.auth import (
    ACTOR_KEY,
    IDENTITY_KEY,
    INVITE_CODE_KEY,
    AuthMiddleware,
    invite_code_in,
)
from src.bot.middlewares.menu import (
    RAW_STATE_KEY,
    STATE_KEY,
    MenuInterceptMiddleware,
    interrupt_keyboard,
)
from src.bot.middlewares.resolver import (
    PAYLOAD_KEY,
    RULES,
    SUBJECT_KEY,
    CallbackResolverMiddleware,
    OwnColumn,
    Refusal,
    Subject,
    SubjectContractError,
    SubjectRepositories,
    ViaParent,
    load_item,
    load_recipe,
    load_run,
    load_template,
    resolve,
)
from src.bot.middlewares.services import ACCESS_KEY, CONTAINER_KEY, SERVICES_KEY, Container
from src.bot.middlewares.throttling import (
    COMMAND_RATE,
    Budget,
    Limiter,
    LruDict,
    ThrottlingMiddleware,
    budget_for,
)
from src.bot.middlewares.venue import VENUE_ID_KEY, VENUE_KEY, VenueContextMiddleware
from src.bot.states import RecipeSearch, VenueWizard
from src.db.models import (
    ChecklistItem,
    ChecklistRun,
    ChecklistTemplate,
    ChecklistType,
    MemberRole,
    Recipe,
    RunStatus,
    User,
    Venue,
    VenueMember,
)
from src.db.repositories.base import CHILD_PARENTS
from src.services.access import AccessContext, Identity, format_invite_code

# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------

BOT_TOKEN: Final = "42:TEST"  # noqa: S105 - not a credential; the API is never reached

VENUE_ID: Final = 1
OTHER_VENUE_ID: Final = 2
CHAT_ID: Final = 555

STAFF_TELEGRAM_ID: Final = 917_323_199
MANAGER_TELEGRAM_ID: Final = 1_672_818_749
STRANGER_TELEGRAM_ID: Final = 777

STAFF_USER_ID: Final = 10
MANAGER_USER_ID: Final = 11
OTHER_USER_ID: Final = 12


# --------------------------------------------------------------------------------------
# A bot that answers itself
# --------------------------------------------------------------------------------------


class RecordingSession(BaseSession):
    """Every Bot API call, answered locally and written down.

    `fail_with` makes one method raise once — how `safe_edit` is shown the two errors
    Telegram sends (`tests/bot/test_dispatcher.py` uses it).
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.fail_with: dict[str, Exception] = {}
        self._message_ids = iter(range(1000, 2000))

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        error = self.fail_with.pop(type(method).__name__, None)
        if error is not None:
            raise error
        if type(method).__name__ in {"SendMessage", "EditMessageText"}:
            return make_message(
                str(getattr(method, "text", "")),
                message_id=next(self._message_ids),
            )
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        # Downloading a file is not part of this framework; declared to satisfy the base
        # class, and an empty stream rather than a raise so the type is honest.
        return
        yield b""

    # -- reading the calls back ---------------------------------------------------------

    def sent_texts(self) -> list[str]:
        return [str(call.text) for call in self.calls if isinstance(call, SendMessage)]

    def answers(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]

    def method_names(self) -> list[str]:
        return [type(call).__name__ for call in self.calls]


def make_bot() -> Bot:
    return Bot(token=BOT_TOKEN, session=RecordingSession())


def session_of(bot: Bot) -> RecordingSession:
    assert isinstance(bot.session, RecordingSession)
    return bot.session


def make_message(
    text: str,
    *,
    telegram_id: int = STAFF_TELEGRAM_ID,
    message_id: int = 1,
    markup: InlineKeyboardMarkup | None = None,
    bot: Bot | None = None,
) -> Message:
    message = Message(
        message_id=message_id,
        date=dt.datetime(2026, 8, 13, 9, 0, tzinfo=dt.UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=TelegramUser(id=telegram_id, is_bot=False, first_name="X"),
        text=text,
        reply_markup=markup,
    )
    return message if bot is None else message.as_(bot)


def make_callback(
    payload: str,
    *,
    telegram_id: int = STAFF_TELEGRAM_ID,
    screen_markup: InlineKeyboardMarkup | None = None,
    bot: Bot | None = None,
) -> CallbackQuery:
    """A button press. `screen_markup` is the keyboard of the message it was pressed on —
    how the interrupt question is told from any other screen (`menu.py`)."""
    query = CallbackQuery(
        id="q1",
        from_user=TelegramUser(id=telegram_id, is_bot=False, first_name="X"),
        chat_instance="ci",
        message=make_message("screen", markup=screen_markup, bot=bot),
        data=payload,
    )
    return query if bot is None else query.as_(bot)


class Handler:
    """A handler that only records that it was reached."""

    def __init__(self, result: Any = "handled") -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    async def __call__(self, event: TelegramObject, data: dict[str, Any]) -> Any:
        self.calls.append(data)
        return self._result

    @property
    def was_called(self) -> bool:
        return bool(self.calls)

    @property
    def context(self) -> dict[str, Any]:
        assert self.calls, "the handler was never reached"
        return self.calls[-1]


class Ticker:
    """A clock the test moves by hand: twenty seconds of tapping without sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------------------
# Model factories (in memory: none of this reaches a database)
# --------------------------------------------------------------------------------------


def make_user(
    user_id: int = STAFF_USER_ID,
    *,
    telegram_id: int = STAFF_TELEGRAM_ID,
    last_seen_at: dt.datetime | None = None,
) -> User:
    return User(
        id=user_id,
        telegram_id=telegram_id,
        full_name=f"user-{user_id}",
        is_active=True,
        last_seen_at=last_seen_at,
    )


def make_venue(venue_id: int = VENUE_ID, *, is_active: bool = True) -> Venue:
    return Venue(
        id=venue_id,
        name=f"venue-{venue_id}",
        city="City",
        timezone="Europe/Moscow",
        is_active=is_active,
    )


def make_member(
    member_id: int, *, venue_id: int = VENUE_ID, user_id: int = STAFF_USER_ID
) -> VenueMember:
    return VenueMember(
        id=member_id,
        venue_id=venue_id,
        user_id=user_id,
        role=MemberRole.STAFF,
        is_active=True,
    )


def make_run(
    run_id: int,
    *,
    venue_id: int = VENUE_ID,
    user_id: int = STAFF_USER_ID,
) -> ChecklistRun:
    return ChecklistRun(
        id=run_id,
        venue_id=venue_id,
        shift_id=None,
        user_id=user_id,
        template_id=1,
        type=ChecklistType.OPENING,
        status=RunStatus.SENT,
        done_items=0,
        total_items=3,
    )


def make_template(template_id: int, *, venue_id: int = VENUE_ID) -> ChecklistTemplate:
    return ChecklistTemplate(
        id=template_id,
        venue_id=venue_id,
        type=ChecklistType.OPENING,
        name=f"template-{template_id}",
        version=1,
        is_active=True,
    )


def make_item(item_id: int, *, template_id: int) -> ChecklistItem:
    """A line of a checklist template — and a row with no `venue_id` at all (decision D9)."""
    return ChecklistItem(
        id=item_id,
        template_id=template_id,
        group_name=None,
        group_index=0,
        order_index=item_id,
        text=f"item-{item_id}",
    )


def make_recipe(recipe_id: int, *, venue_id: int | None = VENUE_ID) -> Recipe:
    return Recipe(
        id=recipe_id,
        venue_id=venue_id,
        name=f"recipe-{recipe_id}",
        aliases=[],
        category="cocktails",
        is_active=True,
    )


def actor(
    role: MemberRole = MemberRole.STAFF,
    *,
    user_id: int = STAFF_USER_ID,
    venue_id: int = VENUE_ID,
    telegram_id: int = STAFF_TELEGRAM_ID,
) -> AccessContext:
    return AccessContext(
        user_id=user_id,
        telegram_id=telegram_id,
        venue_id=venue_id,
        member_id=user_id,
        role=role,
        full_name=f"user-{user_id}",
    )


STAFF: Final = actor()
MANAGER: Final = actor(MemberRole.MANAGER, user_id=MANAGER_USER_ID, telegram_id=MANAGER_TELEGRAM_ID)


def known(user: User, *contexts: AccessContext, active: AccessContext | None = None) -> Identity:
    return Identity(
        user=user,
        contexts=contexts,
        active=active if active is not None else (contexts[0] if len(contexts) == 1 else None),
        may_create_venue=False,
    )


def stranger(*, may_create_venue: bool = False) -> Identity:
    return Identity(user=None, contexts=(), active=None, may_create_venue=may_create_venue)


# --------------------------------------------------------------------------------------
# Fake repositories
# --------------------------------------------------------------------------------------


class FakeAccess:
    """`AccessService.resolve` and nothing else — what the gate actually depends on."""

    def __init__(self, identities: dict[int, Identity] | None = None) -> None:
        self.identities = dict(identities) if identities else {}
        self.asked: list[int] = []

    async def resolve(self, telegram_id: int) -> Identity:
        self.asked.append(telegram_id)
        return self.identities.get(telegram_id, stranger())


class FakeUsers:
    """`UserRepository.touch_last_seen`, recorded."""

    def __init__(self) -> None:
        self.touched: list[tuple[int, dt.datetime]] = []

    async def touch_last_seen(self, user_id: int, moment: dt.datetime) -> None:
        self.touched.append((user_id, moment))


class FakeVenues:
    """`VenueRepository.get`. Global by contract: a venue is not scoped to itself."""

    def __init__(self, venues: Sequence[Venue] = ()) -> None:
        self.venues = list(venues)

    async def get(self, venue_id: int) -> Venue | None:
        return next((row for row in self.venues if row.id == venue_id), None)


class FakeRows:
    """One venue's view of a table every venue writes into (CLAUDE.md, TZ 3.3).

    The list is shared with the neighbouring venue's repository, exactly as the real table
    is; the scope is the predicate below and nothing else. `venue_id IS NULL` is the shared
    BarPoint library, which every venue may read — the `library()` half of the real
    `RecipeRepo`. A table without a `venue_id` column is not this class's business:
    :class:`FakeChildRows` is, and reading the column rather than `getattr`-ing it keeps a
    child row from ever being mistaken for a library row here.
    """

    def __init__(self, venue_id: int, table: list[Any]) -> None:
        self.venue_id = venue_id
        self.table = table

    async def get(self, row_id: int) -> Any:
        return next((row for row in self.table if row.id == row_id and self._visible(row)), None)

    def _visible(self, row: Any) -> bool:
        venue_id = row.venue_id
        return venue_id is None or venue_id == self.venue_id


class FakeChildRows:
    """A child table: no `venue_id` of its own, reached only through its parent (D9).

    `checklist_items` has no venue column at all, so `ChildRepository` joins the template on
    every query — a lookup by the child's own id included, which is exactly what a forged
    `callback_data` is. The fake carries the same join, because the alternative is a fake
    that answers a bare id: in it a line of the bar next door is visible, no test can tell
    the two apart, and a resolver that reads `venue_id` off a row that has none stays green
    (CLAUDE.md, acceptance 11.3).
    """

    def __init__(self, parent_fk: str, parent: FakeRows, table: list[Any]) -> None:
        self.parent_fk = parent_fk
        self.parent = parent
        self.table = table

    async def get(self, row_id: int) -> Any:
        row = next((row for row in self.table if row.id == row_id), None)
        if row is None:
            return None
        parent = await self.parent.get(getattr(row, self.parent_fk))
        return None if parent is None else row


class LeakyChildRows(FakeChildRows):
    """The same table with the join dropped — the repository bug the resolver guards against.

    `ChildRepository.by_id()` carries the join precisely because it is one line away from
    not carrying it. That day is what the resolver's second line of defence is for, and this
    class is the only way to reach that line while the first one still works.
    """

    async def get(self, row_id: int) -> Any:
        return next((row for row in self.table if row.id == row_id), None)


@dataclass
class Tables:
    """The whole database of these tests: one list per table, all venues in each."""

    runs: list[Any] = field(default_factory=list)
    recipes: list[Any] = field(default_factory=list)
    members: list[Any] = field(default_factory=list)
    shifts: list[Any] = field(default_factory=list)
    items: list[Any] = field(default_factory=list)
    templates: list[Any] = field(default_factory=list)


class FakeSubjects:
    """`SubjectRepositories` of one venue, over the shared tables."""

    def __init__(
        self,
        venue_id: int,
        tables: Tables | None = None,
        *,
        leaky_items: bool = False,
    ) -> None:
        self.venue_id = venue_id
        self.tables = tables if tables is not None else Tables()
        self.leaky_items = leaky_items
        self.runs = FakeRows(venue_id, self.tables.runs)
        self.recipes = FakeRows(venue_id, self.tables.recipes)
        self.members = FakeRows(venue_id, self.tables.members)
        self.shifts = FakeRows(venue_id, self.tables.shifts)
        self.templates = FakeRows(venue_id, self.tables.templates)
        # `checklist_items` is a child of `checklist_templates` (decision D9), so its venue
        # is its template's and the join is the whole predicate.
        child = LeakyChildRows if leaky_items else FakeChildRows
        self.items: FakeChildRows = child("template_id", self.templates, self.tables.items)

    def neighbour(self, venue_id: int) -> FakeSubjects:
        """The same tables seen through another venue's repositories (acceptance 11.3)."""
        return FakeSubjects(venue_id, self.tables, leaky_items=self.leaky_items)


class FakeServices:
    """What the resolver reads out of `data["services"]`."""

    def __init__(self, repositories: FakeSubjects) -> None:
        self.repositories = repositories


class StubContainer:
    """The container as the venue middleware uses it.

    `for_venue` is the **real** one: building every service of a venue out of repositories
    scoped to it is the thing under test, and a fake builder would test nothing. The session
    has no bind, so nothing connects and no statement is issued.
    """

    def __init__(self, venues: Sequence[Venue] = ()) -> None:
        self.venues = FakeVenues(venues)
        self.users = FakeUsers()
        self._real = Container(AsyncSession(), bootstrap_owner_ids=frozenset())

    def for_venue(self, venue: Venue) -> Any:
        return self._real.for_venue(venue)


def context_for(
    identity: Identity,
    *,
    container: StubContainer | None = None,
    access: FakeAccess | None = None,
) -> dict[str, Any]:
    """The handler context as it looks after the container middleware has run."""
    return {
        ACCESS_KEY: access if access is not None else FakeAccess(),
        CONTAINER_KEY: container if container is not None else StubContainer(),
        IDENTITY_KEY: identity,
        ACTOR_KEY: identity.active,
    }


# --------------------------------------------------------------------------------------
# The gate (TZ 5.1)
# --------------------------------------------------------------------------------------


async def test_an_unknown_user_reaches_no_handler_and_is_offered_a_code() -> None:
    bot = make_bot()
    handler = Handler()
    access = FakeAccess()
    await AuthMiddleware()(
        handler,
        make_message("/start", telegram_id=STRANGER_TELEGRAM_ID, bot=bot),
        {ACCESS_KEY: access, CONTAINER_KEY: StubContainer()},
    )
    assert not handler.was_called, "TZ 5.1: a stranger must not reach a handler at all"
    assert session_of(bot).sent_texts() == [texts.ONBOARDING_NO_ACCESS]


async def test_a_stranger_is_told_nothing_about_the_bot() -> None:
    """TZ 5.1: «и больше ничего» — one message, and it is the code prompt."""
    bot = make_bot()
    await AuthMiddleware()(
        Handler(),
        make_message("Моя смена", telegram_id=STRANGER_TELEGRAM_ID, bot=bot),
        {ACCESS_KEY: FakeAccess()},
    )
    assert session_of(bot).method_names() == ["SendMessage"]
    assert session_of(bot).sent_texts() == [texts.ONBOARDING_NO_ACCESS]


async def test_a_stranger_pressing_a_button_is_not_told_what_it_was() -> None:
    bot = make_bot()
    handler = Handler()
    await AuthMiddleware()(
        handler,
        make_callback(
            ChecklistToggle(run_id=1, item_id=1).pack(), telegram_id=STRANGER_TELEGRAM_ID, bot=bot
        ),
        {ACCESS_KEY: FakeAccess()},
    )
    assert not handler.was_called
    answers = session_of(bot).answers()
    assert [answer.text for answer in answers] == [texts.ERROR_NOT_ALLOWED]


@pytest.mark.parametrize(
    "text",
    [
        f"/start inv_{format_invite_code(VENUE_ID, 'A7K9QX4M')}",
        f"/start@barpoint_bot inv_{format_invite_code(VENUE_ID, 'A7K9QX4M')}",
        f"/start@barpoint_bot {format_invite_code(VENUE_ID, 'A7K9QX4M')}",
        format_invite_code(VENUE_ID, "A7K9QX4M"),
        f"  {format_invite_code(VENUE_ID, 'a7k9qx4m')}  ",
    ],
    ids=[
        "deeplink",
        "deeplink addressed to the bot",
        "addressed command, bare code",
        "typed",
        "lower-case with spaces",
    ],
)
async def test_an_invite_code_gets_through_the_gate(text: str) -> None:
    """TZ 5.1: a person the bot has never seen turns a code into a membership."""
    bot = make_bot()
    handler = Handler()
    await AuthMiddleware()(
        handler,
        make_message(text, telegram_id=STRANGER_TELEGRAM_ID, bot=bot),
        {ACCESS_KEY: FakeAccess()},
    )
    assert handler.was_called
    assert handler.context[INVITE_CODE_KEY] == format_invite_code(VENUE_ID, "A7K9QX4M")
    assert not session_of(bot).calls, "the gate answers nothing when it lets an update through"


async def test_the_confirmation_of_an_invite_reaches_the_handler() -> None:
    """The whole of TZ 5.1 hangs on this press, and it was refused (bug of 15.08.2026).

    An owner invited a colleague with a live code. The colleague opened the link, was
    shown the name on the code and asked to confirm it — and every button on that
    screen answered that the action was unavailable.

    The reason is one line of arithmetic about types: the gate reads the invite code out of
    `target.text`, and a `CallbackQuery` has no text. The code lives in the FSM state from
    the second step onwards, so from the gate's point of view the presser was a stranger
    with nothing to show. Nobody noticed because every code activated in testing belonged
    to somebody who was already a member or the bootstrap owner — both of whom the gate
    admits a line earlier.
    """
    bot = make_bot()
    handler = Handler()

    await AuthMiddleware()(
        handler,
        make_callback(
            InviteConfirm(is_correct=True).pack(),
            telegram_id=STRANGER_TELEGRAM_ID,
            bot=bot,
        ),
        {ACCESS_KEY: FakeAccess(), RAW_STATE_KEY: "Onboarding:confirm"},
    )

    assert handler.was_called, "the button of the confirmation screen must reach its handler"
    assert not session_of(bot).calls, "and the gate answers nothing when it lets one through"


async def test_the_corrected_name_reaches_the_handler_too() -> None:
    """«Поправить имя» leads to a typed message, which carries no code either."""
    handler = Handler()

    await AuthMiddleware()(
        handler,
        make_message("Иван Петров", telegram_id=STRANGER_TELEGRAM_ID),
        {ACCESS_KEY: FakeAccess(), RAW_STATE_KEY: "Onboarding:name"},
    )

    assert handler.was_called


async def test_only_the_joining_scenario_opens_the_gate() -> None:
    """The exemption is one state group wide, and must not become a way past the gate.

    A stranger sitting in any other scenario — or in none — is still a stranger; otherwise
    a state left over from anywhere would be an entrance (TZ 5.1, TZ 9).
    """
    bot = make_bot()
    handler = Handler()

    await AuthMiddleware()(
        handler,
        make_callback(
            InviteConfirm(is_correct=True).pack(),
            telegram_id=STRANGER_TELEGRAM_ID,
            bot=bot,
        ),
        {ACCESS_KEY: FakeAccess(), RAW_STATE_KEY: "TemplateEditor:bulk"},
    )

    assert not handler.was_called
    assert [answer.text for answer in session_of(bot).answers()] == [texts.ERROR_NOT_ALLOWED]


async def test_a_stranger_with_no_state_at_all_is_still_a_stranger() -> None:
    bot = make_bot()
    handler = Handler()

    await AuthMiddleware()(
        handler,
        make_callback(
            InviteConfirm(is_correct=True).pack(),
            telegram_id=STRANGER_TELEGRAM_ID,
            bot=bot,
        ),
        {ACCESS_KEY: FakeAccess(), RAW_STATE_KEY: None},
    )

    assert not handler.was_called


async def test_a_bare_start_is_not_an_invite_code() -> None:
    """The exact case of TZ 5.1: `/start` with no code from an unknown user."""
    assert invite_code_in("/start") is None
    assert invite_code_in("/start@barpoint_bot") is None
    assert invite_code_in("") is None
    assert invite_code_in("   ") is None
    assert invite_code_in(None) is None


async def test_the_command_is_dropped_as_a_word_and_not_as_a_literal() -> None:
    """Telegram writes the bot's name into the command, and always does so in a group.

    `/start@barpoint_bot inv_...` was read as the payload `@barpoint_bot inv_...`, which is
    not a code — so somebody who followed an invite link was told they are not in the
    system (TZ 5.1) instead of being onboarded.
    """
    code = format_invite_code(VENUE_ID, "A7K9QX4M")
    assert invite_code_in(f"/start@barpoint_bot inv_{code}") == code
    assert invite_code_in(f"/START@BarPoint_Bot inv_{code}") == code
    assert invite_code_in(f"/start   inv_{code}") == code


async def test_a_member_passes_with_an_actor() -> None:
    handler = Handler()
    user = make_user()
    access = FakeAccess({STAFF_TELEGRAM_ID: known(user, STAFF)})
    await AuthMiddleware()(handler, make_message("/start"), {ACCESS_KEY: access})
    assert handler.context[ACTOR_KEY] == STAFF
    assert handler.context[IDENTITY_KEY].user is user


async def test_a_deactivated_member_is_answered_like_a_stranger() -> None:
    """TZ 5.1 keeps the row so the reports keep their history; the row is not access."""
    bot = make_bot()
    handler = Handler()
    access = FakeAccess({STAFF_TELEGRAM_ID: known(make_user())})
    await AuthMiddleware()(handler, make_message("/start", bot=bot), {ACCESS_KEY: access})
    assert not handler.was_called
    assert session_of(bot).sent_texts() == [texts.ONBOARDING_NO_ACCESS]


async def test_the_bootstrap_owner_passes_before_any_venue_exists() -> None:
    """Decision A3: `OWNER_TELEGRAM_IDS` unlocks the wizard, and only the wizard."""
    handler = Handler()
    access = FakeAccess({MANAGER_TELEGRAM_ID: stranger(may_create_venue=True)})
    await AuthMiddleware()(
        handler,
        make_message("/start", telegram_id=MANAGER_TELEGRAM_ID),
        {ACCESS_KEY: access},
    )
    assert handler.was_called
    assert handler.context[ACTOR_KEY] is None


async def test_somebody_working_in_two_venues_passes_without_an_actor() -> None:
    """TZ 5.1: the venue choice comes first, and nothing else can be served yet."""
    handler = Handler()
    user = make_user()
    identity = Identity(
        user=user,
        contexts=(STAFF, actor(venue_id=OTHER_VENUE_ID)),
        active=None,
        may_create_venue=False,
    )
    access = FakeAccess({STAFF_TELEGRAM_ID: identity})
    await AuthMiddleware()(handler, make_message("/start"), {ACCESS_KEY: access})
    assert handler.was_called
    assert handler.context[ACTOR_KEY] is None


async def test_an_update_with_nobody_behind_it_reaches_no_handler() -> None:
    handler = Handler()
    message = Message(
        message_id=1,
        date=dt.datetime(2026, 8, 13, tzinfo=dt.UTC),
        chat=Chat(id=CHAT_ID, type="channel"),
        text="post",
    )
    await AuthMiddleware()(handler, message, {ACCESS_KEY: FakeAccess()})
    assert not handler.was_called


# --------------------------------------------------------------------------------------
# Venue context (TZ 3.3)
# --------------------------------------------------------------------------------------


async def test_the_venue_comes_from_the_membership_and_scopes_every_repository() -> None:
    handler = Handler()
    venue = make_venue()
    container = StubContainer([venue, make_venue(OTHER_VENUE_ID)])
    data = context_for(known(make_user(), STAFF), container=container)

    await VenueContextMiddleware()(handler, make_message("/start"), data)

    context = handler.context
    assert context[VENUE_ID_KEY] == VENUE_ID
    assert context[VENUE_KEY] is venue
    repositories = context[SERVICES_KEY].repositories
    scoped = [
        repositories.runs,
        repositories.recipes,
        repositories.members,
        repositories.shifts,
        repositories.items,
        repositories.templates,
        repositories.notifications,
        repositories.audit,
    ]
    assert {repository.venue_id for repository in scoped} == {VENUE_ID}


async def test_every_service_of_the_venue_is_built() -> None:
    """The composition root, exercised: a service that cannot be built fails here."""
    handler = Handler()
    venue = make_venue()
    data = context_for(known(make_user(), STAFF), container=StubContainer([venue]))
    await VenueContextMiddleware()(handler, make_message("/start"), data)
    services = handler.context[SERVICES_KEY]
    assert services.venue_id == VENUE_ID
    assert services.checklists is not None
    assert services.recipes is not None
    assert services.shifts is not None
    assert services.members is not None
    assert services.notifications is not None


async def test_an_update_without_an_actor_passes_with_no_venue() -> None:
    handler = Handler()
    data = context_for(stranger(may_create_venue=True))
    await VenueContextMiddleware()(handler, make_message("/start"), data)
    assert handler.was_called
    assert VENUE_ID_KEY not in handler.context
    assert SERVICES_KEY not in handler.context


async def test_a_membership_whose_venue_is_gone_reaches_no_handler() -> None:
    bot = make_bot()
    handler = Handler()
    data = context_for(known(make_user(), STAFF), container=StubContainer([]))
    await VenueContextMiddleware()(handler, make_message("/start", bot=bot), data)
    assert not handler.was_called


async def test_a_deactivated_venue_is_treated_as_gone() -> None:
    bot = make_bot()
    handler = Handler()
    container = StubContainer([make_venue(is_active=False)])
    data = context_for(known(make_user(), STAFF), container=container)
    await VenueContextMiddleware()(handler, make_message("/start", bot=bot), data)
    assert not handler.was_called


async def test_a_message_to_a_venue_that_is_gone_is_answered_and_not_ignored() -> None:
    """Silence reads as a broken bot, and the employee stops using it.

    The button press was always answered — an unanswered callback spins on the client
    (TZ 9). A message got nothing at all: the update ended in the middleware and the
    employee, who is on shift, saw the bot say nothing to «Моя смена».
    """
    bot = make_bot()
    handler = Handler()
    container = StubContainer([make_venue(is_active=False)])
    data = context_for(known(make_user(), STAFF), container=container)

    await VenueContextMiddleware()(handler, make_message(texts.MENU_SHIFT_BUTTON, bot=bot), data)

    assert not handler.was_called
    assert session_of(bot).sent_texts() == [texts.ERROR_NOT_ALLOWED]


async def test_a_button_press_into_a_venue_that_is_gone_is_answered_once() -> None:
    bot = make_bot()
    container = StubContainer([make_venue(is_active=False)])
    data = context_for(known(make_user(), STAFF), container=container)

    await VenueContextMiddleware()(
        Handler(), make_callback(Nav(target=NavTarget.HOME).pack(), bot=bot), data
    )

    assert [answer.text for answer in session_of(bot).answers()] == [texts.ERROR_NOT_ALLOWED]
    assert session_of(bot).sent_texts() == [], "a spinner is closed, not written to"


# --------------------------------------------------------------------------------------
# last_seen_at
# --------------------------------------------------------------------------------------

NOW: Final = dt.datetime(2026, 8, 13, 12, 0, tzinfo=dt.UTC)


async def test_activity_is_written_when_it_is_stale() -> None:
    handler = Handler()
    container = StubContainer()
    user = make_user(last_seen_at=NOW - dt.timedelta(hours=1))
    data = context_for(known(user, STAFF), container=container)
    await LastSeenMiddleware(clock=lambda: NOW)(handler, make_message("/start"), data)
    assert container.users.touched == [(user.id, NOW)]
    assert user.last_seen_at == NOW
    assert handler.was_called


async def test_activity_is_written_once_for_a_burst_of_taps() -> None:
    """Forty markers in a minute are one write, not forty (see the module docstring)."""
    container = StubContainer()
    user = make_user(last_seen_at=None)
    data = context_for(known(user, STAFF), container=container)
    middleware = LastSeenMiddleware(clock=lambda: NOW)
    for _ in range(40):
        await middleware(Handler(), make_message("tap"), data)
    assert container.users.touched == [(user.id, NOW)]


async def test_activity_is_not_written_for_an_update_with_no_identity() -> None:
    handler = Handler()
    container = StubContainer()
    await LastSeenMiddleware(clock=lambda: NOW)(
        handler, make_message("/start"), {CONTAINER_KEY: container}
    )
    assert container.users.touched == []
    assert handler.was_called


def test_a_stored_moment_without_a_timezone_is_read_as_utc() -> None:
    resolution = dt.timedelta(minutes=5)
    naive = NOW.replace(tzinfo=None)
    assert not is_stale(naive, NOW, resolution)
    assert is_stale(naive - dt.timedelta(minutes=6), NOW, resolution)
    assert is_stale(None, NOW, resolution)


# --------------------------------------------------------------------------------------
# Rate limiting (TZ 9, plan task 20)
# --------------------------------------------------------------------------------------


def test_forty_markers_over_twenty_seconds_are_never_refused() -> None:
    """The plan's own acceptance: «40 быстрых toggle не дают ни одного отказа»."""
    ticker = Ticker()
    limiter = Limiter(clock=ticker)
    refused = 0
    for _ in range(40):
        if not limiter.allow(Budget.TAPS, STAFF_TELEGRAM_ID):
            refused += 1
        ticker.advance(0.5)
    assert refused == 0


def test_the_same_tapping_would_be_refused_on_the_command_budget() -> None:
    """The teeth of the split: one budget for both is the failure this file prevents."""
    ticker = Ticker()
    limiter = Limiter(clock=ticker)
    refused = 0
    for _ in range(40):
        if not limiter.allow(Budget.COMMANDS, STAFF_TELEGRAM_ID):
            refused += 1
        ticker.advance(0.5)
    assert refused > 0


def test_the_tap_burst_is_twenty_and_refills() -> None:
    ticker = Ticker()
    limiter = Limiter(clock=ticker)
    assert all(limiter.allow(Budget.TAPS, STAFF_TELEGRAM_ID) for _ in range(20))
    assert not limiter.allow(Budget.TAPS, STAFF_TELEGRAM_ID)
    ticker.advance(1.0)
    assert sum(limiter.allow(Budget.TAPS, STAFF_TELEGRAM_ID) for _ in range(5)) == 5


def test_the_budgets_do_not_share_a_bucket() -> None:
    ticker = Ticker()
    limiter = Limiter(clock=ticker)
    for _ in range(20):
        limiter.allow(Budget.TAPS, STAFF_TELEGRAM_ID)
    assert limiter.allow(Budget.COMMANDS, STAFF_TELEGRAM_ID)


def test_two_users_do_not_share_a_bucket() -> None:
    ticker = Ticker()
    limiter = Limiter(clock=ticker)
    for _ in range(20):
        limiter.allow(Budget.TAPS, STAFF_TELEGRAM_ID)
    assert limiter.allow(Budget.TAPS, MANAGER_TELEGRAM_ID)


def test_a_button_is_charged_to_the_tap_budget_and_a_message_to_commands() -> None:
    assert budget_for(make_callback(Nav(target=NavTarget.HOME).pack())) is Budget.TAPS
    assert budget_for(make_message("/start")) is Budget.COMMANDS


async def test_a_refused_button_is_always_answered() -> None:
    """TZ 9: an unanswered callback spins on the client for a minute."""
    bot = make_bot()
    ticker = Ticker()
    middleware = ThrottlingMiddleware(limiter=Limiter(clock=ticker), clock=ticker)
    handler = Handler()
    payload = ChecklistToggle(run_id=1, item_id=1).pack()
    for _ in range(21):
        await middleware(handler, make_callback(payload, bot=bot), {})
    assert len(handler.calls) == 20
    assert [answer.text for answer in session_of(bot).answers()] == [texts.ERROR_TOO_FAST]


async def test_a_flood_of_messages_is_not_answered_message_by_message() -> None:
    bot = make_bot()
    ticker = Ticker()
    middleware = ThrottlingMiddleware(limiter=Limiter(clock=ticker), clock=ticker)
    for _ in range(30):
        await middleware(Handler(), make_message("hello", bot=bot), {})
    assert session_of(bot).sent_texts() == [texts.ERROR_TOO_FAST]


def test_the_limiter_forgets_users_instead_of_growing() -> None:
    """The bug this ceiling exists for: the ledger grew under the flood it protects from.

    A bucket per `telegram_id` ever seen, in a process that runs for weeks, is a leak that
    is fastest exactly when the bot is being hammered — and nothing ever removed a bucket
    that was not brim-full, which under a flood is none of them.
    """
    ticker = Ticker()
    limiter = Limiter(clock=ticker, max_tracked=100)
    for telegram_id in range(1_000):
        assert limiter.allow(Budget.COMMANDS, telegram_id)
    assert limiter.tracked == 100


def test_the_bucket_in_use_is_kept_and_the_idle_one_is_forgotten() -> None:
    """Eviction is by least recent use: the entry that waited longest refilled the most."""
    ticker = Ticker()
    limiter = Limiter(clock=ticker, max_tracked=2)
    for telegram_id in (STAFF_TELEGRAM_ID, MANAGER_TELEGRAM_ID):
        for _ in range(COMMAND_RATE.burst):
            assert limiter.allow(Budget.COMMANDS, telegram_id)

    # Touched last, so it is the freshest entry - and still out of tokens.
    assert not limiter.allow(Budget.COMMANDS, STAFF_TELEGRAM_ID)

    # A third user arrives and pushes the idle one out, not the one in the middle of a run.
    assert limiter.allow(Budget.COMMANDS, STRANGER_TELEGRAM_ID)
    assert limiter.tracked == 2
    assert not limiter.allow(Budget.COMMANDS, STAFF_TELEGRAM_ID), "the live bucket was dropped"
    assert limiter.allow(Budget.COMMANDS, MANAGER_TELEGRAM_ID), "the idle one starts full again"


def test_a_bounded_map_forgets_in_order_of_use() -> None:
    ledger: LruDict[str, int] = LruDict(2)
    ledger.put("a", 1)
    ledger.put("b", 2)
    assert ledger.get("a") == 1  # ... which makes "b" the least recently used
    ledger.put("c", 3)
    assert len(ledger) == 2
    assert ledger.get("b") is None
    assert ledger.get("a") == 1
    assert ledger.get("c") == 3


def test_a_bounded_map_holds_at_least_one_entry() -> None:
    with pytest.raises(ValueError, match="at least one"):
        LruDict[str, int](0)


async def test_the_notice_ledger_forgets_users_instead_of_growing() -> None:
    """The same leak on the other map: one row per `telegram_id` ever refused, forever."""
    bot = make_bot()
    ticker = Ticker()
    middleware = ThrottlingMiddleware(limiter=Limiter(clock=ticker), clock=ticker, max_tracked=50)
    for telegram_id in range(200):
        for _ in range(COMMAND_RATE.burst + 2):
            await middleware(Handler(), make_message("hello", telegram_id=telegram_id, bot=bot), {})
    assert middleware.noticed == 50


async def test_an_allowed_update_reaches_the_handler_untouched() -> None:
    bot = make_bot()
    handler = Handler()
    middleware = ThrottlingMiddleware(limiter=Limiter(clock=Ticker()))
    assert await middleware(handler, make_message("/start", bot=bot), {}) == "handled"
    assert not session_of(bot).calls


# --------------------------------------------------------------------------------------
# The callback resolver (TZ 9, acceptance 11.3)
# --------------------------------------------------------------------------------------


def subjects_with_a_neighbour() -> tuple[FakeSubjects, ChecklistRun, ChecklistRun]:
    """Two runs in one table: ours and the bar next door's."""
    ours = make_run(100)
    theirs = make_run(200, venue_id=OTHER_VENUE_ID, user_id=OTHER_USER_ID)
    tables = Tables(runs=[ours, theirs])
    return FakeSubjects(VENUE_ID, tables), ours, theirs


async def test_a_handler_receives_the_object_and_not_the_id() -> None:
    repositories, ours, _ = subjects_with_a_neighbour()
    resolution = await resolve(
        ChecklistToggle(run_id=ours.id, item_id=7).pack(),
        actor=STAFF,
        repositories=repositories,
    )
    assert resolution.is_allowed
    assert resolution.subject is ours
    assert isinstance(resolution.payload, ChecklistToggle)
    assert resolution.payload.item_id == 7


async def test_a_run_of_another_venue_is_invisible() -> None:
    """Acceptance 11.3: the row genuinely exists, and no method of this venue answers it.

    Removing the venue predicate from `FakeRows` fails exactly here, which is what makes
    the predicate in the fake something other than dead code (CLAUDE.md).
    """
    repositories, _, theirs = subjects_with_a_neighbour()
    resolution = await resolve(
        ChecklistToggle(run_id=theirs.id, item_id=7).pack(),
        actor=STAFF,
        repositories=repositories,
    )
    assert resolution.refusal is Refusal.MISSING
    assert resolution.subject is None
    # ... and the neighbour reads its own row perfectly well.
    theirs_view = repositories.neighbour(OTHER_VENUE_ID)
    mine = await resolve(
        ChecklistToggle(run_id=theirs.id, item_id=7).pack(),
        actor=actor(user_id=OTHER_USER_ID, venue_id=OTHER_VENUE_ID),
        repositories=theirs_view,
    )
    assert mine.is_allowed


def checklist_lines() -> tuple[Tables, ChecklistItem, ChecklistItem]:
    """One `checklist_items` table, two venues: a line of ours and a line of theirs.

    The two lines are indistinguishable by themselves — neither carries a `venue_id`
    (decision D9). Which venue a line belongs to is a fact about its template, and nowhere
    else is it written down at all.
    """
    ours = make_template(10)
    theirs = make_template(20, venue_id=OTHER_VENUE_ID)
    our_line = make_item(101, template_id=ours.id)
    their_line = make_item(201, template_id=theirs.id)
    return Tables(templates=[ours, theirs], items=[our_line, their_line]), our_line, their_line


async def test_a_checklist_line_of_another_venue_is_invisible() -> None:
    """The same acceptance 11.3, on a table with no venue column to filter on.

    `checklist_items` is one table for both bars and the id of a foreign line fits into a
    `callback_data` like any other. The only thing hiding it is the join to the template,
    which is why the join is in the fake: drop it from `FakeChildRows` and this is the test
    that fails (CLAUDE.md).
    """
    tables, _, theirs = checklist_lines()
    repositories = FakeSubjects(VENUE_ID, tables)
    resolution = await resolve(
        EditorLine(item_id=theirs.id).pack(), actor=MANAGER, repositories=repositories
    )
    assert resolution.refusal is Refusal.MISSING
    assert resolution.subject is None
    # ... and their own manager opens the very same line normally.
    mine = await resolve(
        EditorLine(item_id=theirs.id).pack(),
        actor=actor(MemberRole.MANAGER, user_id=OTHER_USER_ID, venue_id=OTHER_VENUE_ID),
        repositories=repositories.neighbour(OTHER_VENUE_ID),
    )
    assert mine.subject is theirs


async def test_a_child_row_a_repository_hands_over_is_still_refused() -> None:
    """The second line of defence, on the one shape of row that cannot answer for itself.

    The repository here has lost its join and returns the neighbour's line — the failure
    `_check_ownership` exists for (acceptance 11.3). Asking such a row for `venue_id` gets
    "no such attribute", and reading that as "a row of the shared library, let it through"
    is how the check came to be skipped for every child table in the schema. The venue is
    read where it actually lives: on the template.
    """
    tables, _, theirs = checklist_lines()
    repositories = FakeSubjects(VENUE_ID, tables, leaky_items=True)
    assert await repositories.items.get(theirs.id) is theirs, "the leak has to be real"

    resolution = await resolve(
        EditorLine(item_id=theirs.id).pack(), actor=MANAGER, repositories=repositories
    )
    assert resolution.refusal is Refusal.FORBIDDEN
    assert resolution.subject is None
    assert resolution.text == texts.ERROR_NOT_ALLOWED


async def test_a_child_row_of_this_venue_passes_both_lines() -> None:
    """The other half: the second check must not refuse a line that is genuinely ours."""
    tables, ours, _ = checklist_lines()
    for leaky in (False, True):
        repositories = FakeSubjects(VENUE_ID, tables, leaky_items=leaky)
        resolution = await resolve(
            EditorLine(item_id=ours.id).pack(), actor=MANAGER, repositories=repositories
        )
        assert resolution.subject is ours


def test_every_subject_says_how_to_reach_its_venue() -> None:
    """A row's venue is part of its description, not a guess about its attributes.

    The declarations are checked against the schema while `RULES` is built, so an entity
    described wrongly — or not described at all — stops the import instead of quietly
    getting no venue check. This reads the result back and names the rule.
    """
    for factory, rule in RULES.items():
        if rule.subject is None:
            continue
        tablename = rule.subject.model.__tablename__
        expected = ViaParent if tablename in CHILD_PARENTS else OwnColumn
        assert isinstance(rule.subject.venue, expected), (
            f"{factory.__name__} names a row of {tablename!r}: its venue path has to be "
            f"a {expected.__name__} (decision D9)"
        )


def test_a_venue_path_that_contradicts_the_schema_is_refused_at_import() -> None:
    """Each failure below is a `RULES` that never finishes building: the bot does not start."""
    with pytest.raises(SubjectContractError):
        # `checklist_items` has no venue_id column at all.
        Subject(ChecklistItem, "item_id", load_item, OwnColumn())
    with pytest.raises(SubjectContractError):
        # ... and `checklist_runs` has one, so it is nobody's child.
        Subject(
            ChecklistRun,
            "run_id",
            load_run,
            ViaParent("template_id", ChecklistTemplate, load_template),
        )
    with pytest.raises(SubjectContractError):
        # The parent of a checklist line is its template, whatever the key is called.
        Subject(
            ChecklistItem,
            "item_id",
            load_item,
            ViaParent("recipe_id", ChecklistTemplate, load_template),
        )
    with pytest.raises(SubjectContractError):
        # ... and a recipe is not a checklist template, whichever loader is handed in.
        Subject(ChecklistItem, "item_id", load_item, ViaParent("template_id", Recipe, load_recipe))
    with pytest.raises(SubjectContractError):
        # `venue_id IS NULL` is the library, and only recipes and articles have one (D9).
        Subject(ChecklistRun, "run_id", load_run, OwnColumn(library=True))
    with pytest.raises(TypeError):
        Subject(ChecklistRun, "run_id", load_run)  # type: ignore[call-arg]


async def test_a_missing_row_and_a_foreign_row_read_the_same() -> None:
    """TZ 9: trying ids must not tell an existing object from one that never was."""
    repositories, _, theirs = subjects_with_a_neighbour()
    foreign = await resolve(
        ChecklistToggle(run_id=theirs.id, item_id=1).pack(), actor=STAFF, repositories=repositories
    )
    missing = await resolve(
        ChecklistToggle(run_id=99_999, item_id=1).pack(), actor=STAFF, repositories=repositories
    )
    assert foreign.text == missing.text == texts.ERROR_NOT_ALLOWED


async def test_staff_may_not_touch_another_persons_checklist() -> None:
    """TZ 2: `staff` sees its own data, a manager sees everyone's in the venue."""
    run = make_run(300, user_id=OTHER_USER_ID)
    repositories = FakeSubjects(VENUE_ID, Tables(runs=[run]))
    payload = ChecklistToggle(run_id=run.id, item_id=1).pack()

    refused = await resolve(payload, actor=STAFF, repositories=repositories)
    assert refused.refusal is Refusal.FORBIDDEN

    allowed = await resolve(payload, actor=MANAGER, repositories=repositories)
    assert allowed.is_allowed


async def test_staff_may_not_press_a_management_button() -> None:
    repositories = FakeSubjects(VENUE_ID)
    resolution = await resolve(
        OpenAdmin(section=AdminSection.STAFF).pack(),
        actor=STAFF,
        repositories=repositories,
    )
    assert resolution.refusal is Refusal.FORBIDDEN


async def test_a_manager_button_needs_a_row_of_this_venue() -> None:
    ours = make_member(1)
    theirs = make_member(2, venue_id=OTHER_VENUE_ID, user_id=OTHER_USER_ID)
    repositories = FakeSubjects(VENUE_ID, Tables(members=[ours, theirs]))
    assert (
        await resolve(
            MemberShow(member_id=ours.id).pack(), actor=MANAGER, repositories=repositories
        )
    ).is_allowed
    assert (
        await resolve(
            MemberShow(member_id=theirs.id).pack(), actor=MANAGER, repositories=repositories
        )
    ).refusal is Refusal.MISSING


async def test_a_library_recipe_is_readable_from_any_venue() -> None:
    """TZ 3.3: `venue_id IS NULL` is the shared BarPoint library."""
    shared = make_recipe(1, venue_id=None)
    repositories = FakeSubjects(VENUE_ID, Tables(recipes=[shared]))
    resolution = await resolve(
        RecipeShow(recipe_id=shared.id).pack(), actor=STAFF, repositories=repositories
    )
    assert resolution.subject is shared


@pytest.mark.parametrize(
    ("raw", "refusal"),
    [
        ("zz:1:2", Refusal.UNKNOWN),
        ("ct:not-a-number:2", Refusal.MALFORMED),
        ("", Refusal.UNKNOWN),
    ],
    ids=["unknown prefix", "wrong shape", "empty"],
)
async def test_a_payload_that_cannot_be_read_is_an_outdated_screen(
    raw: str, refusal: Refusal
) -> None:
    resolution = await resolve(raw, actor=STAFF, repositories=FakeSubjects(VENUE_ID))
    assert resolution.refusal is refusal
    assert resolution.text == texts.ERROR_OUTDATED_SCREEN


async def test_a_button_that_names_no_object_needs_no_venue() -> None:
    """The venue choice of TZ 5.1 happens before there is a venue to scope anything to."""
    resolution = await resolve(Nav(target=NavTarget.HOME).pack(), actor=None, repositories=None)
    assert resolution.is_allowed


async def test_a_button_that_needs_an_actor_is_refused_without_one() -> None:
    resolution = await resolve(
        OpenSection(section=MenuAction.MY_SHIFT).pack(),
        actor=None,
        repositories=None,
    )
    assert resolution.refusal is Refusal.NO_CONTEXT


def test_every_callback_factory_has_a_rule() -> None:
    """A button added without a rule is a button nobody checks (TZ 9).

    The point of a single resolver: the day somebody declares a factory and forgets the
    rule, this fails instead of the object being served to whoever pressed it.
    """
    missing = sorted(factory.__name__ for factory in registered() if factory not in RULES)
    assert not missing, (
        f"{', '.join(missing)} has no rule in src/bot/middlewares/resolver.py: declare the "
        "minimum role and how its object is fetched, or say why it names none"
    )


def test_a_factory_that_carries_an_id_and_no_subject_says_why() -> None:
    """The escape hatch is prose in the diff, not silence."""
    for factory, rule in RULES.items():
        if rule.subject is not None:
            continue
        if any(name.endswith("_id") for name in factory.model_fields):
            assert rule.note, f"{factory.__name__} carries an id but fetches nothing"


async def test_the_middleware_refuses_without_calling_the_handler() -> None:
    bot = make_bot()
    handler = Handler()
    repositories, _, theirs = subjects_with_a_neighbour()
    await CallbackResolverMiddleware()(
        handler,
        make_callback(ChecklistToggle(run_id=theirs.id, item_id=1).pack(), bot=bot),
        {ACTOR_KEY: STAFF, SERVICES_KEY: FakeServices(repositories)},
    )
    assert not handler.was_called
    assert [answer.text for answer in session_of(bot).answers()] == [texts.ERROR_NOT_ALLOWED]


async def test_the_middleware_hands_the_handler_a_checked_object() -> None:
    bot = make_bot()
    handler = Handler()
    repositories, ours, _ = subjects_with_a_neighbour()
    await CallbackResolverMiddleware()(
        handler,
        make_callback(ChecklistToggle(run_id=ours.id, item_id=5).pack(), bot=bot),
        {ACTOR_KEY: STAFF, SERVICES_KEY: FakeServices(repositories)},
    )
    assert handler.context[SUBJECT_KEY] is ours
    assert handler.context[PAYLOAD_KEY] == ChecklistToggle(run_id=ours.id, item_id=5)


def test_the_fake_repositories_are_a_subject_repositories() -> None:
    """Read by mypy: the fakes are checked against the protocol the real ones satisfy."""
    repositories: SubjectRepositories = FakeSubjects(VENUE_ID)
    assert repositories is not None


# --------------------------------------------------------------------------------------
# Leaving a half-filled scenario (TZ 5.2)
# --------------------------------------------------------------------------------------
#
# The caption road is exercised through a real dispatcher in `tests/bot/test_dispatcher.py`,
# where the FSM ordering is the point. Here the middleware is called directly, because what
# is asserted is the rule itself: the two roads into a section — a caption on the reply
# keyboard and an inline button — are answered the same way.


#: A step-by-step scenario, standing in for every wizard of TZ 5.8.
#:
#: The real `VenueWizard` and not a group declared here, because the interception of TZ 5.2
#: asks about a scenario only when `src/bot/states/` says that scenario holds unsaved input
#: (:func:`~src.bot.states.keeps_unsaved_input`). A stand-in declared in a test file is not
#: in that set, so it would exercise the *screen* branch while claiming to exercise the
#: wizard one — green, and about the wrong half of the rule.
Wizard: Final = VenueWizard


SECTION_PRESS: Final = OpenSection(section=MenuAction.MY_SHIFT).pack()


def menu_stand() -> tuple[MenuInterceptMiddleware, FSMContext]:
    return MenuInterceptMiddleware(), FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=42, chat_id=CHAT_ID, user_id=STAFF_TELEGRAM_ID),
    )


async def open_scenario(state: FSMContext, **typed: Any) -> dict[str, Any]:
    """A wizard in the middle of its work, and the context aiogram builds around it."""
    await state.set_state(Wizard.name)
    if typed:
        await state.update_data(**typed)
    return {STATE_KEY: state, RAW_STATE_KEY: Wizard.name.state}


async def test_a_menu_press_after_a_search_asks_nothing_at_all() -> None:
    """TZ 5.2 asks «only when there is unsaved data», and a search is not unsaved data.

    `RecipeSearch` keeps the words searched for and the page of hits in the state, because
    pagination and «report the recipe is missing» both need them and free text may not
    travel in a button. Read as "a scenario with data", that made every press of the main
    menu after any search ask whether to interrupt — about a search whose answer the person
    had already read. A prompt shown when nothing is at stake is a prompt people learn to
    dismiss unread, and then it fails to stop the one case it exists for.
    """
    bot = make_bot()
    middleware, state = menu_stand()
    await state.set_state(RecipeSearch.results)
    await state.update_data(query="mohito", offset=10)
    data = {STATE_KEY: state, RAW_STATE_KEY: RecipeSearch.results.state}
    handler = Handler()

    await middleware(handler, make_callback(SECTION_PRESS, bot=bot), data)

    assert handler.was_called, "the section is served, not questioned"
    assert session_of(bot).sent_texts() == [], "nothing was asked"
    assert await state.get_state() is None, "and the screen state is dropped on the way"


async def test_a_section_button_over_unsaved_data_asks_first() -> None:
    """TZ 5.2 does not name the button: an inline section press is a menu press.

    Before this, a scenario left through an inline button lost everything typed into it
    without a word, while the same scenario left through the reply keyboard asked.
    """
    bot = make_bot()
    middleware, state = menu_stand()
    data = await open_scenario(state, full_name="Иван")
    handler = Handler()

    await middleware(handler, make_callback(SECTION_PRESS, bot=bot), data)

    assert not handler.was_called, "the section waits for the question to be answered"
    assert session_of(bot).sent_texts() == [texts.INTERRUPT_QUESTION]
    assert session_of(bot).answers(), "TZ 9: the spinner is closed either way"
    assert await state.get_state() == Wizard.name.state
    assert await state.get_data() == {"full_name": "Иван"}


async def test_the_question_from_a_button_offers_the_section_it_was_asked_about() -> None:
    bot = make_bot()
    middleware, state = menu_stand()
    data = await open_scenario(state, full_name="Иван")

    await middleware(Handler(), make_callback(SECTION_PRESS, bot=bot), data)

    sent = [call for call in session_of(bot).calls if isinstance(call, SendMessage)]
    markup = sent[0].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    payloads = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert payloads == [SECTION_PRESS, MenuKeep().pack()]


async def test_a_section_button_over_an_empty_scenario_asks_nothing() -> None:
    """The other half of TZ 5.2: nothing typed in, nothing to ask about."""
    bot = make_bot()
    middleware, state = menu_stand()
    data = await open_scenario(state)
    handler = Handler()

    await middleware(handler, make_callback(SECTION_PRESS, bot=bot), data)

    assert handler.was_called
    assert await state.get_state() is None, "the abandoned scenario is gone"
    assert data[RAW_STATE_KEY] is None, "and `StateFilter` downstream is told so"
    assert not session_of(bot).sent_texts()


async def test_confirming_the_question_opens_the_section_and_takes_it_off_the_screen() -> None:
    """The confirmation is the same `OpenSection`, pressed on the question's own message.

    It must not be asked about a second time — that is a loop, not a rule — and the
    question must not stay in the chat as a live keyboard onto a scenario that is gone.
    """
    bot = make_bot()
    middleware, state = menu_stand()
    data = await open_scenario(state, full_name="Иван")
    handler = Handler()
    press = make_callback(
        SECTION_PRESS,
        screen_markup=interrupt_keyboard(MenuAction.MY_SHIFT),
        bot=bot,
    )

    await middleware(handler, press, data)

    assert handler.was_called, "the section opens on the answer; it is not asked again"
    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert not session_of(bot).sent_texts(), "the question is not asked twice"
    assert "DeleteMessage" in session_of(bot).method_names(), "the answered question is gone"


async def test_carrying_on_takes_the_question_away_and_leaves_the_scenario() -> None:
    bot = make_bot()
    middleware, state = menu_stand()
    data = await open_scenario(state, full_name="Иван")
    handler = Handler()
    press = make_callback(
        MenuKeep().pack(),
        screen_markup=interrupt_keyboard(MenuAction.MY_SHIFT),
        bot=bot,
    )

    await middleware(handler, press, data)

    assert not handler.was_called
    assert await state.get_data() == {"full_name": "Иван"}
    assert "DeleteMessage" in session_of(bot).method_names()


async def test_a_button_of_the_scenario_itself_is_not_an_interruption() -> None:
    """Only a menu press interrupts: a tap inside the open screen is ordinary work."""
    bot = make_bot()
    middleware, state = menu_stand()
    data = await open_scenario(state, full_name="Иван")
    handler = Handler()

    await middleware(
        handler, make_callback(ChecklistToggle(run_id=1, item_id=1).pack(), bot=bot), data
    )

    assert handler.was_called
    assert await state.get_state() == Wizard.name.state
    assert not session_of(bot).calls


async def test_a_mistyped_code_does_not_open_the_gate_for_a_stranger() -> None:
    """The exemption is two steps wide, not a whole scenario (found by review).

    `_refuse_the_code` parks *anybody* who typed something wrong in `Onboarding.code`, so a
    group-wide exemption meant one bad guess bought a stranger a day of passing the gate —
    including with callbacks, which is exactly what the gate exists to stop.
    """
    bot = make_bot()
    handler = Handler()

    await AuthMiddleware()(
        handler,
        make_callback(
            InviteConfirm(is_correct=True).pack(),
            telegram_id=STRANGER_TELEGRAM_ID,
            bot=bot,
        ),
        {ACCESS_KEY: FakeAccess(), RAW_STATE_KEY: "Onboarding:code"},
    )

    assert not handler.was_called
    assert [answer.text for answer in session_of(bot).answers()] == [texts.ERROR_NOT_ALLOWED]


async def test_typing_a_code_still_works_from_that_state() -> None:
    """Nothing is lost by leaving `Onboarding.code` out: a typed code passes on its text."""
    handler = Handler()

    await AuthMiddleware()(
        handler,
        make_message(
            format_invite_code(VENUE_ID, "A7K9QX4M"),
            telegram_id=STRANGER_TELEGRAM_ID,
        ),
        {ACCESS_KEY: FakeAccess(), RAW_STATE_KEY: "Onboarding:code"},
    )

    assert handler.was_called
