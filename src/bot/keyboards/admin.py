"""Keyboards of the management section (TZ 5.8; plan tasks 26 and 30).

Three shapes of screen live in this section and each gets a constructor here, so that a
screen module never spells a navigation payload of its own:

* :func:`board` — the root of the section, one button per block of TZ 5.8. It sits one step
  from the main menu, so it carries `texts.HOME_BUTTON` and no back button (the rule
  `src/bot/keyboards/menu.py` states for `submenu(back=False)`);
* :func:`block` — any screen *inside* the section. Its back button returns to the board and
  not to the main menu, which is decision D10 in the shape `Nav(BACK, section=MANAGEMENT)`;
* :func:`wizard_step` — one step of the create-venue wizard. `back` is off there for a
  reason that is particular to this wizard: the person running it is the bootstrap owner of
  decision A3, who belongs to no venue yet and therefore has no main menu to go back to.
  `texts.CANCEL_BUTTON` is the whole of TZ 8.2's requirement for them.

**Why the back button is spelled here and not taken from `keyboards/menu.py`.** `submenu()`
draws `Nav(BACK)` with no section, which means the main menu — right for a first-level
screen and wrong for every screen of this block, which has the board behind it. The section
is a field of the payload and there is no constructor for it yet, so :func:`back_to_board`
builds that one button and takes the home button from `navigation_row()` rather than
repeating it. See the report of plan task 26: `submenu()` gaining a `section` argument would
delete this function.

**The zone buttons carry an index, never a name** (`src/bot/callbacks.py`: the widest IANA
name is over half the callback budget). The order of :func:`~src.services.venues.wizard_timezones`
is what the index means, which is why that enum is documented as append-only.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot import texts
from src.bot.callbacks import (
    AdminAction,
    AdminCommand,
    AdminSection,
    MenuAction,
    Nav,
    NavTarget,
    OpenAdmin,
    TimezoneChoice,
    VenueCreate,
)
from src.bot.keyboards.menu import navigation_row, submenu, wizard

#: How many zone buttons share a row. Two, because an IANA name is long and three of them
#: are cut on a phone (TZ 8.2).
TIMEZONE_COLUMNS: Final = 2

#: The blocks of TZ 5.8 that stage 0 builds, in the order the section lists them. Captions
#: are references into `src/bot/texts/`; this holds no wording of its own.
BLOCKS: Final[tuple[tuple[AdminSection, str], ...]] = (
    (AdminSection.STAFF, texts.ADMIN_STAFF_BUTTON),
    (AdminSection.SCHEDULE, texts.ADMIN_SCHEDULE_BUTTON),
    (AdminSection.CHECKLISTS, texts.ADMIN_CHECKLISTS_BUTTON),
    (AdminSection.CATALOGUE, texts.ADMIN_CATALOGUE_BUTTON),
    (AdminSection.SETTINGS, texts.ADMIN_SETTINGS_BUTTON),
)


def back_to_board() -> InlineKeyboardButton:
    """The back button of this section: it returns to the board, not to the menu (D10)."""
    return InlineKeyboardButton(
        text=texts.BACK_BUTTON,
        callback_data=Nav(target=NavTarget.BACK, section=MenuAction.MANAGEMENT).pack(),
    )


def board() -> InlineKeyboardMarkup:
    """The five blocks of TZ 5.8, one button each, and the way home.

    Every button leads to a block that exists: the four `admin_*` modules of this wave plus
    the settings screen of task 30. TZ 8.1 forbids a caption with nothing behind it, so a
    block that stage 0 does not build is absent from :data:`BLOCKS` rather than greyed out.
    """
    return submenu(*([button] for button in _block_buttons()), back=False)


def _block_buttons() -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=caption, callback_data=OpenAdmin(section=section).pack())
        for section, caption in BLOCKS
    ]


def block(*rows: Sequence[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    """A screen inside the section: its own rows, then back-to-the-board and home."""
    body = [list(row) for row in rows if row]
    body.append([back_to_board(), *navigation_row(back=False, home=True)])
    return InlineKeyboardMarkup(inline_keyboard=body)


def wizard_step(*rows: Sequence[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    """One step of the create-venue wizard: the rows and cancel (see the module docstring)."""
    return wizard(*rows, back=False)


def timezone_rows(zones: Sequence[str]) -> list[list[InlineKeyboardButton]]:
    """The offered zones as buttons, in the order they were handed in.

    The caption is the IANA name itself. It is not wording — nothing in `src/bot/texts/`
    names a zone, and inventing a Russian label per zone would put a list of city names into
    the delivery, which is the collection principle 1.4#6 refuses.
    """
    return [
        [
            InlineKeyboardButton(
                text=zone,
                callback_data=TimezoneChoice(index=start + shift).pack(),
            )
            for shift, zone in enumerate(zones[start : start + TIMEZONE_COLUMNS])
        ]
        for start in range(0, len(zones), TIMEZONE_COLUMNS)
    ]


def wizard_timezones(zones: Sequence[str]) -> InlineKeyboardMarkup:
    """The zone step of the wizard (TZ 3.4, principle 1.4#5: a choice is buttons)."""
    return wizard_step(*timezone_rows(zones))


def settings() -> InlineKeyboardMarkup:
    """The settings screen: one edit button per line of TZ 5.8's settings block.

    Each button names its field through :class:`~src.bot.callbacks.AdminAction`, so the
    resolver checks the manager's role before any of the three handlers runs.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=texts.SETTINGS_TIMEZONE_BUTTON,
                callback_data=AdminCommand(action=AdminAction.VENUE_TIMEZONE).pack(),
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.SETTINGS_CHECKLIST_LEAD_BUTTON,
                callback_data=AdminCommand(action=AdminAction.VENUE_LEAD).pack(),
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.SETTINGS_SHIFT_WINDOW_BUTTON,
                callback_data=AdminCommand(action=AdminAction.VENUE_WINDOW).pack(),
            )
        ],
    ]
    return block(*rows)


def settings_timezones(zones: Sequence[str]) -> InlineKeyboardMarkup:
    """The same zone list, opened from the settings screen instead of from the wizard."""
    return block(*timezone_rows(zones))


def settings_step() -> InlineKeyboardMarkup:
    """A settings field being typed: the cancel button and nothing else (TZ 8.2)."""
    return wizard_step()


def offer() -> InlineKeyboardMarkup:
    """The one button of the empty state: there is no venue yet, so make one (TZ 8.1).

    No navigation row at all, and that is the point: whoever sees this screen has no
    membership anywhere (decision A3), so home would lead to a menu they cannot be shown
    and back to a screen that does not exist.

    :class:`~src.bot.callbacks.VenueCreate` is the same payload
    `src/bot/keyboards/onboarding.py` draws, so the two entrances into the wizard are one
    button with one handler rather than two spellings of it.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.ONBOARDING_CREATE_VENUE_BUTTON,
                    callback_data=VenueCreate().pack(),
                )
            ]
        ]
    )


__all__ = [
    "BLOCKS",
    "TIMEZONE_COLUMNS",
    "back_to_board",
    "block",
    "board",
    "offer",
    "settings",
    "settings_step",
    "settings_timezones",
    "timezone_rows",
    "wizard_step",
    "wizard_timezones",
]
