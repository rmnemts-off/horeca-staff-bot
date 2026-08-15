"""«👥 Сотрудники»: the roster, the card and the invite wizard (TZ 5.1, 5.8; plan task 27).

Three kinds of test, and the split is deliberate.

* **The screens are checked as values.** `src/bot/views/staff.py` is a pure function of what
  the service returned, so the empty state of TZ 8.1 is an ordinary assert and not a bot
  scenario — including the property that matters most about it: every button on an empty
  screen goes somewhere real (`test_no_button_of_the_empty_roster_leads_nowhere`).
* **The handlers are exercised with fake services.** The fakes implement the protocols
  `src/bot/handlers/admin_staff.py` declares, so mypy compares them against the real
  `MemberService` and `AccessService` through :func:`_the_real_services_satisfy_the_protocols`
  — a fake that promised more than the service does would fail the type check rather than
  make a green test about behaviour that does not exist (CLAUDE.md).
* **The rights are checked where they are decided.** Nothing in this module hides a button
  from `staff`: the resolver refuses the payload (TZ 9), so that is what is asserted, with
  the same `resolve()` the middleware calls.

The one thing the fakes copy from the real service on purpose is that
`MemberService.list_roster` answers with the **active** members only. That is why
`test_switching_an_employee_off_keeps_the_history` asserts the row is still in the table
while the list no longer shows it: the row surviving is TZ 5.1, and the mark «не работает»
having nowhere to appear in the list is the service-layer gap this task reports.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any, Final

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage, TelegramMethod
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import User as TelegramUser
from src.bot import texts
from src.bot.callbacks import (
    AdminSection,
    InviteRevoke,
    InviteRole,
    MemberActive,
    MemberEdit,
    MemberField,
    MemberSetRole,
    MemberShow,
    OpenAdmin,
    parse,
)
from src.bot.handlers.admin_staff import (
    MEMBER_FIELD,
    Invites,
    StaffServices,
    choose_role,
    finish_member_edit,
    invite_name,
    invite_position,
    open_staff,
    revoke_code,
    router,
    set_member_active,
    set_member_role,
    show_member,
    skip_position,
    stale_invite_step,
    start_invite,
    start_member_edit,
)
from src.bot.keyboards.staff import INITIAL_ROLE
from src.bot.middlewares.resolver import RULES, Refusal, resolve
from src.bot.middlewares.services import VenueServices
from src.bot.states import InviteWizard, MemberEditing
from src.bot.views.staff import DEEPLINK_URL_TEMPLATE, member_screen, roster_line, roster_screen
from src.db.models import InviteCode, MemberRole, User, Venue, VenueMember
from src.services.access import (
    AccessContext,
    AccessService,
    PermissionDeniedError,
    SelfTargetError,
    invite_deeplink_payload,
    issuable_roles,
)
from src.services.members import LastOwnerError, RosterEntry

from tests.bot.test_middlewares import (
    BOT_TOKEN,
    CHAT_ID,
    MANAGER,
    MANAGER_TELEGRAM_ID,
    STAFF,
    VENUE_ID,
    FakeSubjects,
    RecordingSession,
    make_callback,
    make_message,
    session_of,
)
from tests.bot.test_middlewares import (
    actor as make_actor,
)

BOT_ID: Final = 42
BOT_USERNAME: Final = "barpoint_staff_bot"
CODE: Final = "1-A7K9QX4M"
NOW: Final = dt.datetime(2026, 8, 14, 9, 0, tzinfo=dt.UTC)
#: The venue the roster screen reads the expiry of a code in (TZ 3.4).
TIMEZONE: Final = "Europe/Moscow"

OWNER: Final = make_actor(MemberRole.OWNER, user_id=13, telegram_id=1_001)


# --------------------------------------------------------------------------------------
# A bot that also answers `getMe`, and the rest of the harness
# --------------------------------------------------------------------------------------


class MeSession(RecordingSession):
    """`RecordingSession` plus `getMe`: the deep link of TZ 5.1 needs the bot's username."""

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        if isinstance(method, GetMe):
            self.calls.append(method)
            return TelegramUser(
                id=BOT_ID,
                is_bot=True,
                first_name="BarPoint",
                username=BOT_USERNAME,
            )
        return await super().make_request(bot, method, timeout)


def make_named_bot() -> Bot:
    return Bot(token=BOT_TOKEN, session=MeSession())


def make_state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=MANAGER_TELEGRAM_ID),
    )


