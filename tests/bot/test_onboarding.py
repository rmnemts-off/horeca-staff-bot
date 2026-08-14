"""The way in (TZ 5.1, plan task 22).

What is asserted here is the road, not the wording: TZ 8.2 lets the customer reword every
string without a developer, so the tests compare against `src/bot/texts/` rather than
against phrases. What must not move is the shape of the road —

* a deep link with a live code ends on the confirmation of decision B8, and the code it
  ends on is in the FSM state, not in the button;
* each way a code can fail gets **its own** answer, because "ask the manager for a new one"
  and "you already used it" send the employee to two different places;
* activation ends with a membership and with the permanent keyboard of TZ 5.2, and the two
  arrive together — a menu that appears one message later is a menu the bartender scrolls
  for;
* a second `/start` from somebody who works here is the menu and nothing else (TZ 5.1);
* the venue choice happens before there is a venue, so it is drawn from the identity and
  refuses a venue the person does not work in — the one screen where a forged
  `callback_data` has no venue-scoped repository to die in (TZ 9);
* **the empty state is a first-class screen** (TZ 8.1): a bootstrap owner arrives at a
  database with no venue in it at all, and what he sees has exactly one button, which
  leads to the wizard rather than back to itself.

`AccessService` is faked rather than run against a database, and the fake is deliberately
narrow: the five methods this block calls, with the signatures the real service declares.
The views are pure functions and are exercised without a bot at all, which is what makes the
empty-state assertions plain `assert`s (plan task 41).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from src.bot import texts
from src.bot.callbacks import InviteConfirm, Nav, VenueCreate, VenueSelect, parse
from src.bot.handlers import onboarding as handlers
from src.bot.keyboards import onboarding as keyboards
from src.bot.keyboards.menu import entries_for
from src.bot.middlewares.auth import ACTOR_KEY, IDENTITY_KEY, INVITE_CODE_KEY
from src.bot.middlewares.menu import STATE_KEY
from src.bot.middlewares.resolver import PAYLOAD_KEY, Refusal, resolve
from src.bot.middlewares.services import ACCESS_KEY
from src.bot.states import Onboarding
from src.bot.views import onboarding as views
from src.db.models import MemberRole, User, VenueMember
from src.services.access import (
    AccessContext,
    Identity,
    InviteActivation,
    InvitePreview,
    InviteRejection,
    UnknownMembershipError,
    format_invite_code,
)

from tests.bot.test_middlewares import (
    CHAT_ID,
    MANAGER_TELEGRAM_ID,
    OTHER_VENUE_ID,
    STAFF,
    STAFF_TELEGRAM_ID,
    STAFF_USER_ID,
    VENUE_ID,
    actor,
    known,
    make_bot,
    make_callback,
    make_message,
    make_user,
    session_of,
    stranger,
)

VENUE_NAME: Final = "PIMS"
OTHER_VENUE_NAME: Final = "Invasion"
CODE: Final = format_invite_code(VENUE_ID, "A7K9QX4M")
RECORDED_NAME: Final = "Иван Иванов"
POSITION: Final = "бармен"
TYPED_NAME: Final = "Пётр Смирнов"


# --------------------------------------------------------------------------------------
# The service, faked at the five methods this block calls
# --------------------------------------------------------------------------------------


@dataclass
class Invite:
    """One row of `invite_codes` as onboarding sees it (TZ 5.1, decision B8)."""

    code: str = CODE
    venue_id: int = VENUE_ID
    role: MemberRole = MemberRole.STAFF
    full_name: str | None = RECORDED_NAME
    position: str | None = POSITION
    #: Already decided by the service in the real thing: expired, revoked, spent.
    rejection: InviteRejection | None = None


class FakeAccess:
    """`AccessService` reduced to what `src/bot/handlers/onboarding.py` asks of it.

    Every method keeps the signature of the real one, the moment included: decision D12 has
    the service take `now` rather than read a clock, and a fake that dropped the argument
    would make a handler that forgot to pass it pass this file.
    """

    def __init__(
        self,
        *,
        invites: Sequence[Invite] = (),
        venues: Mapping[int, str] | None = None,
        memberships: Sequence[AccessContext] = (),
    ) -> None:
        self.invites = {invite.code: invite for invite in invites}
        self.venues = dict(venues if venues is not None else {VENUE_ID: VENUE_NAME})
        self.memberships = {(context.user_id, context.venue_id): context for context in memberships}
        self.spent: list[str] = []
        self.joined: list[tuple[str, int, str]] = []
        self.selected: list[tuple[int, int]] = []
        self.asked_for_names: list[tuple[int, ...]] = []
        #: Decision A3: `/start` from a bootstrap id writes the `users` row. Recorded here
        #: because nothing else in the product writes one for the first owner, and a fake
        #: that answered without recording would let the call be dropped again.
        self.bootstrapped: list[tuple[int, str, str | None]] = []

    async def ensure_bootstrap_user(
        self,
        *,
        telegram_id: int,
        full_name: str,
        username: str | None = None,
    ) -> User:
        self.bootstrapped.append((telegram_id, full_name, username))
        return User(id=telegram_id, telegram_id=telegram_id, full_name=full_name, is_active=True)

    async def preview_invite_code(self, raw_code: str, *, now: dt.datetime) -> InvitePreview:
        assert now.tzinfo is not None, "decision D12: every instant crossing a service is aware"
        invite = self.invites.get(raw_code)
        if invite is None:
            return InvitePreview(
                venue_id=0,
                code_id=0,
                role=MemberRole.STAFF,
                position=None,
                suggested_full_name=None,
                rejection=InviteRejection.UNKNOWN,
            )
        return InvitePreview(
            venue_id=invite.venue_id,
            code_id=1,
            role=invite.role,
            position=invite.position,
            suggested_full_name=invite.full_name,
            rejection=invite.rejection,
        )

    async def activate_invite_code(
        self,
        raw_code: str,
        *,
        telegram_id: int,
        full_name: str,
        now: dt.datetime,
        username: str | None = None,
    ) -> InviteActivation:
        assert now.tzinfo is not None
        invite = self.invites.get(raw_code)
        if invite is None:
            return InviteActivation(rejection=InviteRejection.UNKNOWN)
        if invite.rejection is not None:
            return InviteActivation(rejection=invite.rejection, venue_id=invite.venue_id)
        if raw_code in self.spent:
            # TZ 5.1: single use, and the second press of a live button is exactly that.
            return InviteActivation(
                rejection=InviteRejection.ALREADY_USED, venue_id=invite.venue_id
            )
        self.spent.append(raw_code)
        self.joined.append((raw_code, telegram_id, full_name))
        user = User(
            id=STAFF_USER_ID,
            telegram_id=telegram_id,
            full_name=full_name,
            username=username,
            is_active=True,
        )
        member = VenueMember(
            id=1,
            venue_id=invite.venue_id,
            user_id=user.id,
            role=invite.role,
            position=invite.position,
            is_active=True,
        )
        return InviteActivation(user=user, member=member, venue_id=invite.venue_id)

    async def venue_names(self, venue_ids: Iterable[int]) -> Mapping[int, str]:
        asked = tuple(venue_ids)
        self.asked_for_names.append(asked)
        return {venue_id: self.venues[venue_id] for venue_id in asked if venue_id in self.venues}

    async def select_venue(self, user_id: int, venue_id: int) -> AccessContext:
        context = self.memberships.get((user_id, venue_id))
        if context is None:
            raise UnknownMembershipError(user_id, venue_id)
        self.selected.append((user_id, venue_id))
        return context


# --------------------------------------------------------------------------------------
# The stand
# --------------------------------------------------------------------------------------


@dataclass
class Stand:
    """A bot that answers itself, a live FSM and the faked service."""

    bot: Bot
    state: FSMContext
    access: FakeAccess
    identity: Identity | None = None
    actor: AccessContext | None = None
    invite_code: str | None = None
    payload: Any = field(default=None)

    def context(self, **overrides: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            handlers.BOT_KEY: self.bot,
            STATE_KEY: self.state,
            ACCESS_KEY: self.access,
            ACTOR_KEY: self.actor,
            INVITE_CODE_KEY: self.invite_code,
        }
        if self.identity is not None:
            data[IDENTITY_KEY] = self.identity
        if self.payload is not None:
            data[PAYLOAD_KEY] = self.payload
        return {**data, **overrides}


def stand(**fields: Any) -> Stand:
    bot = make_bot()
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot.id, chat_id=CHAT_ID, user_id=STAFF_TELEGRAM_ID),
    )
    fields.setdefault("access", FakeAccess(invites=[Invite()]))
    return Stand(bot=bot, state=state, **fields)


def sends(bot: Bot) -> list[SendMessage]:
    return [call for call in session_of(bot).calls if isinstance(call, SendMessage)]


def edits(bot: Bot) -> list[EditMessageText]:
    return [call for call in session_of(bot).calls if isinstance(call, EditMessageText)]


def payloads_of(markup: Any) -> list[str]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button.callback_data or "" for row in markup.inline_keyboard for button in row]


def captions_of(markup: Any) -> list[str]:
    if isinstance(markup, ReplyKeyboardMarkup):
        return [button.text for row in markup.keyboard for button in row]
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button.text for row in markup.inline_keyboard for button in row]


# --------------------------------------------------------------------------------------
# The deep link and the typed code (TZ 5.1)
# --------------------------------------------------------------------------------------


async def test_a_deeplink_with_a_live_code_reaches_the_confirmation() -> None:
    """The scenario of TZ 5.1: the link opens the bot and the code works by itself."""
    at = stand(invite_code=CODE, identity=stranger())
    message = make_message(f"/start inv_{CODE}", bot=at.bot)

    await handlers.start(message, **at.context())

    (sent,) = sends(at.bot)
    assert VENUE_NAME in sent.text
    assert RECORDED_NAME in sent.text, "decision B8: the manager's spelling is what is confirmed"
    assert POSITION in sent.text
    assert payloads_of(sent.reply_markup) == [
        InviteConfirm(is_correct=True).pack(),
        InviteConfirm(is_correct=False).pack(),
    ]


async def test_the_confirmation_keeps_the_code_in_the_state_and_not_in_the_button() -> None:
    """Rule 2 of `src/bot/callbacks.py`: text lives in the FSM, ids live in buttons."""
    at = stand(invite_code=CODE, identity=stranger())

    await handlers.start(make_message(f"/start inv_{CODE}", bot=at.bot), **at.context())

    assert await at.state.get_state() == Onboarding.confirm.state
    assert await at.state.get_data() == {
        handlers.CODE_FIELD: CODE,
        handlers.NAME_FIELD: RECORDED_NAME,
    }
    assert all(CODE not in payload for payload in payloads_of(sends(at.bot)[0].reply_markup))


async def test_a_code_typed_into_the_chat_takes_the_same_road() -> None:
    """TZ 5.1 sends the manager a code *or* a link, so both roads end on one screen."""
    at = stand(invite_code=CODE, identity=stranger())

    await handlers.by_code(make_message(CODE, bot=at.bot), **at.context())

    (sent,) = sends(at.bot)
    assert VENUE_NAME in sent.text
    assert await at.state.get_state() == Onboarding.confirm.state


async def test_the_filter_reads_the_code_the_gate_parsed() -> None:
    """One definition of "this is a code", and it is the gate's (`middlewares/auth.py`)."""
    message = make_message(CODE)
    assert await handlers.CarriesInviteCode()(message, **{INVITE_CODE_KEY: CODE})
    assert not await handlers.CarriesInviteCode()(message, **{INVITE_CODE_KEY: None})
    assert not await handlers.CarriesInviteCode()(message)


