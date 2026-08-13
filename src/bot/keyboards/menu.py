"""The main menu as a registry, and the navigation every other keyboard must carry.

**The menu is a registry, not a function with `if`s** (decision B7). Each entry declares the
section, its caption, the roles that may see it and one flag: whether stage 0 implements it.
At stage 0 `staff` gets three buttons — the shift, the schedule and the recipe book — and
the write-off, the order and the knowledge base are **not rendered at all**. Not greyed out,
not answered with a "coming soon": TZ 8.1 forbids a button that leads nowhere, and stage
0 is measuring whether this feels like a working tool, which a row of dead buttons decides
on its own. `manager` and `owner` additionally get the management section (TZ 5.2, 5.8).

Turning a section on at stage 1 is one flag in the table below. The keyboard code does not
change — that is principle 1.4#4 applied to our own delivery, and the reason the entries are
data rather than branches.

**The menu is a reply keyboard** (TZ 5.2), permanent and resizable, so it survives forty
messages of a busy shift instead of scrolling away like an inline keyboard would. It is
therefore matched by *caption*: :func:`action_for` is the lookup the intercepting router of
plan task 20 uses to recognise a menu press before the FSM sees it.

**Navigation is not left to each screen.** TZ 5.2 requires a back and a home button in
every submenu and TZ 8.2 a cancel in every step-by-step scenario, so :func:`submenu` and
:func:`wizard` add them; a screen builds its own rows and hands them over. Nothing here
formats venue data — captions come from `src/bot/texts/`, payloads from
`src/bot/callbacks.py`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from src.bot import texts
from src.bot.callbacks import MenuAction, Nav, NavTarget
from src.db.models import MemberRole

#: Everyone who works in the venue. Spelled from the enum so a role added later is not
#: silently excluded from the three sections every employee has.
EVERYONE: Final[frozenset[MemberRole]] = frozenset(MemberRole)
#: TZ 5.2: the management section is added for `manager` and `owner`; TZ 2 gives an
#: `owner` every right a `manager` has.
MANAGEMENT_ROLES: Final[frozenset[MemberRole]] = frozenset({MemberRole.MANAGER, MemberRole.OWNER})

#: How many buttons share a row of the reply keyboard (TZ 5.2 draws it two by two).
MENU_COLUMNS: Final = 2


@dataclass(frozen=True, slots=True)
class MenuEntry:
    """One line of the registry: a section, how it is written, who sees it, is it built.

    `is_available` is the whole of decision B7. An entry with the flag off is declared,
    documented and not rendered — which is different from not existing, because stage 1
    turns it on here instead of editing a keyboard builder.
    """

    action: MenuAction
    caption: str
    roles: frozenset[MemberRole]
    is_available: bool

    def is_visible_to(self, role: MemberRole) -> bool:
        return self.is_available and role in self.roles


#: TZ 5.2 in order, including what stage 0 does not build yet.
MAIN_MENU: Final[tuple[MenuEntry, ...]] = (
    MenuEntry(
        action=MenuAction.MY_SHIFT,
        caption=texts.MENU_SHIFT_BUTTON,
        roles=EVERYONE,
        is_available=True,
    ),
    MenuEntry(
        action=MenuAction.SCHEDULE,
        caption=texts.MENU_SCHEDULE_BUTTON,
        roles=EVERYONE,
        is_available=True,
    ),
    MenuEntry(
        action=MenuAction.CATALOGUE,
        caption=texts.MENU_CATALOGUE_BUTTON,
        roles=EVERYONE,
        is_available=True,
    ),
    # Stage 1 (plan, "does not enter stage 0"): TZ 5.5-5.7 describe these sections, but
    # nothing behind these three captions is built yet, so they are not drawn.
    MenuEntry(
        action=MenuAction.KNOWLEDGE,
        caption=texts.MENU_KNOWLEDGE_BUTTON,
        roles=EVERYONE,
        is_available=False,
    ),
    MenuEntry(
        action=MenuAction.WRITEOFF,
        caption=texts.MENU_WRITEOFF_BUTTON,
        roles=EVERYONE,
        is_available=False,
    ),
    MenuEntry(
        action=MenuAction.ORDER,
        caption=texts.MENU_ORDER_BUTTON,
        roles=EVERYONE,
        is_available=False,
    ),
    MenuEntry(
        action=MenuAction.MANAGEMENT,
        caption=texts.MENU_MANAGEMENT_BUTTON,
        roles=MANAGEMENT_ROLES,
        is_available=True,
    ),
)

#: Caption -> section, for the rendered entries only. A caption that stage 0 never draws is
#: deliberately unknown here: recognising it would mean answering a button that does not
#: exist, and TZ 8.1 would rather it stayed silent.
_ACTION_BY_CAPTION: Final[Mapping[str, MenuAction]] = {
    entry.caption: entry.action for entry in MAIN_MENU if entry.is_available
}


def entries_for(role: MemberRole) -> tuple[MenuEntry, ...]:
    """The registry lines this role actually sees, in the order of TZ 5.2."""
    return tuple(entry for entry in MAIN_MENU if entry.is_visible_to(role))


def action_for(caption: str) -> MenuAction | None:
    """The section a main-menu press means, or None when the text is not a menu press."""
    return _ACTION_BY_CAPTION.get(caption)


def main_menu(role: MemberRole) -> ReplyKeyboardMarkup:
    """The permanent keyboard at the bottom of the chat (TZ 5.2)."""
    captions = [entry.caption for entry in entries_for(role)]
    keyboard = [
        [KeyboardButton(text=caption) for caption in captions[start : start + MENU_COLUMNS]]
        for start in range(0, len(captions), MENU_COLUMNS)
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        # TZ 5.2: it must not get lost in the conversation, so it stays after a press and
        # after a restart of the client.
        is_persistent=True,
        one_time_keyboard=False,
    )


# --------------------------------------------------------------------------------------
# Navigation (TZ 5.2, 8.2)
# --------------------------------------------------------------------------------------


def back_button() -> InlineKeyboardButton:
    """The back button, required in every submenu (TZ 5.2)."""
    return InlineKeyboardButton(
        text=texts.BACK_BUTTON,
        callback_data=Nav(target=NavTarget.BACK).pack(),
    )


def home_button() -> InlineKeyboardButton:
    """The home button, required in every submenu (TZ 5.2)."""
    return InlineKeyboardButton(
        text=texts.HOME_BUTTON,
        callback_data=Nav(target=NavTarget.HOME).pack(),
    )


def cancel_button() -> InlineKeyboardButton:
    """Cancel: required in every step-by-step scenario, returns without saving (TZ 8.2)."""
    return InlineKeyboardButton(
        text=texts.CANCEL_BUTTON,
        callback_data=Nav(target=NavTarget.CANCEL).pack(),
    )


def navigation_row(
    *,
    back: bool = True,
    home: bool = True,
    cancel: bool = False,
) -> list[InlineKeyboardButton]:
    """The navigation row itself, for a screen that needs an unusual combination."""
    row: list[InlineKeyboardButton] = []
    if back:
        row.append(back_button())
    if home:
        row.append(home_button())
    if cancel:
        row.append(cancel_button())
    return row


def _markup(
    rows: Iterable[Sequence[InlineKeyboardButton]],
    navigation: Sequence[InlineKeyboardButton],
) -> InlineKeyboardMarkup:
    body = [list(row) for row in rows if row]
    if navigation:
        body.append(list(navigation))
    return InlineKeyboardMarkup(inline_keyboard=body)


def submenu(
    *rows: Sequence[InlineKeyboardButton],
    back: bool = True,
) -> InlineKeyboardMarkup:
    """A submenu: the screen's own rows plus the back and home buttons (TZ 5.2).

    `back` is off only on a screen that is itself one step from the menu, where two
    buttons would lead to the same place.
    """
    return _markup(rows, navigation_row(back=back, home=True))


def wizard(
    *rows: Sequence[InlineKeyboardButton],
    back: bool = True,
) -> InlineKeyboardMarkup:
    """One step of a step-by-step scenario: the rows plus back and cancel (TZ 8.2).

    Home is not here on purpose: leaving a half-filled wizard is what cancel is for, and
    pressing a main-menu button does it too (TZ 5.2 — the reply keyboard is always there,
    and plan task 20 makes it interrupt the FSM).
    """
    return _markup(rows, navigation_row(back=back, home=False, cancel=True))


__all__ = [
    "EVERYONE",
    "MAIN_MENU",
    "MANAGEMENT_ROLES",
    "MENU_COLUMNS",
    "MenuEntry",
    "action_for",
    "back_button",
    "cancel_button",
    "entries_for",
    "home_button",
    "main_menu",
    "navigation_row",
    "submenu",
    "wizard",
]