def make_venue(timezone: str = TIMEZONE) -> Venue:
    """The venue the screen reads its clock from; nothing else on it is looked at."""
    return Venue(id=VENUE_ID, name="PIMS", city="Moscow", timezone=timezone, is_active=True)


def make_entry(
    member_id: int = 7,
    *,
    full_name: str = "Ivan Ivanov",
    role: MemberRole = MemberRole.STAFF,
    position: str | None = None,
    is_active: bool = True,
    is_bot_blocked: bool = False,
) -> RosterEntry:
    """One roster line, in memory: nothing here reaches a database."""
    return RosterEntry(
        member=VenueMember(
            id=member_id,
            venue_id=VENUE_ID,
            user_id=100 + member_id,
            role=role,
            position=position,
            is_active=is_active,
        ),
        user=User(
            id=100 + member_id,
            telegram_id=900 + member_id,
            full_name=full_name,
            is_active=True,
            is_bot_blocked=is_bot_blocked,
        ),
    )


def make_code(code_id: int = 5, *, role: MemberRole = MemberRole.STAFF) -> InviteCode:
    return InviteCode(
        id=code_id,
        venue_id=VENUE_ID,
        code=CODE,
        role=role,
        expires_at=NOW + dt.timedelta(days=7),
    )


class FakeRoster:
    """`MemberService` as this module uses it, with every call written down.

    `list_roster` answers with the **active** members only, exactly as the real service
    does (`VenueMemberRepo.list_active`). Copying that predicate is what lets
    `test_switching_an_employee_off_keeps_the_history` tell "the row is gone" from "the row
    is no longer listed" — the two are different facts, and only the second one is true.
    """

    def __init__(
        self,
        entries: Sequence[RosterEntry] = (),
        *,
        refusal: Exception | None = None,
    ) -> None:
        self.table = list(entries)
        self.calls: list[tuple[str, int]] = []
        #: What the service refuses with, when a test is about the refusal rather than the
        #: change. Reproduced and not stubbed: the screen has to name each one.
        self.refusal = refusal

    async def list_roster(self, actor: AccessContext) -> tuple[RosterEntry, ...]:
        self.calls.append(("list_roster", 0))
        return tuple(entry for entry in self.table if entry.is_active)

    async def get(self, actor: AccessContext, member_id: int) -> RosterEntry | None:
        self.calls.append(("get", member_id))
        return next((entry for entry in self.table if entry.member_id == member_id), None)

    async def deactivate(self, actor: AccessContext, member_id: int) -> RosterEntry:
        self.calls.append(("deactivate", member_id))
        return self._set_active(member_id, is_active=False)

    async def reactivate(self, actor: AccessContext, member_id: int) -> RosterEntry:
        self.calls.append(("reactivate", member_id))
        return self._set_active(member_id, is_active=True)

    async def set_role(self, actor: AccessContext, member_id: int, role: MemberRole) -> RosterEntry:
        self.calls.append(("set_role", member_id))
        if self.refusal is not None:
            raise self.refusal
        entry = self._entry(member_id)
        entry.member.role = role
        return entry

    async def rename(self, actor: AccessContext, member_id: int, full_name: str) -> RosterEntry:
        self.calls.append(("rename", member_id))
        if self.refusal is not None:
            raise self.refusal
        entry = self._entry(member_id)
        assert entry.user is not None
        entry.user.full_name = full_name
        return entry

    async def set_position(
        self, actor: AccessContext, member_id: int, position: str | None
    ) -> RosterEntry:
        self.calls.append(("set_position", member_id))
        if self.refusal is not None:
            raise self.refusal
        entry = self._entry(member_id)
        entry.member.position = position
        return entry

    def _entry(self, member_id: int) -> RosterEntry:
        return next(entry for entry in self.table if entry.member_id == member_id)

    def _set_active(self, member_id: int, *, is_active: bool) -> RosterEntry:
        entry = next(entry for entry in self.table if entry.member_id == member_id)
        # TZ 5.1: the flag moves, the row stays where it is — deletion is not an option
        # the service offers, and this fake offers none either.
        entry.member.is_active = is_active
        return entry


class FakeStaff:
    """`data["services"]`, narrowed to what these screens read."""

    def __init__(self, roster: FakeRoster) -> None:
        self.members = roster