@pytest.mark.parametrize(
    ("rejection", "expected"),
    [
        (InviteRejection.EXPIRED, texts.ONBOARDING_CODE_EXPIRED),
        (InviteRejection.ALREADY_USED, texts.ONBOARDING_CODE_USED),
        (InviteRejection.REVOKED, texts.ONBOARDING_CODE_UNKNOWN),
    ],
    ids=["expired", "already used", "revoked"],
)
async def test_each_way_a_code_fails_has_its_own_answer(
    rejection: InviteRejection, expected: str
) -> None:
    """TZ 5.1: a code lives seven days and is used once, and the two failures differ.

    "Ask for a new one" and "this one already worked" send the employee to two different
    places, so one flat error here is a support call in the middle of a shift.
    """
    at = stand(
        access=FakeAccess(invites=[Invite(rejection=rejection)]),
        invite_code=CODE,
        identity=stranger(),
    )

    await handlers.by_code(make_message(CODE, bot=at.bot), **at.context())

    (sent,) = sends(at.bot)
    assert sent.text == expected
    assert sent.reply_markup is None, "TZ 8.1: a refusal offers no button into nowhere"
    assert await at.state.get_state() == Onboarding.code.state, "the next line is another try"


async def test_a_code_nobody_issued_is_refused() -> None:
    at = stand(access=FakeAccess(), invite_code=CODE, identity=stranger())

    await handlers.by_code(make_message(CODE, bot=at.bot), **at.context())

    assert [call.text for call in sends(at.bot)] == [texts.ONBOARDING_CODE_UNKNOWN]


