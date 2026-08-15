"""The manager's report on one day, as a file in the chat (TZ 5.9, 5.8).

Three presses at most: the section, then one of the two shortcuts or a typed date, then the workbook
arrives. The date is the venue's own (TZ 3.4), never the server's, and "today" is
resolved here for the same reason every other screen does it — the services take a moment
and never read a clock (decision D12).

Two things about this module are decisions.

**Building the workbook happens off the event loop.** openpyxl is synchronous and
CPU-bound, and the bot is a single process: a report assembled inline freezes every
employee of the venue while it runs. `asyncio.to_thread` is why the report model is a
snapshot of scalars — a lazy ORM attribute touched from that thread would reach for a
session that belongs to another one.

**The file is built in memory and never written to disk.** `BufferedInputFile` takes bytes,
and the guard on seeded data forbids a workbook inside the delivery in the first place.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Final, Protocol

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from src.bot import texts
from src.bot.callbacks import AdminSection, OpenAdmin, ReportDay
from src.bot.safe_edit import safe_edit
from src.bot.states import ReportWizard
from src.bot.views import Screen
from src.bot.views.reports import report_date_screen, report_ready_screen
from src.db.models import Venue
from src.exporters import shift_report_filename, shift_report_workbook
from src.services.access import AccessContext, require_manager
from src.services.reports import ShiftReport
from src.services.timezones import utc_now, venue_timezone

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "admin_reports"


class Reports(Protocol):
    """The one service these screens use (TZ 5.9)."""

    async def shift_report(self, day: dt.date) -> ShiftReport: ...


class ReportServices(Protocol):
    """`data["services"]`, narrowed to what this section reads."""

    @property
    def reports(self) -> Reports: ...


def today_in(venue: Venue) -> dt.date:
    """The venue's own date, which is not the server's (TZ 3.4).

    A report asked for at half past midnight in Moscow is about the day that has just
    started there, whatever the server thinks the date is.
    """
    return utc_now().astimezone(venue_timezone(venue.timezone)).date()


async def open_reports(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    venue: Venue,
) -> None:
    """The section: two shortcuts and an invitation to type a date (TZ 5.9)."""
    require_manager(actor)
    await state.set_state(ReportWizard.day)
    await _render(bot, callback, report_date_screen(today_in(venue)))


async def report_for_day(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    venue: Venue,
    services: ReportServices,
    callback_payload: ReportDay,
) -> None:
    """The two shortcuts — today and yesterday, the days a manager asks about (TZ 5.9)."""
    require_manager(actor)
    day = today_in(venue) - dt.timedelta(days=callback_payload.days_back)
    origin = callback.message if isinstance(callback.message, Message) else None
    await _deliver(bot, origin, state=state, services=services, day=day, answer=callback)


async def report_for_typed_day(
    message: Message,
    bot: Bot,
    state: FSMContext,
    actor: AccessContext,
    venue: Venue,
    services: ReportServices,
) -> None:
    """A date typed in any of the shapes `admin_schedule` already accepts (TZ 5.9)."""
    require_manager(actor)
    day = _date_in(message.text or "", today=today_in(venue))
    if day is None:
        await message.answer(texts.REPORT_DATE_PROMPT)
        return
    await _deliver(bot, message, state=state, services=services, day=day)


async def _deliver(
    bot: Bot,
    origin: Message | None,
    *,
    state: FSMContext,
    services: ReportServices,
    day: dt.date,
    answer: CallbackQuery | None = None,
) -> None:
    """Build the workbook and send it into the chat that asked (TZ 5.9)."""
    if origin is None:
        if answer is not None:
            await answer.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
        return
    if answer is not None:
        await answer.answer(texts.REPORT_BUILDING)

    report = await services.reports.shift_report(day)
    await state.clear()
    screen = report_ready_screen(report)
    if report.is_empty:
        # TZ 8.1: a day nobody worked is an answer, and an empty workbook is not one.
        await bot.send_message(origin.chat.id, screen.text, reply_markup=screen.markup)
        return

    # openpyxl is synchronous and CPU-bound; on the event loop it would stop the venue.
    workbook = await asyncio.to_thread(shift_report_workbook, report)
    await bot.send_document(
        origin.chat.id,
        BufferedInputFile(workbook, filename=shift_report_filename(report)),
        caption=screen.text,
        reply_markup=screen.markup,
    )


def _date_in(text: str, *, today: dt.date) -> dt.date | None:
    """Day-first, with or without a year — the shapes `admin_schedule` already accepts.

    A year that was not typed is this venue's current one, which is right for a report:
    nobody asks for last August by typing two numbers.
    """
    cleaned = text.strip().replace("/", ".").replace("-", ".")
    parts = [part for part in cleaned.split(".") if part]
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        return None
    day, month = int(parts[0]), int(parts[1])
    year = int(parts[2]) if len(parts) == 3 else today.year
    if year < 100:
        year += 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


async def _render(bot: Bot, callback: CallbackQuery, screen: Screen) -> None:
    """Rewrite the message the button was pressed on, and close the spinner (TZ 8.2)."""
    origin = callback.message
    if origin is None:
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


def router() -> Router:
    """The reports block of the management section (TZ 5.8, 5.9)."""
    instance = Router(name=ROUTER_NAME)
    instance.callback_query.register(
        open_reports,
        OpenAdmin.filter(F.section == AdminSection.REPORTS),
    )
    instance.callback_query.register(report_for_day, ReportDay.filter())
    instance.message.register(report_for_typed_day, StateFilter(ReportWizard.day))
    return instance


__all__ = ["ROUTER_NAME", "open_reports", "report_for_day", "report_for_typed_day", "router"]