class FakeInvites:
    """The invite half of `AccessService` (TZ 5.1), recorded."""

    def __init__(
        self,
        *,
        code: InviteCode | None = None,
        pending: Sequence[InviteCode] = (),
    ) -> None:
        self.code = code if code is not None else make_code()
        self.issued: list[dict[str, Any]] = []
        self.revoked: list[int] = []
        self.known_codes = {self.code.id}
        #: Codes nobody has used yet — what the roster screen lists under the people.
        self.pending = list(pending)

    async def list_pending_invite_codes(self, actor: AccessContext) -> Sequence[InviteCode]:
        return tuple(self.pending)

    async def issue_invite_code(
        self,
        actor: AccessContext,
        *,
        role: MemberRole,
        now: dt.datetime,
        position: str | None = None,
        full_name: str | None = None,
    ) -> InviteCode:
        self.issued.append({"role": role, "now": now, "position": position, "full_name": full_name})
        return self.code

    async def revoke_invite_code(
        self,
        actor: AccessContext,
        code_id: int,
        *,
        now: dt.datetime,
    ) -> InviteCode | None:
        self.revoked.append(code_id)
        return self.code if code_id in self.known_codes else None


def _the_real_services_satisfy_the_protocols(
    services: VenueServices,
    access: AccessService,
) -> tuple[StaffServices, Invites]:
    """mypy is the assertion here, and it is the point of the protocols.

    The handlers are typed against `StaffServices` and `Invites` so a fake can be built
    without a session; that only stays honest while the real services still satisfy them.
    Nothing calls this function — it fails at type-check time or not at all.
    """
    return services, access


def staff_services(
    *entries: RosterEntry,
    refusal: Exception | None = None,
) -> tuple[FakeStaff, FakeRoster]:
    roster = FakeRoster(entries, refusal=refusal)
    return FakeStaff(roster), roster


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
# The empty state (TZ 8.1) — the first thing every venue sees in this section
# --------------------------------------------------------------------------------------


def test_the_empty_roster_says_so_and_offers_the_button_that_ends_it() -> None:
    screen = roster_screen((), timezone=TIMEZONE)
    assert screen.text == f"{texts.STAFF_TITLE}\n\n{texts.STAFF_EMPTY}"
    assert texts.STAFF_ADD_BUTTON in captions(screen.markup)


def test_no_button_of_the_empty_roster_leads_nowhere() -> None:
    """TZ 8.1: «ни одной кнопки в никуда» — every payload parses and has a rule.

    A rule in the resolver is what makes a payload reachable at all: a factory without one
    is refused before any handler sees it, so a button carrying it is decoration.
    """
    for payload in payloads(roster_screen((), timezone=TIMEZONE).markup):
        assert type(parse(payload)) in RULES, payload


async def test_the_add_button_of_the_empty_roster_actually_starts_the_wizard() -> None:
    """The empty state is only done if its one button works — pressed, and not read."""
    bot = make_named_bot()
    state = make_state()
    empty = roster_screen((), timezone=TIMEZONE)
    pressed = next(
        payload
        for payload, caption in zip(payloads(empty.markup), captions(empty.markup), strict=True)
        if caption == texts.STAFF_ADD_BUTTON
    )
    resolution = await resolve(pressed, actor=MANAGER, repositories=FakeSubjects(VENUE_ID))
    assert resolution.is_allowed, "a manager may press the button their own empty screen drew"

    payload = parse(pressed)
    assert isinstance(payload, InviteRole)
    await start_invite(
        make_callback(pressed, bot=bot),
        bot=bot,
        state=state,
        callback_payload=payload,
        actor=MANAGER,
    )
    assert await state.get_state() == InviteWizard.full_name.state
    assert str(edits_of(bot)[-1].text) == texts.INVITE_NAME_PROMPT


# --------------------------------------------------------------------------------------
# The roster itself (TZ 5.8)
# --------------------------------------------------------------------------------------


def test_a_line_carries_the_position_the_role_and_both_marks() -> None:
    line = roster_line(
        make_entry(
            full_name="Ivan Ivanov",
            position="bartender",
            role=MemberRole.MANAGER,
            is_active=False,
            is_bot_blocked=True,
        )
    )
    assert line.startswith("Ivan Ivanov")
    assert "bartender" in line
    assert texts.role_label(MemberRole.MANAGER) in line
    # TZ 6 and TZ 5.1: an undelivered notification and a dismissed employee are two
    # different facts, and the manager reads both off the same line.
    assert texts.STAFF_BLOCKED_MARK in line
    assert texts.STAFF_DEACTIVATED_MARK in line


def test_a_line_without_a_position_does_not_pretend_to_have_one() -> None:
    line = roster_line(make_entry(full_name="Ivan Ivanov", position=None))
    assert line == texts.STAFF_LINE_TEMPLATE.format(
        full_name="Ivan Ivanov", role=texts.role_label(MemberRole.STAFF)
    )