async def test_a_line_that_was_meant_to_be_a_code_is_answered() -> None:
    """`Onboarding.code` with nothing the gate could read: naming it beats silence."""
    at = stand(identity=stranger())
    await at.state.set_state(Onboarding.code)

    await handlers.by_code(make_message("код?", bot=at.bot), **at.context())

    assert [call.text for call in sends(at.bot)] == [texts.ONBOARDING_CODE_UNKNOWN]


async def test_a_code_whose_venue_is_gone_reads_like_a_code_that_never_was() -> None:
    """TZ 9: trying ids must not tell a deleted venue from one that never existed."""
    at = stand(
        access=FakeAccess(invites=[Invite()], venues={}),
        invite_code=CODE,
        identity=stranger(),
    )

    await handlers.by_code(make_message(CODE, bot=at.bot), **at.context())

    assert [call.text for call in sends(at.bot)] == [texts.ONBOARDING_CODE_UNKNOWN]


async def test_a_code_with_no_name_on_it_asks_for_one(  # decision B8 leaves it optional
) -> None:
    at = stand(
        access=FakeAccess(invites=[Invite(full_name=None)]),
        invite_code=CODE,
        identity=stranger(),
    )

    await handlers.by_code(make_message(CODE, bot=at.bot), **at.context())

    (sent,) = sends(at.bot)
    assert VENUE_NAME in sent.text
    assert texts.ONBOARDING_NAME_PROMPT in sent.text
    assert sent.reply_markup is None, "the answer is typed, so nothing is offered to press"
    assert await at.state.get_state() == Onboarding.name.state


