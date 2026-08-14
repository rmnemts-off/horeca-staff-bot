"""Keyboards of the shift screen and of the schedule (TZ 5.3, plan task 23).

Both screens sit one step from the main menu — the reply keyboard opens them directly
(TZ 5.2) - so both are built with ``submenu(back=False)``: a back button and a home one
next to each other would be two captions for one destination, and decision D10 says a
first-level screen has one way up.

The navigation itself is never spelled here. It comes from ``keyboards/menu.py``, which is
the module that guarantees TZ 5.2's "every submenu has a back and a home"; a screen that
assembled its own row would be the screen where one of the two is forgotten.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot import texts
from src.bot.callbacks import ChecklistShow
from src.bot.keyboards.menu import submenu


def checklist_button(run_id: int) -> InlineKeyboardButton:
    """Reopen the checklist of the shift on the screen (TZ 5.4).

    TZ 5.4 asks for this in so many words: the message with the checklist gets swiped away
    or buried under forty others, and the employee must be able to get back to it without
    waiting for the bot. It carries the *run*, not the template — the ticks belong to the
    run, and a button naming the template would open a fresh copy of a checklist that is
    already half done.
    """
    return InlineKeyboardButton(
        text=texts.SHIFT_OPEN_CHECKLIST_BUTTON,
        callback_data=ChecklistShow(run_id=run_id).pack(),
    )


def shift_keyboard(*, checklist_run_id: int | None = None) -> InlineKeyboardMarkup:
    """The shift screen: the checklist button when there is a run, navigation always.

    ``None`` covers both the empty state (no shifts at all) and a shift whose opening
    checklist has not been sent yet, and in both of them the button is absent rather than
    inert: TZ 8.1 has no room for a button that leads nowhere.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if checklist_run_id is not None:
        rows.append([checklist_button(checklist_run_id)])
    return submenu(*rows, back=False)


def schedule_keyboard() -> InlineKeyboardMarkup:
    """The schedule: a list with nothing to press on it but the way home.

    Deliberately empty of its own buttons. TZ 5.3 gives the employee a read-only fortnight
    — editing a shift is the manager's screen (TZ 5.8), reached from the management section
    — so a row of per-shift buttons here would be a right the role does not have.
    """
    return submenu(back=False)


__all__ = ["checklist_button", "schedule_keyboard", "shift_keyboard"]