async def test_the_roster_lists_everybody_with_a_button_each() -> None:
    bot = make_named_bot()
    services, roster = staff_services(
        make_entry(7, full_name="Ivan Ivanov"),
        make_entry(8, full_name="Petr Petrov", position="bartender"),
    )
    await open_staff(
        make_callback(OpenAdmin(section=AdminSection.STAFF).pack(), bot=bot),
        bot=bot,
        state=make_state(),
        actor=MANAGER,
        services=services,
        access=FakeInvites(),
        venue=make_venue(),
    )
    screen = edits_of(bot)[-1]
    assert "Ivan Ivanov" in str(screen.text)
    assert "Petr Petrov" in str(screen.text)
    opened = [
        parse(payload)
        for payload in payloads(markup_of(screen))
        if isinstance(parse(payload), MemberShow)
    ]
    assert opened == [MemberShow(member_id=7), MemberShow(member_id=8)]
    assert roster.calls == [("list_roster", 0)]


async def test_opening_the_section_ends_a_half_filled_wizard() -> None:
    """Arriving at the board is leaving the scenario; otherwise the next line typed in the
    chat would be read as somebody's name (TZ 8.2)."""
    bot = make_named_bot()
    state = make_state()
    await state.set_state(InviteWizard.full_name)
    services, _ = staff_services()
    await open_staff(
        make_callback(OpenAdmin(section=AdminSection.STAFF).pack(), bot=bot),
        bot=bot,
        state=state,
        actor=MANAGER,
        services=services,
        access=FakeInvites(),
        venue=make_venue(),
    )
    assert await state.get_state() is None


# --------------------------------------------------------------------------------------
# The card, and deactivation that keeps the history (TZ 5.1)
# --------------------------------------------------------------------------------------


async def test_the_card_shows_the_person_and_the_way_to_switch_them_off() -> None:
    bot = make_named_bot()
    services, _ = staff_services(make_entry(7, full_name="Ivan Ivanov", position="bartender"))
    await show_member(
        make_callback(MemberShow(member_id=7).pack(), bot=bot),
        bot=bot,
        actor=MANAGER,
        services=services,
        callback_payload=MemberShow(member_id=7),
    )
    screen = edits_of(bot)[-1]
    assert "Ivan Ivanov" in str(screen.text)
    assert texts.STAFF_DEACTIVATE_BUTTON in captions(markup_of(screen))


async def test_switching_an_employee_off_keeps_the_history() -> None:
    """TZ 5.1: `is_active` goes false and the row stays — checked on the call, not the row.

    The handler has no way to delete anything (the service does not offer one), so what is
    asserted is that it calls `deactivate` and nothing else, and that the membership is
    still in the fake's table afterwards with every id it had.
    """
    bot = make_named_bot()
    services, roster = staff_services(make_entry(7, full_name="Ivan Ivanov"))
    await set_member_active(
        make_callback(MemberActive(member_id=7, is_active=False).pack(), bot=bot),
        bot=bot,
        actor=MANAGER,
        services=services,
        callback_payload=MemberActive(member_id=7, is_active=False),
    )
    assert roster.calls == [("deactivate", 7)]
    assert [entry.member_id for entry in roster.table] == [7], "the row must survive"
    assert roster.table[0].is_active is False
    assert texts.STAFF_DEACTIVATED_MARK in str(edits_of(bot)[-1].text)
    # And the gap this reveals: the list of the section shows active members only, so the
    # mark has nowhere to appear until `MemberService` can list everybody (see the report).
    assert await roster.list_roster(MANAGER) == ()


async def test_an_employee_can_be_brought_back_to_the_same_row() -> None:
    bot = make_named_bot()
    services, roster = staff_services(make_entry(7, full_name="Ivan Ivanov", is_active=False))
    await set_member_active(
        make_callback(MemberActive(member_id=7, is_active=True).pack(), bot=bot),
        bot=bot,
        actor=MANAGER,
        services=services,
        callback_payload=MemberActive(member_id=7, is_active=True),
    )
    assert roster.calls == [("reactivate", 7)]
    assert roster.table[0].is_active is True


def test_the_card_button_asks_for_the_state_it_wants() -> None:
    """A press says «make them inactive», not «toggle whatever you find»: two managers on
    the same screen must not undo each other."""
    active = payloads(member_screen(make_entry(7), actor=OWNER).markup)
    assert MemberActive(member_id=7, is_active=False).pack() in active
    dismissed = payloads(member_screen(make_entry(7, is_active=False), actor=OWNER).markup)
    assert MemberActive(member_id=7, is_active=True).pack() in dismissed