# --------------------------------------------------------------------------------------
# Confirming, correcting and joining (TZ 5.1, decision B8)
# --------------------------------------------------------------------------------------


async def confirming(at: Stand, *, is_correct: bool) -> None:
    await at.state.set_state(Onboarding.confirm)
    await at.state.set_data({handlers.CODE_FIELD: CODE, handlers.NAME_FIELD: RECORDED_NAME})
    payload = InviteConfirm(is_correct=is_correct)
    press = make_callback(payload.pack(), bot=at.bot)
    await handlers.confirm(press, **at.context(**{PAYLOAD_KEY: payload}))


async def test_confirming_the_name_puts_the_person_in_the_venue() -> None:
    at = stand(identity=stranger())

    await confirming(at, is_correct=True)

    assert at.access.joined == [(CODE, STAFF_TELEGRAM_ID, RECORDED_NAME)]
    assert await at.state.get_state() is None, "the scenario is over, not left half open"
    assert await at.state.get_data() == {}


async def test_joining_ends_with_the_permanent_keyboard_of_the_menu() -> None:
    """TZ 5.2: the menu is a reply keyboard, so it arrives with a message and not an edit."""
    at = stand(identity=stranger())

    await confirming(at, is_correct=True)

    (edited,) = edits(at.bot)
    assert VENUE_NAME in str(edited.text)
    assert RECORDED_NAME in str(edited.text)
    (sent,) = sends(at.bot)
    assert sent.text == texts.MENU_PROMPT
    assert captions_of(sent.reply_markup) == [
        entry.caption for entry in entries_for(MemberRole.STAFF)
    ]


