"""Keyboards of the employees section — the roster, a card, the invite wizard (TZ 5.8).

Captions come from `src/bot/texts/admin.py`, payloads from `src/bot/callbacks.py`, and the
navigation row is never spelled here: `submenu()` and `wizard()` of
`src/bot/keyboards/menu.py` append it, which is what makes "every submenu has a back and a
home button" a property of the constructor rather than a habit of four screens (TZ 5.2, 8.2).

Two things about the navigation are decided here and are worth reading before editing.

**Back leads to the screen one came from, through that screen's own factory.** `Nav(BACK)`
without a section means the main menu, and this section sits two steps below it (menu ->
management -> employees), so a bare back button would take a manager past the board they
came from. :class:`~src.bot.callbacks.Nav` says how that is spelled — "a parent that needs
ids is reached through its own factory under the back caption" — and that is exactly what
:func:`back_to_management` and :func:`back_to_roster` do: the caption is `texts.BACK_BUTTON`
and the payload is the one that draws the parent screen. `submenu(..., back=False)` then
adds the home button alone, so the row never carries two buttons leading to two different
places under one caption.

**The `STAFF_ADD_BUTTON` press carries a role** (:func:`add_button`). The callback scheme
has no factory meaning "start the invite wizard", and adding one would touch a file two
other waves are writing in; :class:`~src.bot.callbacks.InviteRole` is the honest carrier,
because what the button starts *is* the issuing of a code, and the role it carries
(:data:`INITIAL_ROLE`) is the role that code will have unless the wizard's own role step
changes it. `src/bot/handlers/admin_staff.py` tells the three presses apart by FSM state and
documents the trade-off in full.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot import texts
from src.bot.callbacks import (
    AdminSection,
    InviteRevoke,
    InviteRole,
    MemberActive,
    MemberShow,
    MenuAction,
    OpenAdmin,
    OpenSection,
)
from src.bot.keyboards.menu import submenu, wizard
from src.db.models import MemberRole
from src.services.access import AccessContext, role_rank
from src.services.members import RosterEntry

#: The role «Добавить» starts a code with. TZ 2 lets every manager hand out `staff`, so this
#: is the one role the button is pressable with whoever is looking at the roster.
INITIAL_ROLE: Final = MemberRole.STAFF


def issuable_roles(actor: AccessContext) -> tuple[MemberRole, ...]:
    """Roles this actor may put on a code — the buttons of the role step (TZ 2).

    A mirror of the rule inside `AccessService.issue_invite_code`: a code for `manager` or
    `owner` needs an owner behind it. Drawing all three for a manager would be a button
    that answers with a refusal, which TZ 8.1 forbids as loudly as a button that leads
    nowhere. That the rule is written twice is a gap in the service layer and is reported
    as one — `AccessService` has no "what may I hand out" of its own.
    """
    if actor.is_owner:
        return tuple(MemberRole)
    return tuple(role for role in MemberRole if role_rank(role) < role_rank(MemberRole.MANAGER))


# --------------------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------------------


def member_button(entry: RosterEntry) -> InlineKeyboardButton:
    """One employee in the roster.

    The caption is the name and nothing else: the marks of TZ 5.8 (`STAFF_BLOCKED_MARK`,
    `STAFF_DEACTIVATED_MARK`) are printed in the message, where there is room for them. A
    membership whose `users` row did not come back has no name to show, and Telegram
    refuses an empty caption, so the role stands in — the button still opens the card that
    says who it is.
    """
    return InlineKeyboardButton(
        text=entry.full_name or texts.role_label(entry.role),
        callback_data=MemberShow(member_id=entry.member_id).pack(),
    )


def add_button() -> InlineKeyboardButton:
    """`STAFF_ADD_BUTTON` — the first step of the wizard (see the module docstring)."""
    return InlineKeyboardButton(
        text=texts.STAFF_ADD_BUTTON,
        callback_data=InviteRole(role=INITIAL_ROLE).pack(),
    )


def active_button(entry: RosterEntry) -> InlineKeyboardButton:
    """Switch an employee off, or bring them back (TZ 5.1: the row survives either way).

    The payload carries the state the press asks for, not the state the person is in, so a
    tap on a screen somebody else has already acted on is idempotent rather than a toggle
    of whatever it finds.
    """
    return InlineKeyboardButton(
        text=texts.STAFF_ACTIVATE_BUTTON if not entry.is_active else texts.STAFF_DEACTIVATE_BUTTON,
        callback_data=MemberActive(member_id=entry.member_id, is_active=not entry.is_active).pack(),
    )


def role_button(role: MemberRole) -> InlineKeyboardButton:
    """One role of the wizard's role step (TZ 5.1: the code is bound to a role)."""
    return InlineKeyboardButton(
        text=texts.role_label(role),
        callback_data=InviteRole(role=role).pack(),
    )