def test_your_own_card_carries_no_switch() -> None:
    """TZ 8.1: the server refuses it, so the button must not be there to press.

    The owner of the test venue pressed exactly this button on exactly this card and lost
    every screen he had. Hiding it is not the protection — `require_not_self` is — but a
    button that always refuses is the broken button 8.1 is about.
    """
    mine = make_entry(OWNER.member_id, role=MemberRole.OWNER)

    buttons = payloads(member_screen(mine, actor=OWNER).markup)

    assert not any(button.startswith(MemberActive.__prefix__) for button in buttons)


def test_a_manager_sees_no_switch_on_the_owners_card() -> None:
    """TZ 2: `manager` may not switch off what it could not create."""
    chief = make_entry(9, role=MemberRole.OWNER)

    buttons = payloads(member_screen(chief, actor=MANAGER).markup)

    assert not any(button.startswith(MemberActive.__prefix__) for button in buttons)


def test_the_owner_still_sees_the_switch_on_somebody_elses_card() -> None:
    """The card loses the button in two cases and keeps it in every other one."""
    buttons = payloads(member_screen(make_entry(9), actor=OWNER).markup)

    assert MemberActive(member_id=9, is_active=False).pack() in buttons


# --------------------------------------------------------------------------------------
# The invite wizard (TZ 5.1, decision B8)
# --------------------------------------------------------------------------------------


async def test_issuing_a_code_walks_every_step_and_shows_the_link() -> None:
    """«Добавить» → ФИО → роль → должность → код, with the deep link of TZ 5.1."""
    bot = make_named_bot()
    state = make_state()
    access = FakeInvites()

    await start_invite(
        make_callback(InviteRole(role=INITIAL_ROLE).pack(), bot=bot),
        bot=bot,
        state=state,
        callback_payload=InviteRole(role=INITIAL_ROLE),
        actor=MANAGER,
    )
    assert str(edits_of(bot)[-1].text) == texts.INVITE_NAME_PROMPT

    await invite_name(make_message("Ivan Ivanov", bot=bot), state=state, actor=MANAGER)
    assert await state.get_state() == InviteWizard.role.state
    assert str(sends_of(bot)[-1].text) == texts.INVITE_ROLE_PROMPT

    await choose_role(
        make_callback(InviteRole(role=MemberRole.STAFF).pack(), bot=bot),
        bot=bot,
        state=state,
        callback_payload=InviteRole(role=MemberRole.STAFF),
        actor=MANAGER,
    )
    assert await state.get_state() == InviteWizard.position.state
    assert str(edits_of(bot)[-1].text) == texts.INVITE_POSITION_PROMPT

    await skip_position(
        make_callback(InviteRole(role=MemberRole.STAFF).pack(), bot=bot),
        bot=bot,
        state=state,
        actor=MANAGER,
        access=access,
        callback_payload=InviteRole(role=MemberRole.STAFF),
    )

    assert access.issued == [
        {
            "role": MemberRole.STAFF,
            "now": access.issued[0]["now"],
            "position": None,
            "full_name": "Ivan Ivanov",
        }
    ]
    assert access.issued[0]["now"].tzinfo is not None, "decision D12: instants are aware UTC"

    shown = str(edits_of(bot)[-1].text)
    assert CODE in shown
    assert (
        DEEPLINK_URL_TEMPLATE.format(username=BOT_USERNAME, payload=invite_deeplink_payload(CODE))
        in shown
    )
    assert texts.INVITE_HINT in shown, "TZ 5.1: single use, seven days"
    assert texts.INVITE_REVOKE_BUTTON in captions(markup_of(edits_of(bot)[-1]))
    assert await state.get_state() is None, "the wizard is over the moment the code exists"


async def test_a_typed_position_reaches_the_code() -> None:
    bot = make_named_bot()
    state = make_state()
    access = FakeInvites()
    await state.set_state(InviteWizard.position)
    await state.update_data({"full_name": "Ivan Ivanov", "role": MemberRole.STAFF.value})

    await invite_position(
        make_message("bartender", bot=bot),
        state=state,
        actor=MANAGER,
        access=access,
        bot=bot,
    )
    assert access.issued[0]["position"] == "bartender"
    assert CODE in str(sends_of(bot)[-1].text)