async def test_the_answered_question_stops_being_a_live_button() -> None:
    """TZ 8.2 edits the screen in place; here that is also what makes «use once» hold."""
    at = stand(identity=stranger())

    await confirming(at, is_correct=True)

    (edited,) = edits(at.bot)
    assert edited.reply_markup is None
    assert session_of(at.bot).answers(), "TZ 9: the spinner is closed on the way out"


async def test_correcting_the_name_asks_for_it_without_a_greeting() -> None:
    at = stand(identity=stranger())

    await confirming(at, is_correct=False)

    (edited,) = edits(at.bot)
    assert edited.text == texts.ONBOARDING_NAME_PROMPT
    assert at.access.joined == [], "nothing is activated until the name is settled"
    assert await at.state.get_state() == Onboarding.name.state


async def test_the_typed_name_is_the_one_that_is_recorded() -> None:
    """Decision B8: the profile holds nicknames, the schedule is matched by full name."""
    at = stand(identity=stranger())
    await at.state.set_state(Onboarding.name)
    await at.state.set_data({handlers.CODE_FIELD: CODE, handlers.NAME_FIELD: RECORDED_NAME})

    await handlers.named(make_message(TYPED_NAME, bot=at.bot), **at.context())

    assert at.access.joined == [(CODE, STAFF_TELEGRAM_ID, TYPED_NAME)]
    (sent,) = sends(at.bot)
    assert TYPED_NAME in sent.text
    assert captions_of(sent.reply_markup) == [
        entry.caption for entry in entries_for(MemberRole.STAFF)
    ]


async def test_a_name_too_short_to_be_one_leaves_the_scenario_where_it_was() -> None:
    at = stand(identity=stranger())
    await at.state.set_state(Onboarding.name)
    await at.state.set_data({handlers.CODE_FIELD: CODE, handlers.NAME_FIELD: RECORDED_NAME})

    await handlers.named(make_message("Ян", bot=at.bot), **at.context())

    assert [call.text for call in sends(at.bot)] == [texts.ONBOARDING_NAME_TOO_SHORT]
    assert at.access.joined == []
    assert await at.state.get_state() == Onboarding.name.state, "the question is still open"


async def test_a_code_spent_between_the_screen_and_the_press_is_refused() -> None:
    """The confirmation is a live button and the code is single use (TZ 5.1)."""
    at = stand(identity=stranger())

    await confirming(at, is_correct=True)
    session_of(at.bot).calls.clear()
    await confirming(at, is_correct=True)

    (edited,) = edits(at.bot)
    assert edited.text == texts.ONBOARDING_CODE_USED
    assert len(at.access.joined) == 1, "the second press must not create a second membership"


async def test_a_confirmation_the_store_forgot_says_the_screen_is_stale() -> None:
    """The FSM has a day's TTL (`src/bot/dispatcher.py`); a button outlives it."""
    at = stand(identity=stranger())
    payload = InviteConfirm(is_correct=True)
    press = make_callback(payload.pack(), bot=at.bot)

    await handlers.confirm(press, **at.context(**{PAYLOAD_KEY: payload}))

    assert [answer.text for answer in session_of(at.bot).answers()] == [texts.ERROR_OUTDATED_SCREEN]
    assert at.access.joined == []


# --------------------------------------------------------------------------------------
# Coming back, and working in two places (TZ 5.1)
# --------------------------------------------------------------------------------------


async def test_a_member_who_says_start_again_gets_the_menu_and_nothing_else() -> None:
    """TZ 5.1: «при повторном `/start` от известного пользователя — сразу главное меню»."""
    at = stand(identity=known(make_user(), STAFF), actor=STAFF)

    await handlers.start(make_message("/start", bot=at.bot), **at.context())

    (sent,) = sends(at.bot)
    assert sent.text == texts.MENU_PROMPT
    assert captions_of(sent.reply_markup) == [
        entry.caption for entry in entries_for(MemberRole.STAFF)
    ]


def two_venues() -> tuple[Identity, FakeAccess]:
    other = actor(venue_id=OTHER_VENUE_ID)
    identity = Identity(
        user=make_user(),
        contexts=(STAFF, other),
        active=None,
        may_create_venue=False,
    )
    access = FakeAccess(
        venues={VENUE_ID: VENUE_NAME, OTHER_VENUE_ID: OTHER_VENUE_NAME},
        memberships=(STAFF, other),
    )
    return identity, access


