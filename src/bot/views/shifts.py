"""The shift screen and the schedule, as pure functions (TZ 5.3, plan task 23).

Both screens are drawn from what :class:`~src.services.shifts.ShiftService` returned and
from nothing else — no clock, no timezone, no repository. That is not tidiness for its own
sake: the shift screen is also the screen the scheduled opening checklist links back to
(TZ 5.4, TZ 6), and a worker process has no update to build it from.

**Neither function converts a time, and that is deliberate** (TZ 3.4, decision D12).
``shift_date``, ``start_time`` and ``end_time`` are already the venue's own wall clock —
that is what those ``DATE``/``TIME`` columns mean — so printing them is printing, not
arithmetic. The one question that *does* need instants is "is this shift on right now",
and it is answered by :meth:`ShiftView.covers`, whose two ends were built by
``src/services/timezones.py`` when the service made the view. ``now`` therefore arrives as
an argument: a view that reads a clock is a view that cannot be snapshot-tested, and the
14:00-00:00 shift seen at 00:30 is exactly the case that has to be.

**Empty is the first case of each function, not the last** (TZ 8.1). A new venue has no
schedule at all, so ``SHIFT_NONE`` is an ordinary output with the ordinary keyboard under
it, and there is no branch anywhere in a handler that decides between two screens.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from src.bot import texts
from src.bot.keyboards.shifts import schedule_keyboard, shift_keyboard
from src.bot.views import Screen, quoted
from src.services.shifts import ShiftView

# --------------------------------------------------------------------------------------
# Formats
# --------------------------------------------------------------------------------------


def format_date(value: dt.date) -> str:
    """A venue-local date as «14.08».

    Day and month only: both screens look at most two weeks ahead (TZ 5.3), so the year
    is a column of noise on every line. Written out rather than through ``strftime`` —
    ``%d.%m`` is the same string in every locale here, but the day the format needs a
    weekday it would not be, and a locale-dependent screen is a screen that reads
    differently on the server than on a laptop.
    """
    return f"{value.day:02d}.{value.month:02d}"


def format_time(value: dt.time) -> str:
    """A venue-local wall clock reading as «14:00».

    Zero-padded, which is a decision about the schedule screen: fourteen lines whose hours
    line up are read at a glance, and the end of a night shift is «00:00» rather than the
    «0:00» that reads like a typo.
    """
    return f"{value.hour:02d}:{value.minute:02d}"


def format_window(start: dt.time, end: dt.time) -> str:
    """A shift window as 14:00-00:00, through the one dash of the product."""
    return texts.TIME_RANGE_TEMPLATE.format(start=format_time(start), end=format_time(end))


def window_of(shift: ShiftView) -> str:
    return format_window(shift.start_time, shift.end_time)


# --------------------------------------------------------------------------------------
# The shift screen (TZ 5.3)
# --------------------------------------------------------------------------------------


def shift_screen(
    shift: ShiftView | None,
    *,
    now: dt.datetime,
    roster: Sequence[ShiftView] = (),
    checklist_run_id: int | None = None,
) -> Screen:
    """The shift being worked, else the nearest one, else the empty state (TZ 5.3).

    Which of the two it is has already been decided by
    :meth:`~src.services.shifts.ShiftService.nearest_shift`; this only asks the view it was
    handed whether it covers ``now``, so that the heading is ``SHIFT_NOW_TITLE`` for the
    person standing behind the bar at half past midnight and ``SHIFT_NEXT_TITLE`` for the
    one who is not. Both readings come off the same object, so the heading and the dates
    below it cannot disagree.

    ``roster`` is the whole date (:meth:`~src.services.shifts.ShiftService.roster`) and the
    person themselves is dropped from it here: TZ 5.3 asks who is *with* them, and the
    filter is by person rather than by row so that somebody rostered twice on one date is
    not printed as their own colleague.

    ``checklist_run_id`` draws the one button of this screen (TZ 5.4: the checklist can be
    reopened from this screen at any time). ``None`` means there is no run to open — no
    checklist has gone out for this shift yet — and then the button is not drawn at all,
    rather than drawn and leading nowhere (TZ 8.1).
    """
    if shift is None:
        return Screen(text=texts.SHIFT_NONE, markup=shift_keyboard())

    lines = [
        texts.SHIFT_NOW_TITLE if shift.covers(now) else texts.SHIFT_NEXT_TITLE,
        texts.SHIFT_WHEN_TEMPLATE.format(
            date=format_date(shift.shift_date),
            window=window_of(shift),
        ),
    ]
    if shift.is_opener:
        lines.append(texts.SHIFT_YOU_OPEN)
    if shift.is_closer:
        lines.append(texts.SHIFT_YOU_CLOSE)

    lines.append("")
    lines.extend(_team_lines(shift, roster))
    return Screen(
        text="\n".join(lines),
        markup=shift_keyboard(checklist_run_id=checklist_run_id),
    )


def _team_lines(shift: ShiftView, roster: Sequence[ShiftView]) -> list[str]:
    """The team block: `SHIFT_TEAM_TITLE` and a line each, or `SHIFT_TEAM_ALONE` (TZ 5.3, 8.1).

    A venue where one bartender works alone is the ordinary case rather than a degenerate
    one, so the empty roster gets a sentence of its own instead of a heading with nothing
    under it.
    """
    others = [entry for entry in roster if entry.user_id != shift.user_id]
    if not others:
        return [texts.SHIFT_TEAM_ALONE]
    return [
        texts.SHIFT_TEAM_TITLE,
        *(
            texts.SHIFT_TEAM_LINE_TEMPLATE.format(
                full_name=quoted(entry.full_name),
                window=window_of(entry),
            )
            for entry in others
        ),
    ]


# --------------------------------------------------------------------------------------
# The schedule (TZ 5.3)
# --------------------------------------------------------------------------------------


def schedule_screen(shifts: Sequence[ShiftView]) -> Screen:
    """One line per shift for the next two weeks (TZ 5.3).

    The horizon is the service's (``SCHEDULE_HORIZON_DAYS``) and the order is the service's
    too, so this function neither slices nor sorts: a screen that re-decided either would
    be a second, quieter copy of the rule.
    """
    if not shifts:
        return Screen(text=texts.SCHEDULE_EMPTY, markup=schedule_keyboard())
    return Screen(
        text="\n".join([texts.SCHEDULE_TITLE, "", *(_schedule_line(shift) for shift in shifts)]),
        markup=schedule_keyboard(),
    )


def _schedule_line(shift: ShiftView) -> str:
    """One line of the fortnight: the date, the window, and the marks the date carries.

    Both marks can land on one line: TZ 4.2 allows the same person to open and to close a
    date, and a line that showed only the first of them would hide half of what they are
    responsible for.
    """
    mark = ""
    if shift.is_opener:
        mark += texts.SCHEDULE_OPENER_MARK
    if shift.is_closer:
        mark += texts.SCHEDULE_CLOSER_MARK
    return texts.SCHEDULE_LINE_TEMPLATE.format(
        date=format_date(shift.shift_date),
        window=window_of(shift),
        mark=mark,
    )


__all__ = [
    "format_date",
    "format_time",
    "format_window",
    "schedule_screen",
    "shift_screen",
    "window_of",
]