async def test_a_blank_name_is_asked_for_again_and_nothing_is_remembered() -> None:
    """The schedule matches people by name (TZ 5.3), so an empty one is not an answer."""
    bot = make_named_bot()
    state = make_state()
    await state.set_state(InviteWizard.full_name)
    await invite_name(make_message("   ", bot=bot), state=state, actor=MANAGER)
    assert await state.get_state() == InviteWizard.full_name.state
    assert str(sends_of(bot)[-1].text) == texts.INVITE_NAME_PROMPT
    assert await state.get_data() == {}


def test_a_manager_is_only_offered_the_roles_they_may_hand_out() -> None:
    """TZ 2: `manager` and `owner` codes are the owner's alone, so a manager is not shown a
    button that would answer with a refusal (TZ 8.1)."""
    assert issuable_roles(MANAGER) == (MemberRole.STAFF,)
    assert issuable_roles(OWNER) == tuple(MemberRole)


async def test_a_manager_cannot_smuggle_a_higher_role_through_the_payload() -> None:
    """The button is not the check: a hand-made `InviteRole(owner)` from a manager is put
    back to what they may actually issue, and the service refuses it again anyway (TZ 9)."""
    bot = make_named_bot()
    state = make_state()
    access = FakeInvites()
    await state.set_state(InviteWizard.position)
    await state.update_data({"full_name": "Ivan Ivanov"})
    await skip_position(
        make_callback(InviteRole(role=MemberRole.OWNER).pack(), bot=bot),
        bot=bot,
        state=state,
        actor=MANAGER,
        access=access,
        callback_payload=InviteRole(role=MemberRole.OWNER),
    )
    assert access.issued[0]["role"] == MemberRole.STAFF


async def test_a_role_button_pressed_in_a_step_it_does_not_belong_to_is_answered() -> None:
    """TZ 9: an action answers in a second, even when the answer is «this screen is old»."""
    bot = make_named_bot()
    await stale_invite_step(make_callback(InviteRole(role=MemberRole.STAFF).pack(), bot=bot))
    assert alerts_of(bot) == [texts.ERROR_OUTDATED_SCREEN]
    assert edits_of(bot) == []


# --------------------------------------------------------------------------------------
# Withdrawing a code (TZ 5.1)
# --------------------------------------------------------------------------------------


async def test_revoking_a_code_calls_the_service_and_says_so() -> None:
    bot = make_named_bot()
    access = FakeInvites()
    await revoke_code(
        make_callback(InviteRevoke(code_id=5).pack(), bot=bot),
        bot=bot,
        actor=MANAGER,
        access=access,
        callback_payload=InviteRevoke(code_id=5),
    )
    assert access.revoked == [5]
    assert str(edits_of(bot)[-1].text) == texts.INVITE_REVOKED


async def test_a_code_the_venue_does_not_own_is_refused_like_everything_else() -> None:
    """`InviteRevoke` reaches the handler without a row — the resolver's rule says why — so
    the venue check is the service's, and its `None` is the flat refusal of TZ 9."""
    bot = make_named_bot()
    access = FakeInvites()
    await revoke_code(
        make_callback(InviteRevoke(code_id=999).pack(), bot=bot),
        bot=bot,
        actor=MANAGER,
        access=access,
        callback_payload=InviteRevoke(code_id=999),
    )
    assert access.revoked == [999]
    assert alerts_of(bot) == [texts.ERROR_NOT_ALLOWED]
    assert edits_of(bot) == []


# --------------------------------------------------------------------------------------
# Rights: `staff` never reaches this module at all (TZ 2, 9)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        OpenAdmin(section=AdminSection.STAFF),
        InviteRole(role=MemberRole.STAFF),
        InviteRevoke(code_id=1),
        MemberShow(member_id=7),
        MemberActive(member_id=7, is_active=False),
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
        OpenAdmin(section=AdminSection.STAFF).pack(),
        actor=MANAGER,
        repositories=FakeSubjects(VENUE_ID),
    )
    assert resolution.is_allowed


def test_the_router_is_named_and_registers_the_screens() -> None:
    instance = router()
    assert instance.name == "admin_staff"
    assert len(instance.callback_query.handlers) == 11
    assert len(instance.message.handlers) == 3


# --------------------------------------------------------------------------------------
# Codes that are still waiting (TZ 5.1, 5.8)
# --------------------------------------------------------------------------------------
#
# Issued codes used to exist on exactly one screen — the one that had just shown them. A
# manager who pressed «Назад» could no longer see the code, and could no longer withdraw
# it, for the seven days it stayed alive. On the live stand that turned a link forwarded to
# the wrong person into something nobody could stop.


