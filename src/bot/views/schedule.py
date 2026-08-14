"""The manager's schedule as pure functions (TZ 5.3, 5.8; plan task 29).

Same contract as every other module of this package: a view object of
:mod:`src.services.shifts` goes in, a :class:`~src.bot.views.Screen` comes out, and nothing
is sent, edited or answered. That is what lets the empty state of TZ 8.1 be asserted as a
value rather than as a bot scenario, and it is why none of these functions reads a clock:
which fortnight is being shown was decided by the caller, so the same screen can be drawn
from an update and — the day the customer asks for a nightly digest — by the worker.

**Nothing here converts a time** (TZ 3.4, decision D12). ``shift_date``, ``start_time`` and
``end_time`` are already the venue's own wall clock, which is what those ``DATE``/``TIME``
columns mean, so printing them is printing. The formats themselves come from
`src/bot/views/shifts.py`, because the employee's schedule and the manager's are the same
two weeks seen twice, and two spellings of one window on neighbouring screens is how a
product starts reading as two.

**Decision B4 is a line of text on this screen and nothing else.** A shift whose date has
nobody opening it is *saved*, and `SCHEDULE_NO_OPENER_WARNING` is printed under it; the
warnings arrive in `ShiftSaved.warnings` / `ShiftRemoved.warnings` and are not re-derived
here, so the screen and the service cannot disagree about whether a date is complete. TZ
section 6 is a closed list of eight notification types and this is not one of them — nobody
is messaged, the manager who caused it reads it where they caused it.

`ShiftWarning.NO_CLOSER` has no wording in `src/bot/texts/` and is therefore not drawn. That
is the text layer's decision to make, not this module's: a warning invented here would be a
Russian string in `src/`, which the whole project is checked for.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from itertools import groupby
from typing import Final

from src.bot import texts
from src.bot.keyboards.schedule import (
    date_keyboard,
    no_staff_keyboard,
    person_keyboard,
    schedule_keyboard,
    shift_keyboard,
    window_keyboard,
)
from src.bot.views import Screen, quoted
from src.bot.views.shifts import format_date, window_of
from src.services.members import RosterEntry
from src.services.shifts import ShiftRole, ShiftView, ShiftWarning

#: Between a title and a body, and between the days of the fortnight. One blank line.
PARAGRAPH: Final = "\n\n"

#: Between the lines inside one day.
LINE: Final = "\n"

#: The sentence a warning of :class:`~src.services.shifts.ShiftWarning` is written as.
#: `NO_CLOSER` is absent on purpose — see the module docstring.
WARNING_TEXTS: Final[dict[ShiftWarning, str]] = {
    ShiftWarning.NO_OPENER: texts.SCHEDULE_NO_OPENER_WARNING,
}

#: «this role of this date is already somebody else's» (TZ 4.2, one of each per date).
TAKEN_TEXTS: Final[dict[ShiftRole, str]] = {
    ShiftRole.OPENER: texts.SCHEDULE_OPENER_TAKEN_TEMPLATE,
    ShiftRole.CLOSER: texts.SCHEDULE_CLOSER_TAKEN_TEMPLATE,
}


# --------------------------------------------------------------------------------------
# Pieces
# --------------------------------------------------------------------------------------


def marks(view: ShiftView) -> str:
    """The two marks a shift can carry, appended to a line (TZ 4.2).

    Both can land on one line: the same person may open and close a date, and a line showing
    only the first of them would hide half of what they are responsible for.
    """
    mark = ""
    if view.is_opener:
        mark += texts.SCHEDULE_OPENER_MARK
    if view.is_closer:
        mark += texts.SCHEDULE_CLOSER_MARK
    return mark


def person_line(view: ShiftView) -> str:
    """One shift inside a day: who works it and when.

    The name is a person's own text and goes through :func:`~src.bot.views.quoted`, for the
    reason the package docstring gives — the parse mode is HTML for the whole process, so an
    employee with a `<` in their name would otherwise make Telegram refuse the message and
    the manager would see no schedule at all.
    """
    return texts.SHIFT_TEAM_LINE_TEMPLATE.format(
        full_name=quoted(view.full_name),
        window=window_of(view),
    ) + marks(view)


def warning_lines(warnings: Iterable[ShiftWarning]) -> list[str]:
    """The warnings that have wording, in the order the service reported them (B4)."""
    return [WARNING_TEXTS[warning] for warning in warnings if warning in WARNING_TEXTS]


def _days(shifts: Sequence[ShiftView]) -> list[str]:
    """The fortnight as one block per day, in the order it was handed in (TZ 5.3).

    Grouped and never sorted: the order is the service's — `roster()` returns a date in the
    order the day runs, and the caller walks the dates forward — and a screen that re-sorted
    would be a second, quieter copy of that rule. A day with no shifts is simply not printed:
    fourteen empty headings are the noise a manager scrolls past to find the one line that
    matters.
    """
    return [
        LINE.join([format_date(day), *(person_line(view) for view in group)])
        for day, group in groupby(shifts, key=_date_of)
    ]


def _date_of(view: ShiftView) -> dt.date:
    return view.shift_date


# --------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------


def schedule_screen(
    shifts: Sequence[ShiftView],
    *,
    warnings: Sequence[ShiftWarning] = (),
) -> Screen:
    """The venue's next two weeks, by day, or the empty state that starts one (TZ 5.8, 8.1).

    `warnings` is what a delete left behind: removing the person who opened a date leaves
    that date without an opener, and decision B4 says so on the screen the manager is looking
    at rather than in a notification.
    """
    body = _days(shifts) if shifts else [texts.SCHEDULE_ADMIN_EMPTY]
    return Screen(
        text=PARAGRAPH.join([texts.SCHEDULE_ADMIN_TITLE, *body, *warning_lines(warnings)]),
        markup=schedule_keyboard(shifts),
    )


def person_step(entries: Sequence[RosterEntry]) -> Screen:
    """Step 1 of the wizard: who works it — and the empty state of TZ 8.1 when nobody can.

    A venue that has issued no invite codes yet has exactly one member, the owner, and a
    list of one is still a list; a venue whose only employees were dismissed has none. Both
    end up here, and neither gets a step with no buttons on it: the screen says why and
    carries the button that fixes it, which is the employees section.
    """
    if not entries:
        return Screen(text=texts.SCHEDULE_NO_STAFF, markup=no_staff_keyboard())
    return Screen(text=texts.SCHEDULE_PICK_PERSON, markup=person_keyboard(entries))


def date_step() -> Screen:
    """Step 2: the date. Typed, because the scheme has no factory to carry one."""
    return Screen(text=texts.SCHEDULE_PICK_DATE, markup=date_keyboard())


def window_step(*, member_id: int, start: dt.time, end: dt.time) -> Screen:
    """Step 3: the window, with the venue's own default one tap away (TZ 4.2, 5.8).

    `start` and `end` are `venue_settings.default_shift_*` and never a constant of this
    module: "the shift starts at eight" is a fact about one bar, and a product that ships it
    is a product that needs a code change per venue (principle 1.4#4).
    """
    return Screen(
        text=texts.SCHEDULE_PICK_WINDOW,
        markup=window_keyboard(member_id=member_id, start=start, end=end),
    )


def shift_screen(
    view: ShiftView,
    *,
    warnings: Sequence[ShiftWarning] = (),
    taken: ShiftRole | None = None,
    taken_by: str = "",
) -> Screen:
    """One shift's card: who, when, and what can be done about it (TZ 5.3, 5.8).

    Three things can be printed under the shift and they are three different facts:

    * `warnings` — decision B4. The shift **is saved**; the date it lands on has nobody
      opening it and the opening checklist will therefore not be sent (TZ 5.4);
    * `taken` / `taken_by` — the refusal of TZ 4.2. A second opener for a date is not saved,
      and the manager is told whose it already is instead of being shown a traceback. The
      name comes from the service that raised it, never from a repository;
    * nothing at all, which is the ordinary card.
    """
    lines = [
        texts.SCHEDULE_LINE_TEMPLATE.format(
            date=format_date(view.shift_date),
            window=window_of(view),
            mark=marks(view),
        ),
        texts.BULLET_LINE_TEMPLATE.format(text=quoted(view.full_name)),
    ]
    if taken is not None:
        lines.append(TAKEN_TEXTS[taken].format(full_name=quoted(taken_by)))
    lines.extend(warning_lines(warnings))
    return Screen(text=LINE.join(lines), markup=shift_keyboard(view))


__all__ = [
    "LINE",
    "PARAGRAPH",
    "TAKEN_TEXTS",
    "WARNING_TEXTS",
    "date_step",
    "marks",
    "person_line",
    "person_step",
    "schedule_screen",
    "shift_screen",
    "warning_lines",
    "window_step",
]
