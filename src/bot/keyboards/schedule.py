"""Keyboards of the manager's schedule (TZ 5.3, 5.8; plan task 29).

Captions come from `src/bot/texts/admin.py`, payloads from `src/bot/callbacks.py`, and the
navigation row is never spelled out here — `keyboards/menu.py` and `keyboards/admin.py`
append it, which is what makes "every submenu has a back and a home" a property of the
constructor rather than a habit of six screens (TZ 5.2, 8.2).

Three decisions of this module are worth reading before editing.

**`SCHEDULE_ADD_BUTTON` travels on :class:`~src.bot.callbacks.TimezoneChoice`, at a negative
index.** The callback scheme declares no factory meaning "start the shift wizard", and every
factory it does declare is either taken by another module or carries an id this button has
none of: a shift that does not exist yet has no `shift_id`, and a wizard that has not asked
who works yet has no `member_id`. Declaring a new factory means editing
`src/bot/callbacks.py` **and** the resolver's `RULES` — two files other waves are writing in
this hour, and a factory without a rule fails the build. So this press uses the same stopgap
`src/bot/keyboards/admin.py` documents for its three settings buttons
(:class:`~src.bot.keyboards.admin.VenueCommand`): a negative index can never name a zone
(`timezone_at` answers `None` below zero), and the index chosen here is distinct from every
one of theirs. The moment `callbacks.py` gains a `ShiftNew`, :class:`ScheduleCommand` and one
registration in `src/bot/handlers/admin_schedule.py` go away. Reported with plan task 29.

**The default-window button carries the person, not the times.** A wall clock reading is
text, and the scheme refuses text in a payload (rule 2 of `src/bot/callbacks.py`); the times
themselves come from `venue_settings` on the server, which is where the venue's default
lives (TZ 4.2). What the button needs to say is *whose* shift is being saved, and
:class:`~src.bot.callbacks.ShiftMember` is exactly that — the same factory the person step
draws, told apart from it by the FSM state, the way `admin_staff.py` tells its three
`InviteRole` presses apart.

**The card's back button is spelled here.** `Nav(BACK)` means the main menu and
`keyboards.admin.block()` returns to the board of the section; a shift card is one step below
the schedule, so its back button carries the schedule's own `OpenAdmin` payload and the
navigation row is asked for `home` alone. Two buttons under one caption leading to two places
is the failure `keyboards/staff.py` documents at length, and this is the same fix.
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Sequence
from typing import Final

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
)
from src.bot.keyboards.admin import block
from src.bot.keyboards.menu import submenu, wizard
from src.bot.views.shifts import format_date, format_window, window_of
from src.services.members import RosterEntry
from src.services.shifts import ShiftView

#: Between the two facts a shift button carries. The same separator the roster lines of
#: `src/bot/views/staff.py` use, so the two lists read as one product.
BUTTON_SEPARATOR: Final = " · "


class ScheduleCommand(enum.IntEnum):
    """A press of this block that names no timezone (see the module docstring).

    The value is deliberately far from `VenueCommand`'s -1..-4: several modules share this
    one factory as a stopgap, they are being written in parallel, and two of them landing on
    the same index would route one block's button into the other's handler. -29 is the number
    of the plan task this block is, which is as close to a namespace as a stopgap gets.
    """

    ADD_SHIFT = -29


# --------------------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------------------


def shift_button(view: ShiftView) -> InlineKeyboardButton:
    """One shift of the fortnight, opening its card (TZ 5.8).

    The date is on the caption and not only in the message, because the button rows sit
    under a list of several days, and one name twice in a column says nothing about which of
    the two shifts is which. A shift whose `users` row did not come back has no name to
    show and Telegram refuses an empty caption, so the window stands in — the card behind
    the button says the rest.
    """
    caption = BUTTON_SEPARATOR.join(
        (format_date(view.shift_date), view.full_name or window_of(view))
    )
    return InlineKeyboardButton(
        text=caption,
        callback_data=ShiftShow(shift_id=view.shift_id).pack(),
    )


def add_button() -> InlineKeyboardButton:
    """`SCHEDULE_ADD_BUTTON` — the first step of the wizard (see the module docstring)."""
    return InlineKeyboardButton(
        text=texts.SCHEDULE_ADD_BUTTON,
        callback_data=TimezoneChoice(index=ScheduleCommand.ADD_SHIFT.value).pack(),
    )


def person_button(entry: RosterEntry) -> InlineKeyboardButton:
    """One candidate of the person step — an active member of this venue (TZ 5.8, 3.3).

    The list is `MemberService.list_assignable`, which is scoped to the actor's venue and
    drops dismissed employees, so a stranger cannot appear here even as a caption; the
    resolver checks the id again on the press, and `ShiftService.create` a third time.
    """
    return InlineKeyboardButton(
        text=entry.full_name or texts.role_label(entry.role),
        callback_data=ShiftMember(member_id=entry.member_id).pack(),
    )


def staff_button() -> InlineKeyboardButton:
    """The way out of the empty state: there is nobody to roster, so go and add somebody.

    TZ 8.1 in one button — `SCHEDULE_NO_STAFF` with no way to act on it is the dead
    end the rule is about.
    """
    return InlineKeyboardButton(
        text=texts.ADMIN_STAFF_BUTTON,
        callback_data=OpenAdmin(section=AdminSection.STAFF).pack(),
    )


def default_window_button(
    member_id: int,
    start: dt.time,
    end: dt.time,
) -> InlineKeyboardButton:
    """The venue's own default shift window, offered instead of typing it (TZ 4.2).

    The caption is the window itself, which is the venue's data and not wording — the same
    arrangement `keyboards/admin.py` uses for the IANA zone names, and for the same reason:
    inventing a phrase for it in `src/bot/texts/` would ship a value the venue is supposed
    to own (principle 1.4#6).
    """
    return InlineKeyboardButton(
        text=format_window(start, end),
        callback_data=ShiftMember(member_id=member_id).pack(),
    )


def opener_button(view: ShiftView) -> InlineKeyboardButton:
    """Assign the opener of this date, or take the mark off (TZ 4.2).

    The payload carries the state the press asks for and not the state the shift is in, so
    two managers on the same card do not undo each other.
    """
    return InlineKeyboardButton(
        text=texts.SCHEDULE_OPENER_BUTTON,
        callback_data=ShiftOpener(shift_id=view.shift_id, is_set=not view.is_opener).pack(),
    )


def closer_button(view: ShiftView) -> InlineKeyboardButton:
    """The same for the closer of the date (TZ 4.2)."""
    return InlineKeyboardButton(
        text=texts.SCHEDULE_CLOSER_BUTTON,
        callback_data=ShiftCloser(shift_id=view.shift_id, is_set=not view.is_closer).pack(),
    )


def delete_button(view: ShiftView) -> InlineKeyboardButton:
    """Remove the shift. What hung off it is cancelled by the service (TZ 6, task 33)."""
    return InlineKeyboardButton(
        text=texts.SCHEDULE_DELETE_BUTTON,
        callback_data=ShiftDelete(shift_id=view.shift_id).pack(),
    )


def back_to_schedule() -> InlineKeyboardButton:
    """Back to the fortnight, from a card or from a shift that was just saved."""
    return InlineKeyboardButton(
        text=texts.BACK_BUTTON,
        callback_data=OpenAdmin(section=AdminSection.SCHEDULE).pack(),
    )


# --------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------


def schedule_keyboard(shifts: Sequence[ShiftView]) -> InlineKeyboardMarkup:
    """The fortnight, with `SCHEDULE_ADD_BUTTON` under it.

    The empty schedule gets the same keyboard minus the shifts: TZ 8.1 wants the empty state
    to carry the button that ends it, and it is the same button either way.
    """
    rows: list[list[InlineKeyboardButton]] = [[shift_button(view)] for view in shifts]
    rows.append([add_button()])
    return block(*rows)


def person_keyboard(entries: Sequence[RosterEntry]) -> InlineKeyboardMarkup:
    """The person step: one button per active member, and cancel (TZ 8.2)."""
    return wizard(*([person_button(entry)] for entry in entries), back=False)


def no_staff_keyboard() -> InlineKeyboardMarkup:
    """The empty state of the wizard: its one button leads to the employees section."""
    return wizard([staff_button()], back=False)


def date_keyboard() -> InlineKeyboardMarkup:
    """The date step, which is typed: nothing on it but cancel (TZ 8.2)."""
    return wizard(back=False)


def window_keyboard(*, member_id: int, start: dt.time, end: dt.time) -> InlineKeyboardMarkup:
    """The window step: type two times, or take the venue's default with one tap."""
    return wizard([default_window_button(member_id, start, end)], back=False)


def shift_keyboard(view: ShiftView) -> InlineKeyboardMarkup:
    """One shift's card: the two marks of TZ 4.2, the delete, and the way back."""
    return submenu(
        [opener_button(view), closer_button(view)],
        [delete_button(view)],
        [back_to_schedule()],
        back=False,
    )


__all__ = [
    "BUTTON_SEPARATOR",
    "ScheduleCommand",
    "add_button",
    "back_to_schedule",
    "closer_button",
    "date_keyboard",
    "default_window_button",
    "delete_button",
    "no_staff_keyboard",
    "opener_button",
    "person_button",
    "person_keyboard",
    "schedule_keyboard",
    "shift_button",
    "shift_keyboard",
    "staff_button",
    "window_keyboard",
]