def make_pending(code_id: int = 5, *, full_name: str | None = "Мария Сидорова") -> InviteCode:
    return InviteCode(
        id=code_id,
        venue_id=VENUE_ID,
        code=f"{VENUE_ID}-A7K9QX4{code_id}",
        role=MemberRole.STAFF,
        full_name=full_name,
        # Late enough in the UTC day that a far-eastern venue is already on the next one —
        # which is the whole point of `test_the_expiry_is_read_in_the_venues_own_clock`.
        expires_at=dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.UTC),
    )


async def test_the_roster_shows_the_codes_nobody_has_used_yet() -> None:
    bot = make_named_bot()
    services, _ = staff_services(make_entry(7, full_name="Ivan Ivanov"))

    await open_staff(
        make_callback(OpenAdmin(section=AdminSection.STAFF).pack(), bot=bot),
        bot=bot,
        state=make_state(),
        actor=MANAGER,
        services=services,
        access=FakeInvites(pending=[make_pending()]),
        venue=make_venue(),
    )

    text = str(edits_of(bot)[-1].text)
    assert texts.STAFF_PENDING_TITLE in text
    assert "Мария Сидорова" in text


async def test_every_waiting_code_can_be_withdrawn_from_the_roster() -> None:
    """The point of the block: the withdraw button outlives the screen that issued it."""
    bot = make_named_bot()
    services, _ = staff_services()

    await open_staff(
        make_callback(OpenAdmin(section=AdminSection.STAFF).pack(), bot=bot),
        bot=bot,
        state=make_state(),
        actor=MANAGER,
        services=services,
        access=FakeInvites(pending=[make_pending(5), make_pending(6)]),
        venue=make_venue(),
    )

    buttons = payloads(edits_of(bot)[-1].reply_markup)
    assert InviteRevoke(code_id=5).pack() in buttons
    assert InviteRevoke(code_id=6).pack() in buttons


def test_a_venue_with_no_waiting_codes_gets_no_heading() -> None:
    """TZ 8.1: a heading over an empty list is noise on every new venue's first screen."""
    screen = roster_screen((), timezone=TIMEZONE)

    assert texts.STAFF_PENDING_TITLE not in screen.text


def test_the_expiry_is_read_in_the_venues_own_clock() -> None:
    """TZ 3.4: the column is UTC and the screen is the venue's.

    22.08 20:00 UTC is already 23.08 in Kamchatka, and a manager there must not be told
    the code dies a day early.
    """
    code = make_pending()

    moscow = roster_screen((), [code], timezone="Europe/Moscow")
    kamchatka = roster_screen((), [code], timezone="Asia/Kamchatka")

    assert "22.08" in moscow.text
    assert "23.08" in kamchatka.text


def test_a_code_issued_without_a_name_is_still_listed() -> None:
    """The name step may be skipped, and a line with an empty gap reads as a bug."""
    screen = roster_screen((), [make_pending(full_name=None)], timezone=TIMEZONE)

    assert texts.STAFF_INVITE_NO_NAME in screen.text


# --------------------------------------------------------------------------------------
# The card: role, name, position (TZ 5.8)
# --------------------------------------------------------------------------------------
#
# `MemberService.set_role`, `rename` and `set_position` were written, tested and reachable
# from nowhere: the card carried two buttons, and neither of them was any of these. TZ 5.8
# asks for «сменить роль/должность» in so many words, and decision B8 promises the manager
# that a name can be corrected later — a promise the product could not keep.


async def test_the_card_offers_the_roles_the_actor_may_hand_out() -> None:
    buttons = payloads(member_screen(make_entry(7), actor=OWNER).markup)

    for role in MemberRole:
        assert MemberSetRole(member_id=7, role=role).pack() in buttons


async def test_a_manager_is_not_offered_roles_they_cannot_grant() -> None:
    """TZ 2: `manager` hands out `staff` and nothing above it, so it sees one role."""
    buttons = payloads(member_screen(make_entry(7), actor=MANAGER).markup)

    assert MemberSetRole(member_id=7, role=MemberRole.STAFF).pack() in buttons
    assert MemberSetRole(member_id=7, role=MemberRole.MANAGER).pack() not in buttons
    assert MemberSetRole(member_id=7, role=MemberRole.OWNER).pack() not in buttons


