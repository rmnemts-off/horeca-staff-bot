"""Screens of the reports block (TZ 5.9, 8.1).

Two of them: the one that asks which day, and the one that comes back with the file. Pure
functions, like every other view — a snapshot of the service goes in, a
:class:`~src.bot.views.Screen` comes out, and nothing is sent.

The second screen is a *caption* on the document rather than a message of its own: the
manager gets one thing in the chat, not a file and a sentence about it.
"""

from __future__ import annotations

import datetime as dt

from src.bot import texts
from src.bot.keyboards.reports import report_day_keyboard, report_ready_keyboard
from src.bot.views import Screen
from src.bot.views.shifts import format_date
from src.services.reports import ShiftReport


def report_date_screen(today: dt.date) -> Screen:
    """Which day (TZ 5.9). The two shortcuts carry the venue's own date, not the server's."""
    return Screen(
        text="\n".join([texts.REPORT_TITLE, "", texts.REPORT_DATE_PROMPT]),
        markup=report_day_keyboard(),
    )


def report_ready_screen(report: ShiftReport) -> Screen:
    """What the workbook holds, in one line — or that there was nothing to hold (TZ 8.1).

    The numbers are the two a manager acts on: how many people worked, and how much was
    left unticked. Everything else is in the file.
    """
    day = format_date(report.day)
    if report.is_empty:
        return Screen(
            text=texts.REPORT_EMPTY_TEMPLATE.format(date=day),
            markup=report_ready_keyboard(),
        )
    return Screen(
        text=texts.REPORT_READY_TEMPLATE.format(
            date=day,
            shifts=len(report.shifts),
            pending=len(report.pending),
        ),
        markup=report_ready_keyboard(),
    )


__all__ = ["report_date_screen", "report_ready_screen"]