async def test_somebody_working_in_two_places_is_asked_which_one() -> None:
    identity, access = two_venues()
    at = stand(access=access, identity=identity)

    await handlers.start(make_message("/start", bot=at.bot), **at.context())

    (sent,) = sends(at.bot)
    assert sent.text == texts.ONBOARDING_PICK_VENUE
    assert access.asked_for_names == [(VENUE_ID, OTHER_VENUE_ID)], (
        "the list is the memberships the gate already resolved, not every venue there is"
    )
    assert captions_of(sent.reply_markup) == [VENUE_NAME, OTHER_VENUE_NAME]
    assert payloads_of(sent.reply_markup) == [
        VenueSelect(venue_id=VENUE_ID).pack(),
        VenueSelect(venue_id=OTHER_VENUE_ID).pack(),
    ]


async def test_the_chosen_venue_is_remembered_and_the_menu_follows() -> None:
    """TZ 5.1: the choice is remembered, which `select_venue` writes to `users`."""
    identity, access = two_venues()
    at = stand(access=access, identity=identity)
    payload = VenueSelect(venue_id=OTHER_VENUE_ID)

    await handlers.choose_venue(
        make_callback(payload.pack(), bot=at.bot),
        **at.context(**{PAYLOAD_KEY: payload}),
    )

    assert access.selected == [(STAFF_USER_ID, OTHER_VENUE_ID)]
    (sent,) = sends(at.bot)
    assert sent.text == texts.MENU_PROMPT
    assert isinstance(sent.reply_markup, ReplyKeyboardMarkup)


async def test_a_venue_the_person_does_not_work_in_is_refused() -> None:
    """The one screen with no venue-scoped repository behind it, so the check is here (TZ 9)."""
    identity, access = two_venues()
    at = stand(access=access, identity=identity)
    payload = VenueSelect(venue_id=999)

    await handlers.choose_venue(
        make_callback(payload.pack(), bot=at.bot),
        **at.context(**{PAYLOAD_KEY: payload}),
    )

    assert access.selected == []
    assert [answer.text for answer in session_of(at.bot).answers()] == [texts.ERROR_NOT_ALLOWED]
    assert sends(at.bot) == [], "a spinner is closed, not written to"


# --------------------------------------------------------------------------------------
# The empty state: no venue exists at all (TZ 8.1, decision A3)
# --------------------------------------------------------------------------------------


async def test_the_bootstrap_owner_is_offered_the_only_thing_there_is_to_do() -> None:
    """Decision A3: `OWNER_TELEGRAM_IDS` unlocks the wizard, and only the wizard."""
    at = stand(access=FakeAccess(venues={}), identity=stranger(may_create_venue=True))
    message = make_message("/start", telegram_id=MANAGER_TELEGRAM_ID, bot=at.bot)

    await handlers.start(message, **at.context())

    (sent,) = sends(at.bot)
    assert sent.text == texts.ONBOARDING_CREATE_VENUE_OFFER
    assert payloads_of(sent.reply_markup) == [VenueCreate().pack()]


async def test_the_bootstrap_owner_gets_a_users_row_before_the_wizard_asks_anything() -> None:
    """Decision A3 in full: `/start` from a bootstrap id **creates the `users` row**.

    This is the assertion the product went a whole wave without, and the cost was total:
    nothing else writes a `users` row for the first owner (`activate_invite_code` does, but
    a code can only be issued from inside a venue that does not exist yet), and the wizard
    refuses an identity with no user behind it. So on a brand-new installation the owner
    pressed the one button the screen had and was told the action was unavailable —
    criterion 11.6 failing on an empty database, with every venue, employee and checklist
    behind it unreachable.

    No unit test could see it: the fixtures of `tests/bot/test_admin_venue.py` build the
    identity *with* a user and call that "`/start` made the row". A live stack found it in
    a minute, which is the point of criterion 11.6 asking for a live stack.
    """
    at = stand(access=FakeAccess(venues={}), identity=stranger(may_create_venue=True))
    message = make_message("/start", telegram_id=MANAGER_TELEGRAM_ID, bot=at.bot)

    await handlers.start(message, **at.context())

    assert at.access.bootstrapped == [(MANAGER_TELEGRAM_ID, "X", None)], (
        "the row is written on /start, so the wizard has an owner to point at"
    )