async def test_your_own_card_offers_no_role_buttons_either() -> None:
    """The same rule that hides the switch hides everything else that changes rights."""
    mine = make_entry(OWNER.member_id, role=MemberRole.OWNER)

    buttons = payloads(member_screen(mine, actor=OWNER).markup)

    assert not any(button.startswith(MemberSetRole.__prefix__) for button in buttons)
    assert not any(button.startswith(MemberEdit.__prefix__) for button in buttons)


async def test_pressing_a_role_changes_it_and_redraws_the_card() -> None:
    bot = make_named_bot()
    services, roster = staff_services(make_entry(7, full_name="Ivan Ivanov"))

    await set_member_role(
        make_callback(MemberSetRole(member_id=7, role=MemberRole.MANAGER).pack(), bot=bot),
        bot=bot,
        actor=OWNER,
        services=services,
        callback_payload=MemberSetRole(member_id=7, role=MemberRole.MANAGER),
    )

    assert roster.calls == [("set_role", 7)]
    assert roster.table[0].role is MemberRole.MANAGER
    assert texts.role_label(MemberRole.MANAGER) in str(edits_of(bot)[-1].text)


@pytest.mark.parametrize(
    ("refusal", "expected"),
    [
        (SelfTargetError(7), texts.STAFF_SELF_ROLE_REFUSED),
        (LastOwnerError(VENUE_ID, 7), texts.STAFF_LAST_OWNER_REFUSED),
        (
            PermissionDeniedError(MemberRole.MANAGER, MemberRole.OWNER),
            texts.STAFF_OWNER_ONLY_REFUSED,
        ),
    ],
    ids=["self", "last owner", "outranked"],
)
async def test_each_refusal_of_a_role_change_says_what_it_is(
    refusal: Exception, expected: str
) -> None:
    """Three different reasons, three different sentences — not one generic apology.

    The card that offered the button may have been drawn before the rule existed or on
    another device, so the refusal is a normal answer and has to read like one (TZ 8.2).
    """
    bot = make_named_bot()
    services, _ = staff_services(make_entry(7), refusal=refusal)

    await set_member_role(
        make_callback(MemberSetRole(member_id=7, role=MemberRole.STAFF).pack(), bot=bot),
        bot=bot,
        actor=OWNER,
        services=services,
        callback_payload=MemberSetRole(member_id=7, role=MemberRole.STAFF),
    )

    assert [answer.text for answer in session_of(bot).answers()] == [expected]
    assert not edits_of(bot), "a refused press leaves the card exactly as it was"


async def test_renaming_asks_then_saves_onto_the_card_that_was_open() -> None:
    """Decision B8, finally kept. The id comes from the state, never from the message."""
    bot = make_named_bot()
    state = make_state()
    services, roster = staff_services(make_entry(7, full_name="Ivan Ivanov"))

    await start_member_edit(
        make_callback(MemberEdit(member_id=7, field=MemberField.FULL_NAME).pack(), bot=bot),
        bot=bot,
        state=state,
        callback_payload=MemberEdit(member_id=7, field=MemberField.FULL_NAME),
    )
    assert await state.get_state() == MemberEditing.full_name.state
    assert texts.STAFF_RENAME_PROMPT in str(edits_of(bot)[-1].text)

    await finish_member_edit(
        make_message("Пётр Петров", bot=bot),
        state=state,
        actor=MANAGER,
        services=services,
    )

    assert roster.calls[-1] == ("rename", 7)
    assert roster.table[0].full_name == "Пётр Петров"
    assert await state.get_state() is None


async def test_the_position_step_saves_through_the_other_service_call() -> None:
    bot = make_named_bot()
    state = make_state()
    services, roster = staff_services(make_entry(7))

    await start_member_edit(
        make_callback(MemberEdit(member_id=7, field=MemberField.POSITION).pack(), bot=bot),
        bot=bot,
        state=state,
        callback_payload=MemberEdit(member_id=7, field=MemberField.POSITION),
    )
    await finish_member_edit(
        make_message("бармен", bot=bot),
        state=state,
        actor=MANAGER,
        services=services,
    )

    assert roster.calls[-1] == ("set_position", 7)
    assert roster.table[0].position == "бармен"


async def test_an_empty_line_asks_again_instead_of_saving_nothing() -> None:
    bot = make_named_bot()
    state = make_state()
    services, roster = staff_services(make_entry(7, full_name="Ivan Ivanov"))
    await state.set_state(MemberEditing.full_name)
    await state.update_data({MEMBER_FIELD: 7})

    await finish_member_edit(
        make_message("   ", bot=bot),
        state=state,
        actor=MANAGER,
        services=services,
    )

    assert roster.calls == []
    assert await state.get_state() == MemberEditing.full_name.state