def skip_button(role: MemberRole) -> InlineKeyboardButton:
    """`INVITE_SKIP_BUTTON` on the position step: the position is optional (TZ 5.1).

    It carries the role for the same reason `add_button` does — the scheme has one factory
    for "this code will carry this role", and the step is told by the FSM state.
    """
    return InlineKeyboardButton(
        text=texts.INVITE_SKIP_BUTTON,
        callback_data=InviteRole(role=role).pack(),
    )


def revoke_button(code_id: int) -> InlineKeyboardButton:
    """Withdraw a code that has not been used yet (TZ 5.1)."""
    return InlineKeyboardButton(
        text=texts.INVITE_REVOKE_BUTTON,
        callback_data=InviteRevoke(code_id=code_id).pack(),
    )


def back_to_roster() -> InlineKeyboardButton:
    """Back to the employees list, from a card or from a freshly issued code."""
    return InlineKeyboardButton(
        text=texts.BACK_BUTTON,
        callback_data=OpenAdmin(section=AdminSection.STAFF).pack(),
    )


def back_to_management() -> InlineKeyboardButton:
    """Back to the board of the management section, which owns that screen end to end."""
    return InlineKeyboardButton(
        text=texts.BACK_BUTTON,
        callback_data=OpenSection(section=MenuAction.MANAGEMENT).pack(),
    )


# --------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------


def roster_keyboard(entries: Sequence[RosterEntry]) -> InlineKeyboardMarkup:
    """The employees list, with `STAFF_ADD_BUTTON` under it.

    The empty roster gets the same keyboard minus the people: TZ 8.1 wants the empty state
    to carry the button that ends it, and it is the same button either way.
    """
    rows: list[list[InlineKeyboardButton]] = [[member_button(entry)] for entry in entries]
    rows.append([add_button()])
    rows.append([back_to_management()])
    return submenu(*rows, back=False)


def member_keyboard(entry: RosterEntry) -> InlineKeyboardMarkup:
    """One employee's card: switch them off or back on, and return to the list."""
    return submenu([active_button(entry)], [back_to_roster()], back=False)


def name_keyboard() -> InlineKeyboardMarkup:
    """The name step, which is typed: nothing on it but the cancel button (TZ 8.2)."""
    return wizard(back=False)


def role_keyboard(roles: Sequence[MemberRole]) -> InlineKeyboardMarkup:
    """The role step — one button per role the actor may hand out."""
    return wizard([role_button(role) for role in roles], back=False)


def position_keyboard(role: MemberRole) -> InlineKeyboardMarkup:
    """The position step: typed, or skipped with the button TZ 5.1 asks for."""
    return wizard([skip_button(role)], back=False)


def code_keyboard(code_id: int) -> InlineKeyboardMarkup:
    """The issued code: withdraw it, or go back to the roster."""
    return submenu([revoke_button(code_id)], [back_to_roster()], back=False)


def revoked_keyboard() -> InlineKeyboardMarkup:
    """After a code was withdrawn there is nothing left to press but the way back."""
    return submenu([back_to_roster()], back=False)


__all__ = [
    "INITIAL_ROLE",
    "active_button",
    "add_button",
    "back_to_management",
    "back_to_roster",
    "code_keyboard",
    "issuable_roles",
    "member_button",
    "member_keyboard",
    "name_keyboard",
    "position_keyboard",
    "revoke_button",
    "revoked_keyboard",
    "role_button",
    "role_keyboard",
    "roster_keyboard",
    "skip_button",
]
