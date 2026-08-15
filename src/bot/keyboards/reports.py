"""Keyboards of the reports block (TZ 5.9, 5.2).

Two shortcuts and the navigation row `submenu()` adds by itself. Today and yesterday are
the two days a manager actually asks about; anything else is typed, because a calendar of
thirty buttons is not a screen a phone reads (TZ 8.2).

The buttons carry a *number of days back*, not a date. A date in the payload would be spent
budget on something the screen already knows, and the "today" button pressed at ten past midnight
has to mean the day that has just started — which a button drawn yesterday cannot say.
"""

from __future__ import annotations

from typing import Final

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot import texts
from src.bot.callbacks import AdminSection, OpenAdmin, ReportDay
from src.bot.keyboards.menu import submenu

#: How far back each shortcut looks, in days.
TODAY: Final = 0
YESTERDAY: Final = 1


def day_button(caption: str, days_back: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=caption,
        callback_data=ReportDay(days_back=days_back).pack(),
    )


def back_to_reports() -> InlineKeyboardButton:
    """Back to the block, so a second report needs no trip through the board."""
    return InlineKeyboardButton(
        text=texts.BACK_BUTTON,
        callback_data=OpenAdmin(section=AdminSection.REPORTS).pack(),
    )


def report_day_keyboard() -> InlineKeyboardMarkup:
    """The two shortcuts; any other day is typed into the chat."""
    return submenu(
        [
            day_button(texts.REPORT_TODAY_BUTTON, TODAY),
            day_button(texts.REPORT_YESTERDAY_BUTTON, YESTERDAY),
        ],
        back=True,
    )


def report_ready_keyboard() -> InlineKeyboardMarkup:
    """The caption of the delivered file: one way back, one way home (TZ 5.2)."""
    return submenu([back_to_reports()], back=False)


__all__ = [
    "TODAY",
    "YESTERDAY",
    "back_to_reports",
    "day_button",
    "report_day_keyboard",
    "report_ready_keyboard",
]
