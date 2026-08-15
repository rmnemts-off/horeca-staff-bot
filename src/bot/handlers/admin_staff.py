"""Employees and invite codes (TZ 5.1, 5.8; plan task 27).

Four screens and one wizard. The list of who works here, with the marks TZ 5.8 asks for; a
person's card, where the only lifecycle action is switching access off **without touching a
single row of their history** (TZ 5.1); the wizard that issues a code for a named person
(decision B8); and the code itself, with the deep link of TZ 5.1 and the button that
withdraws it.

Every handler below is the shape TZ 3.2 requires — take the update, call a service, render
the screen. The roster and the marks come from `src/services/members.py`, the codes from
`src/services/access.py`, the wording from `src/bot/texts/admin.py` and the screens from
`src/bot/views/staff.py`, so that the worker and a later web panel can draw the same list
without going through aiogram.

Two decisions are this module's own and neither is invisible:

**The wizard is entered and skipped through `InviteRole`.** The callback scheme declares no
factory for "start issuing a code" and none for "skip this step", and both are needed by
wording that already exists (`STAFF_ADD_BUTTON`, `INVITE_SKIP_BUTTON`). Declaring two new
ones means editing `src/bot/callbacks.py` and the resolver's `RULES` — two files three
other waves are writing in this hour, and a factory without a rule fails the build. So the
one factory that already means "this code will carry this role" carries all three presses,
and the *step* is decided by the FSM state, which is what an FSM is for:

* no state at all — the press came off the roster: remember the role and ask for the name;
* `InviteWizard.role` — the role step proper: remember the role and ask for the position;
* `InviteWizard.position` — the skip: issue the code with no position.

The routing is by `StateFilter`, so the three cannot overlap, and a press that arrives in
none of those states (a screen left open while another wizard was started) is answered as
the stale screen it is instead of leaving a spinner on the button (TZ 9). It is still a
workaround: the report of this task asks for `InviteNew` and `InviteSkip` in the scheme.

**Deactivation is `MemberService.deactivate` and nothing else.** TZ 5.1 keeps the row so
that every checklist run, shift and write-off of that person stays in the reports; the
service is the only place that is written down, and `tests/bot/test_admin_staff.py` asserts
this module calls it rather than anything that removes a row.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Final, Protocol

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot import texts
from src.bot.callbacks import (
    AdminSection,
    InviteRevoke,
    InviteRole,
    MemberActive,
    MemberShow,
    OpenAdmin,
)
from src.bot.keyboards.staff import issuable_roles
from src.bot.safe_edit import safe_edit
from src.bot.states import InviteWizard
from src.bot.views import Screen
from src.bot.views.staff import (
    deeplink,
    invite_code_screen,
    invite_name_screen,
    invite_position_screen,
    invite_revoked_screen,
    invite_role_screen,
    member_screen,
    roster_screen,
)
from src.db.models import InviteCode, MemberRole, Venue
from src.services.access import (
    AccessContext,
    PermissionDeniedError,
    SelfTargetError,
    invite_deeplink_payload,
)
from src.services.members import RosterEntry
from src.services.timezones import utc_now

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "admin_staff"

#: Keys of `state.get_data()`, named after the steps of `InviteWizard` that fill them.
NAME_FIELD: Final = "full_name"
ROLE_FIELD: Final = "role"


class Roster(Protocol):
    """What this module asks of `MemberService` (TZ 5.8), and no more.

    Narrower than the service on purpose, the way the resolver's `SubjectRepositories` is
    narrower than `VenueRepositories`: the roster of a venue is the thing under test here,
    and a test that had to build a real `MemberService` would have to build a session to
    build a repository to build it. `tests/bot/test_admin_staff.py` checks with mypy that
    the real service still satisfies this.
    """

    async def list_roster(self, actor: AccessContext) -> tuple[RosterEntry, ...]: ...

    async def get(self, actor: AccessContext, member_id: int) -> RosterEntry | None: ...

    async def deactivate(self, actor: AccessContext, member_id: int) -> RosterEntry: ...

    async def reactivate(self, actor: AccessContext, member_id: int) -> RosterEntry: ...


class StaffServices(Protocol):
    """The one service of `data["services"]` these screens use."""

    @property
    def members(self) -> Roster: ...


class Invites(Protocol):
    """The half of `AccessService` that issues and withdraws codes (TZ 5.1, plan task 15).

    `now` is an argument and never `datetime.now()` inside the service — decision D12 — so
    the handler is the one that reads the clock, through `src/services/timezones.py`.
    """

    async def issue_invite_code(
        self,
        actor: AccessContext,
        *,
        role: MemberRole,
        now: dt.datetime,
        position: str | None = None,
        full_name: str | None = None,
    ) -> InviteCode: ...

    async def revoke_invite_code(
        self,
        actor: AccessContext,
        code_id: int,
        *,
        now: dt.datetime,
    ) -> InviteCode | None: ...

    async def list_pending_invite_codes(self, actor: AccessContext) -> Sequence[InviteCode]: ...


# --------------------------------------------------------------------------------------
# Delivery: one screen, edited in place (TZ 8.2)
# --------------------------------------------------------------------------------------


async def _render(bot: Bot, callback: CallbackQuery, screen: Screen) -> None:
    """Rewrite the message the button was pressed on, and close the spinner (TZ 8.2, 9)."""
    origin = callback.message
    if origin is None:
        # Telegram gives no message with a press on a very old inline keyboard. There is
        # nothing to edit, and sending a screen into a chat we cannot name is worse.
        await callback.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
        return
    await safe_edit(
        bot,
        chat_id=origin.chat.id,
        message_id=origin.message_id,
        text=screen.text,
        reply_markup=screen.markup,
        answer=callback.answer,
    )


async def _reply(message: Message, screen: Screen) -> None:
    """A step answered by typing continues in a new message: the old one is the question."""
    await message.answer(screen.text, reply_markup=screen.markup)


# --------------------------------------------------------------------------------------
# The roster (TZ 5.8)
# --------------------------------------------------------------------------------------


async def open_staff(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    services: StaffServices,
    access: Invites,
    venue: Venue,
) -> None:
    """The employees list and the codes still waiting, empty state included (TZ 5.8, 8.1).

    The state is dropped first: this screen is the section's board, and arriving at it from
    the code the wizard just issued — or from a menu press that interrupted it — must not
    leave a half-filled scenario waiting for the manager's next line.
    """
    await state.clear()
    await _render(
        bot,
        callback,
        roster_screen(
            await services.members.list_roster(actor),
            await access.list_pending_invite_codes(actor),
            timezone=venue.timezone,
        ),
    )


async def show_member(
    callback: CallbackQuery,
    bot: Bot,
    actor: AccessContext,
    services: StaffServices,
    callback_payload: MemberShow,
) -> None:
    """One employee's card.

    The resolver has already fetched the membership through the venue's repository and
    refused a foreign id (TZ 9); the service is asked again because the card shows the
    person's *name*, which lives on the global `users` table and is not on that row.
    """
    entry = await services.members.get(actor, callback_payload.member_id)
    if entry is None:
        await callback.answer(texts.ERROR_NOT_ALLOWED, show_alert=True)
        return
    await _render(bot, callback, member_screen(entry, actor=actor))


async def set_member_active(
    callback: CallbackQuery,
    bot: Bot,
    actor: AccessContext,
    services: StaffServices,
    callback_payload: MemberActive,
) -> None:
    """The employee left, or came back (TZ 5.1).

    Neither branch deletes anything: `venue_members.is_active` goes false and the row —
    with every run, shift and write-off hanging off it — stays exactly where it was.

    The two refusals are caught and named. Neither is a failure and neither may read as
    one: the card that offered the button was drawn before the rule existed, or on another
    device, and the generic apology over a deliberate refusal sends the manager looking for
    a bug that is not there (TZ 8.2). The rule itself stays in the service — this only puts
    a sentence on it.
    """
    try:
        if callback_payload.is_active:
            entry = await services.members.reactivate(actor, callback_payload.member_id)
        else:
            entry = await services.members.deactivate(actor, callback_payload.member_id)
    except SelfTargetError:
        await callback.answer(texts.STAFF_SELF_DEACTIVATE_REFUSED, show_alert=True)
        return
    except PermissionDeniedError:
        await callback.answer(texts.STAFF_OWNER_ONLY_REFUSED, show_alert=True)
        return
    await _render(bot, callback, member_screen(entry, actor=actor))


# --------------------------------------------------------------------------------------
# The invite wizard (TZ 5.1, decision B8)
# --------------------------------------------------------------------------------------


async def start_invite(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    callback_payload: InviteRole,
    actor: AccessContext,
) -> None:
    """`STAFF_ADD_BUTTON`: the code starts as `staff`, and the name is asked for (B8).

    See the module docstring for why the button carries a role at all. Somebody who may not
    hand out even that role never gets here — `InviteRole` needs a manager in the resolver.
    """
    await state.set_state(InviteWizard.full_name)
    await state.update_data({ROLE_FIELD: _role_of(callback_payload, actor)})
    await _render(bot, callback, invite_name_screen())


async def invite_name(message: Message, state: FSMContext, actor: AccessContext) -> None:
    """The name the employee will be known by in the schedule (decision B8).

    An empty line is not an answer, and is asked again rather than saved: the name is what
    the schedule import matches people by (TZ 5.3), so a blank one is a person nobody can
    be put on a shift.
    """
    full_name = (message.text or "").strip()
    if not full_name:
        await _reply(message, invite_name_screen())
        return
    await state.update_data({NAME_FIELD: full_name})
    await state.set_state(InviteWizard.role)
    await _reply(message, invite_role_screen(issuable_roles(actor)))


async def choose_role(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    callback_payload: InviteRole,
    actor: AccessContext,
) -> None:
    """The role step (TZ 5.1: a code is bound to venue, role and position)."""
    role = _role_of(callback_payload, actor)
    await state.update_data({ROLE_FIELD: role})
    await state.set_state(InviteWizard.position)
    await _render(bot, callback, invite_position_screen(MemberRole(role)))


async def invite_position(
    message: Message,
    state: FSMContext,
    actor: AccessContext,
    access: Invites,
    bot: Bot,
) -> None:
    """A position was typed: the last thing the code needed."""
    position = (message.text or "").strip() or None
    await _reply(message, await _issue(state, actor, access, bot, position=position))


async def skip_position(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    access: Invites,
    callback_payload: InviteRole,
) -> None:
    """The skip button: TZ 5.1 makes the position optional, so the code goes out without."""
    await state.update_data({ROLE_FIELD: _role_of(callback_payload, actor)})
    await _render(bot, callback, await _issue(state, actor, access, bot, position=None))


async def stale_invite_step(callback: CallbackQuery) -> None:
    """A role button pressed in a state it does not belong to (TZ 9: always answer).

    The screens of this wizard are edited in place, so the only way to reach this is a
    message that was superseded — a second copy of the roster left open while a scenario
    was started elsewhere. Nothing is issued, and the spinner is closed with the truth.
    """
    await callback.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)


async def revoke_code(
    callback: CallbackQuery,
    bot: Bot,
    actor: AccessContext,
    access: Invites,
    callback_payload: InviteRevoke,
) -> None:
    """Withdraw a code before anybody used it (TZ 5.1).

    `InviteRevoke` is the one factory of this module the resolver hands over without a row,
    and its rule says why: invite codes are addressed by the code a person types, so there
    is no lookup by id outside the access service. The service is built around the actor's
    venue and answers `None` for a code of any other, which is refused here in the same
    wording every other refusal uses (TZ 9).
    """
    withdrawn = await access.revoke_invite_code(actor, callback_payload.code_id, now=utc_now())
    if withdrawn is None:
        await callback.answer(texts.ERROR_NOT_ALLOWED, show_alert=True)
        return
    await _render(bot, callback, invite_revoked_screen())


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


def _role_of(payload: InviteRole, actor: AccessContext) -> str:
    """The role a press asks for, as it is kept in the FSM state.

    Stored as the enum's value and not as the enum: the state lives in Redis as JSON
    (`src/bot/dispatcher.py`), and a member of an enum does not survive that round trip.

    A role this actor may not hand out is read as `staff` — the role every manager may
    issue — because such a payload cannot have come off a screen this module drew
    (`issuable_roles` never puts it there) and is therefore either a stale keyboard from
    when the presser was an owner, or a hand-made string. Neither is a reason to refuse a
    manager the thing they are allowed to do, and it is not the check either:
    `AccessService.issue_invite_code` decides on the server, and would refuse the raw role
    (TZ 2, TZ 9).
    """
    if payload.role in issuable_roles(actor):
        return payload.role.value
    return MemberRole.STAFF.value


async def _issue(
    state: FSMContext,
    actor: AccessContext,
    access: Invites,
    bot: Bot,
    *,
    position: str | None,
) -> Screen:
    """Hand out the code the wizard has been collecting, and draw it (TZ 5.1).

    The state is cleared before the screen is built rather than after: the wizard is over
    the moment the code exists, and a failure while rendering must not leave the manager in
    a step that would issue a second one.
    """
    collected = await state.get_data()
    full_name = str(collected.get(NAME_FIELD, ""))
    role = MemberRole(str(collected.get(ROLE_FIELD, MemberRole.STAFF.value)))
    await state.clear()
    code = await access.issue_invite_code(
        actor,
        role=role,
        now=utc_now(),
        position=position,
        full_name=full_name or None,
    )
    return invite_code_screen(
        full_name=full_name,
        code=code.code,
        # The username of the running bot is the only correct source of the deep link: one
        # binary serves several deployments, and a name in the configuration would build
        # links into whichever bot the file was copied from (TZ 5.1).
        link=deeplink((await bot.me()).username or "", invite_deeplink_payload(code.code)),
        code_id=code.id,
    )


def router() -> Router:
    """The screens of this block (see the module docstring).

    Order matters twice. The three `InviteRole` handlers are told apart by state and the
    catch-all sits after them, so a press in a known step never reaches the apology; and
    the message steps are filtered by state, so nothing typed outside the wizard is read as
    a name (a main-menu press is intercepted before any of this by
    `src/bot/middlewares/menu.py`).
    """
    instance = Router(name=ROUTER_NAME)
    instance.callback_query.register(open_staff, OpenAdmin.filter(F.section == AdminSection.STAFF))
    instance.callback_query.register(show_member, MemberShow.filter())
    instance.callback_query.register(set_member_active, MemberActive.filter())
    instance.callback_query.register(start_invite, InviteRole.filter(), StateFilter(None))
    instance.callback_query.register(
        choose_role, InviteRole.filter(), StateFilter(InviteWizard.role)
    )
    instance.callback_query.register(
        skip_position, InviteRole.filter(), StateFilter(InviteWizard.position)
    )
    instance.callback_query.register(stale_invite_step, InviteRole.filter())
    instance.callback_query.register(revoke_code, InviteRevoke.filter())
    instance.message.register(invite_name, StateFilter(InviteWizard.full_name))
    instance.message.register(invite_position, StateFilter(InviteWizard.position))
    return instance


__all__ = [
    "NAME_FIELD",
    "ROLE_FIELD",
    "ROUTER_NAME",
    "Invites",
    "Roster",
    "StaffServices",
    "choose_role",
    "invite_name",
    "invite_position",
    "open_staff",
    "revoke_code",
    "router",
    "set_member_active",
    "show_member",
    "skip_position",
    "stale_invite_step",
    "start_invite",
]