async def test_nobody_else_gets_a_users_row_from_pressing_start() -> None:
    """The row is the bootstrap trigger's, not a side effect of saying hello (TZ 5.1)."""
    at = stand(access=FakeAccess(venues={}), identity=stranger())

    await handlers.start(make_message("/start", bot=at.bot), **at.context())

    assert at.access.bootstrapped == []


async def test_nothing_is_said_to_somebody_the_gate_would_not_have_let_through() -> None:
    """TZ 5.1: the refusal is the gate's, and this module must not grow a second one."""
    at = stand(access=FakeAccess(venues={}), identity=stranger())

    await handlers.start(make_message("/start", bot=at.bot), **at.context())

    assert session_of(at.bot).calls == []


# --------------------------------------------------------------------------------------
# The views, without a bot (plan task 41)
# --------------------------------------------------------------------------------------


def test_the_empty_state_has_exactly_one_way_out() -> None:
    screen = views.venue_missing()
    assert screen.text == texts.ONBOARDING_CREATE_VENUE_OFFER
    assert payloads_of(screen.markup) == [VenueCreate().pack()]
    assert captions_of(screen.markup) == [texts.ONBOARDING_CREATE_VENUE_BUTTON]


def test_a_venue_choice_over_nothing_says_so_instead_of_drawing_an_empty_list() -> None:
    """A view is total, and «Выберите заведение.» over no buttons is the worse rendering."""
    screen = views.venue_choice([])
    assert screen.text == texts.ONBOARDING_NO_ACCESS
    assert screen.markup is None


@pytest.mark.parametrize("rejection", list(InviteRejection), ids=str)
def test_every_rejection_has_wording_and_no_buttons(rejection: InviteRejection) -> None:
    """A sixth outcome added to the enum fails here rather than falling into a default."""
    screen = views.invite_rejected(rejection)
    assert screen.text.strip()
    assert screen.markup is None


def test_the_confirmation_drops_the_position_when_the_code_carries_none() -> None:
    with_position = views.invite_confirmation(
        venue=VENUE_NAME, full_name=RECORDED_NAME, position=POSITION
    )
    without = views.invite_confirmation(venue=VENUE_NAME, full_name=RECORDED_NAME, position=None)
    assert POSITION in with_position.text
    assert POSITION not in without.text
    assert RECORDED_NAME in without.text
    assert without.text.count(VENUE_NAME) == 1


def test_no_screen_of_this_block_offers_navigation_into_nowhere() -> None:
    """TZ 8.1 over TZ 5.2: back, home and cancel all lead to a menu this person has not got.

    The three navigation buttons are drawn by `keyboards/menu.py` for every other screen of
    the bot, which is why their absence here is asserted rather than assumed.
    """
    markups = [
        keyboards.invite_confirmation(),
        keyboards.venue_choice([(VENUE_ID, VENUE_NAME)]),
        keyboards.venue_missing(),
    ]
    for markup in markups:
        for payload in payloads_of(markup):
            assert not isinstance(parse(payload), Nav), f"{payload} leads to a menu that is not"


# --------------------------------------------------------------------------------------
# The two new factories, against the resolver that has to know them (TZ 9)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [InviteConfirm(is_correct=True).pack(), VenueCreate().pack()],
    ids=["invite confirmation", "create a venue"],
)
async def test_the_buttons_of_the_way_in_are_pressable_without_a_membership(
    payload: str,
) -> None:
    """Both are pressed before there is a venue, so both declare `needs_actor=False`.

    Without the rule the resolver refuses them outright, and TZ 5.1's whole first screen
    becomes unreachable for exactly the people it is written for.
    """
    resolution = await resolve(payload, actor=None, repositories=None)
    assert resolution.is_allowed
    assert resolution.subject is None


async def test_a_button_of_the_way_in_still_carries_no_venue_of_its_own() -> None:
    """They grant nothing: the code is re-read by the service, the wizard by task 26."""
    resolution = await resolve("vc", actor=None, repositories=None)
    assert resolution.refusal is not Refusal.UNREGISTERED
