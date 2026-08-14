"""The screens of the management section: the board, the create-venue wizard, the settings.

Pure functions, for the reason the package docstring gives: nothing here sends, edits or
answers anything, so the same screen can be drawn by a handler and — once the customer asks
for a nightly "your venue is set up like this" digest — by the worker, without a second copy
of it existing anywhere.

**The empty state of this block is :func:`no_venue_yet`** (TZ 8.1). It is not an edge case:
decision A3 has the very first owner arrive before any venue exists, and every other screen
of this section is unreachable until the wizard behind that one button has run. It is a view
and not a branch inside a handler, so the wording and the button are asserted in
`tests/bot/test_admin_venue.py` without a bot.

The other screens of this block do not have an empty state of their own, and that is a
statement rather than an omission: the board's five buttons all lead to blocks that exist
(each with its own empty state), and a venue always has a `venue_settings` row because
:meth:`~src.services.venues.VenueService.create` writes it in the same transaction as the
venue (task 26). A venue without one is a broken row, not an empty screen.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Final

from src.bot import texts
from src.bot.keyboards import admin as keyboards
from src.bot.views import Screen, quoted
from src.services.venues import VenueConfiguration

#: A wall clock reading as the venue writes it — `08:00`, zero-padded, which is the shape
#: `VENUE_WINDOW_PROMPT` asks for and the shape the parser of the wizard accepts back.
CLOCK_FORMAT: Final = "%H:%M"

#: Between a screen's title and its body. One blank line, everywhere in this block.
PARAGRAPH: Final = "\n\n"

#: Between the lines of a list.
LINE: Final = "\n"


def clock(value: dt.time) -> str:
    """A `TIME` column as the wall clock reading it is (decision D12: never a moment)."""
    return value.strftime(CLOCK_FORMAT)


def window(start: dt.time, end: dt.time) -> str:
    """A shift window, in the one format `texts.TIME_RANGE_TEMPLATE` fixes for the project."""
    return texts.TIME_RANGE_TEMPLATE.format(start=clock(start), end=clock(end))


def _titled(title: str, body: str) -> str:
    return f"{title}{PARAGRAPH}{body}"


# --------------------------------------------------------------------------------------
# The board (TZ 5.8)
# --------------------------------------------------------------------------------------


def board() -> Screen:
    """The root of the section: the blocks of TZ 5.8 that stage 0 built."""
    return Screen(text=texts.ADMIN_TITLE, markup=keyboards.board())


# --------------------------------------------------------------------------------------
# The wizard (plan task 26, decisions A3 and B1)
# --------------------------------------------------------------------------------------


def no_venue_yet() -> Screen:
    """TZ 8.1 for the whole product: the bootstrap owner, before anything exists.

    Drawn by the onboarding module (TZ 5.1 owns `/start`), which is why it lives here as a
    view rather than inside the wizard's own handler: the screen belongs to this block, the
    moment it is shown belongs to that one.
    """
    return Screen(text=texts.ONBOARDING_CREATE_VENUE_OFFER, markup=keyboards.offer())


def name_step() -> Screen:
    return Screen(
        text=_titled(texts.VENUE_WIZARD_TITLE, texts.VENUE_NAME_PROMPT),
        markup=keyboards.wizard_step(),
    )


def city_step() -> Screen:
    return Screen(
        text=_titled(texts.VENUE_WIZARD_TITLE, texts.VENUE_CITY_PROMPT),
        markup=keyboards.wizard_step(),
    )


def timezone_step(zones: Sequence[str]) -> Screen:
    """TZ 3.4 through principle 1.4#5: the zone is picked, never typed."""
    return Screen(
        text=_titled(texts.VENUE_WIZARD_TITLE, texts.VENUE_TIMEZONE_PROMPT),
        markup=keyboards.wizard_timezones(zones),
    )


def window_step() -> Screen:
    return Screen(
        text=_titled(texts.VENUE_WIZARD_TITLE, texts.VENUE_WINDOW_PROMPT),
        markup=keyboards.wizard_step(),
    )


def created(venue_name: str) -> Screen:
    """The end of the wizard. No inline keyboard: what follows is the main menu itself.

    Decision D3 in one screen — the person is an `owner` in `venue_members` from this moment,
    so the handler answers with `main_menu(OWNER)` and the reply keyboard of TZ 5.2 replaces
    the wizard entirely.
    """
    return Screen(text=texts.VENUE_CREATED_TEMPLATE.format(venue=quoted(venue_name)))


# --------------------------------------------------------------------------------------
# The settings (plan task 30)
# --------------------------------------------------------------------------------------


def settings(configuration: VenueConfiguration) -> Screen:
    """The three settings of TZ 5.8, each with the button that changes it.

    Both halves of :class:`~src.services.venues.VenueConfiguration` are read, because the
    screen edits both tables: the zone is a column of `venues` and the other two lines are
    columns of `venue_settings`.
    """
    lines = (
        texts.SETTINGS_TITLE,
        texts.SETTINGS_LINE_TEMPLATE.format(
            name=texts.SETTINGS_TIMEZONE_BUTTON,
            value=configuration.venue.timezone,
        ),
        texts.SETTINGS_LINE_TEMPLATE.format(
            name=texts.SETTINGS_CHECKLIST_LEAD_BUTTON,
            value=texts.SETTINGS_MINUTES_TEMPLATE.format(
                value=configuration.settings.opening_checklist_lead_minutes
            ),
        ),
        texts.SETTINGS_LINE_TEMPLATE.format(
            name=texts.SETTINGS_SHIFT_WINDOW_BUTTON,
            value=window(
                configuration.settings.default_shift_start,
                configuration.settings.default_shift_end,
            ),
        ),
    )
    return Screen(text=LINE.join(lines), markup=keyboards.settings())


def settings_timezone(zones: Sequence[str]) -> Screen:
    """Changing the zone of a venue that already exists (TZ 3.4, TZ 5.8)."""
    return Screen(
        text=_titled(texts.SETTINGS_TITLE, texts.VENUE_TIMEZONE_PROMPT),
        markup=keyboards.settings_timezones(zones),
    )


def settings_lead_minutes() -> Screen:
    """`SETTINGS_CHECKLIST_LEAD_PROMPT`: the number that moves the push.

    The move itself is the notification service's (TZ 6, plan task 37): this screen asks,
    `VenueService.update_settings` writes, and the queue is rebuilt from the stored value.
    """
    return Screen(
        text=_titled(texts.SETTINGS_TITLE, texts.SETTINGS_CHECKLIST_LEAD_PROMPT),
        markup=keyboards.settings_step(),
    )


def settings_window() -> Screen:
    return Screen(
        text=_titled(texts.SETTINGS_TITLE, texts.VENUE_WINDOW_PROMPT),
        markup=keyboards.settings_step(),
    )


__all__ = [
    "CLOCK_FORMAT",
    "LINE",
    "PARAGRAPH",
    "board",
    "city_step",
    "clock",
    "created",
    "name_step",
    "no_venue_yet",
    "settings",
    "settings_lead_minutes",
    "settings_timezone",
    "settings_window",
    "timezone_step",
    "window",
    "window_step",
]
