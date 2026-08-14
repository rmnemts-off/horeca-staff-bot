"""Keyboards of the recipe form — the section, the wizard, what follows a save (TZ 5.5).

Decision B5, plan task 30a. Captions come from `src/bot/texts/admin.py`, payloads from
`src/bot/callbacks.py`, and the navigation row is never spelled here: `block()` and
`wizard_step()` of `src/bot/keyboards/admin.py` append it, so "every screen of the
management section returns to its board" stays a property of the constructor rather than a
habit of eight screens (TZ 5.2, 8.2).

**Three presses of this block ride on :class:`~src.bot.callbacks.TimezoneChoice`, and that
is a stopgap.** The scheme declares no factory for "start the form", none for "skip this
step" and none for "this category" — and the third is the one that hurts: a category is a
*string*, which rule 2 of `src/bot/callbacks.py` refuses outright, so a category button can
only ever carry an index into a page held in the FSM state. That is exactly the shape
`TimezoneChoice` has, and it is the shape `src/bot/handlers/recipes.py` already names in so
many words as the reason browsing by category is not built.

Declaring the right factories means editing `src/bot/callbacks.py` and the resolver's
`RULES` — two files other waves are writing in this hour, and a factory without a rule fails
the build (`tests/bot/test_middlewares.py::test_every_callback_factory_has_a_rule`). So this
block does what `src/bot/keyboards/admin.py::VenueCommand` did before it: it takes a slice
of the one index space that is already carved into "a zone" and "not a zone", at values that
can never name either.

The slices are disjoint by construction and by test
(`tests/bot/test_admin_recipes.py::test_the_recipe_indices_cannot_be_mistaken_for_a_venue_command`):

* `0 …` — a zone of the page `src/bot/handlers/admin_venue.py` is showing;
* :class:`~src.bot.keyboards.admin.VenueCommand` — the four presses of the venue block;
* :data:`COMMAND_BASE` and below to :data:`CATEGORY_BASE` — :class:`RecipeCommand`;
* :data:`CATEGORY_BASE` and below — one index per category offered, counted downwards.

Two consequences a reader has to know about. `TimezoneChoice` is declared `needs_actor=False`
in the resolver — it has to be, since the venue wizard runs before any membership exists — so
**nothing checks the presser's rights before these buttons reach a handler**, and
`src/bot/handlers/admin_recipes.py` checks for itself on every one of them (TZ 9). And the
day `callbacks.py` gains a `FormCommand(...)` and a `PickOption(index=…)`, this module and
the registrations of that handler change factory and nothing else moves.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from typing import Final

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot import texts
from src.bot.callbacks import AdminSection, OpenAdmin, RecipeShow, TimezoneChoice
from src.bot.keyboards.admin import block, wizard_step
from src.bot.keyboards.menu import submenu

#: How many category buttons share a row. Two, like the zone buttons: a category is a word
#: the venue chose and three of them are cut on a phone (TZ 8.2).
CATEGORY_COLUMNS: Final = 2

#: How many categories are offered as buttons at all. A venue names a handful of them (the
#: reference book has under ten), and a keyboard past this is a wall rather than a choice.
#: Nothing becomes unreachable: the step accepts a typed category too, and a category that
#: is not on the keyboard is entered by writing it exactly as the others were.
CATEGORY_LIMIT: Final = 12

#: First index of this block's slice (see the module docstring). Below every value of
#: `VenueCommand`, with room left there for a command the venue block has not needed yet.
COMMAND_BASE: Final = -101

#: First index of the category slice; the i-th category offered is `CATEGORY_BASE - i`.
CATEGORY_BASE: Final = -201


class RecipeCommand(enum.IntEnum):
    """A press of this block that names no category.

    Both are the shape a factory of their own would have: they carry nothing at all, and
    *which step* a skip belongs to is decided by the FSM state, which is what an FSM is for.
    See the module docstring for why they travel on `TimezoneChoice`.
    """

    #: `CARD_ADD_BUTTON` — open the form (decision B5).
    ADD = COMMAND_BASE
    #: `INVITE_SKIP_BUTTON` on an optional step: TZ 5.5 draws a card without any of them.
    SKIP = COMMAND_BASE - 1


def command(action: RecipeCommand) -> str:
    """The payload of one of the two buttons above."""
    return TimezoneChoice(index=action.value).pack()


def category_command(offered_index: int) -> str:
    """The payload of the button offering the `offered_index`-th category of the page."""
    return TimezoneChoice(index=CATEGORY_BASE - offered_index).pack()


def category_index(index: int) -> int | None:
    """Which offered category an index names, or `None` when it names none.

    The inverse of :func:`category_command`, and the only place the arithmetic is written:
    a handler that recomputed it would be a second opinion about what a button meant.
    """
    if index > CATEGORY_BASE:
        return None
    return CATEGORY_BASE - index


# --------------------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------------------


def add_button() -> InlineKeyboardButton:
    """`CARD_ADD_BUTTON` — the button TZ 8.1 requires on the empty section (decision B5)."""
    return InlineKeyboardButton(
        text=texts.CARD_ADD_BUTTON,
        callback_data=command(RecipeCommand.ADD),
    )


def skip_button() -> InlineKeyboardButton:
    """`INVITE_SKIP_BUTTON`: an optional field is left out, not filled with a blank."""
    return InlineKeyboardButton(
        text=texts.INVITE_SKIP_BUTTON,
        callback_data=command(RecipeCommand.SKIP),
    )


def category_button(name: str, offered_index: int) -> InlineKeyboardButton:
    """One category the venue already uses.

    The caption is the category itself, and it is venue data rather than wording — the same
    arrangement `src/bot/keyboards/admin.py::timezone_rows` has for an IANA name. Nothing in
    `src/bot/texts/` names a category and nothing may: a list of them shipped in the code
    would be exactly the delivered data principle 1.4#6 refuses.
    """
    return InlineKeyboardButton(text=name, callback_data=category_command(offered_index))


def open_card_button(*, name: str, category: str, recipe_id: int) -> InlineKeyboardButton:
    """The saved card, captioned the way every recipe button in the bot is (decision D6).

    `RecipeShow` belongs to `src/bot/handlers/recipes.py`: the card the bartender opens and
    the card the manager has just entered are one screen, drawn by one module.
    """
    return InlineKeyboardButton(
        text=texts.TTK_HIT_BUTTON_TEMPLATE.format(name=name, category=category),
        callback_data=RecipeShow(recipe_id=recipe_id).pack(),
    )


def back_to_catalogue() -> InlineKeyboardButton:
    """Back to this section, from a screen that follows a save."""
    return InlineKeyboardButton(
        text=texts.BACK_BUTTON,
        callback_data=OpenAdmin(section=AdminSection.CATALOGUE).pack(),
    )


# --------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------


def section() -> InlineKeyboardMarkup:
    """The section itself, full or empty: the same one button either way (TZ 8.1)."""
    return block([add_button()])


def typed_step() -> InlineKeyboardMarkup:
    """A required step, which is answered by writing: cancel and nothing else (TZ 8.2)."""
    return wizard_step()


def category_step(categories: Sequence[str]) -> InlineKeyboardMarkup:
    """The categories the venue has, as buttons (principle 1.4#5: a choice is pressed).

    A venue that has entered nothing yet gets the cancel button alone, and that is the
    honest empty state of this step rather than a gap: there is no category to offer until
    a first card names one, and the step is answered by typing it.
    """
    return wizard_step(*category_rows(categories))


def category_rows(categories: Sequence[str]) -> list[list[InlineKeyboardButton]]:
    """The offered categories, two to a row, in the order they were handed in."""
    offered = list(categories[:CATEGORY_LIMIT])
    return [
        [
            category_button(name, start + shift)
            for shift, name in enumerate(offered[start : start + CATEGORY_COLUMNS])
        ]
        for start in range(0, len(offered), CATEGORY_COLUMNS)
    ]


def optional_step() -> InlineKeyboardMarkup:
    """An optional step: the skip TZ 5.5 makes necessary, plus cancel (TZ 8.2)."""
    return wizard_step([skip_button()])


def after_save(*, name: str, category: str, recipe_id: int) -> InlineKeyboardMarkup:
    """What a manager does next: open the card, or enter the following one.

    The same keyboard serves the card that was just written and the card that was already
    there (decision D6), because the next two things to do are the same in both cases.

    Back leads to this section and not to the board of TZ 5.8: the form was opened from the
    section, and `block()` would send a manager who has just entered eight cards one screen
    further back than they came from. Built with `submenu(back=False)` for the reason
    `src/bot/keyboards/staff.py` states — one caption, one destination.
    """
    return submenu(
        [open_card_button(name=name, category=category, recipe_id=recipe_id)],
        [add_button()],
        [back_to_catalogue()],
        back=False,
    )


__all__ = [
    "CATEGORY_BASE",
    "CATEGORY_COLUMNS",
    "CATEGORY_LIMIT",
    "COMMAND_BASE",
    "RecipeCommand",
    "add_button",
    "after_save",
    "back_to_catalogue",
    "category_button",
    "category_command",
    "category_index",
    "category_rows",
    "category_step",
    "command",
    "open_card_button",
    "optional_step",
    "section",
    "skip_button",
    "typed_step",
]
