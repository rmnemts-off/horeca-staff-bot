"""Keyboards of the recipe form — the section, the wizard, what follows a save (TZ 5.5).

Decision B5, plan task 30a. Captions come from `src/bot/texts/admin.py`, payloads from
`src/bot/callbacks.py`, and the navigation row is never spelled here: `block()` and
`wizard_step()` of `src/bot/keyboards/admin.py` append it, so "every screen of the
management section returns to its board" stays a property of the constructor rather than a
habit of eight screens (TZ 5.2, 8.2).

**Three presses of this block carry no id, and each says so in its own way.** "Start the
form" and "skip this step" are :class:`~src.bot.callbacks.AdminCommand` with
:attr:`~src.bot.callbacks.AdminAction.RECIPE_NEW` and
:attr:`~src.bot.callbacks.AdminAction.STEP_SKIP` — they name nothing at all, and *which*
step a skip belongs to is the FSM state's answer. A category button carries
:class:`~src.bot.callbacks.RecipeCategory`, an index into the page held in the FSM state: a
category is a *string*, which rule 2 of `src/bot/callbacks.py` refuses outright, so what
travels is the place on the page and never the word.

Both factories are `minimum_role=MANAGER` in `src/bot/middlewares/resolver.py`, so every
one of these presses is vetted before `src/bot/handlers/admin_recipes.py` sees it (TZ 9).
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
    CatalogueCategory,
    CataloguePage,
    OpenAdmin,
    RecipeCategory,
    RecipeDrop,
    RecipeEdit,
    RecipeManage,
    RecipeShow,
)
from src.bot.keyboards.admin import block, wizard_step
from src.bot.keyboards.menu import submenu
from src.services.recipes import REQUIRED_FIELDS, RecipeField, RecipeHit, SearchPage

#: How many category buttons share a row. Two, like the zone buttons: a category is a word
#: the venue chose and three of them are cut on a phone (TZ 8.2).
CATEGORY_COLUMNS: Final = 2

#: How many field buttons share a row on the open card. Two, for the same reason as the
#: categories: three captions are cut on a phone (TZ 8.2).
FIELD_COLUMNS: Final = 2

#: How many categories are offered as buttons at all. A venue names a handful of them (the
#: reference book has under ten), and a keyboard past this is a wall rather than a choice.
#: Nothing becomes unreachable: the step accepts a typed category too, and a category that
#: is not on the keyboard is entered by writing it exactly as the others were.
CATEGORY_LIMIT: Final = 12


# --------------------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------------------


def add_button() -> InlineKeyboardButton:
    """`CARD_ADD_BUTTON` — the button TZ 8.1 requires on the empty section (decision B5)."""
    return InlineKeyboardButton(
        text=texts.CARD_ADD_BUTTON,
        callback_data=AdminCommand(action=AdminAction.RECIPE_NEW).pack(),
    )


def skip_button() -> InlineKeyboardButton:
    """`INVITE_SKIP_BUTTON`: an optional field is left out, not filled with a blank."""
    return InlineKeyboardButton(
        text=texts.INVITE_SKIP_BUTTON,
        callback_data=AdminCommand(action=AdminAction.STEP_SKIP).pack(),
    )


def browse_button(name: str, offered_index: int) -> InlineKeyboardButton:
    """One category of the section, on its way to the cards it holds (TZ 5.8).

    `CatalogueCategory` and not `RecipeCategory`: the two carry the same number and mean
    different things, and the class docstring of the first says what happens when one screen
    borrows the other's factory.
    """
    return InlineKeyboardButton(
        text=name,
        callback_data=CatalogueCategory(index=offered_index).pack(),
    )


def browse_rows(categories: Sequence[str]) -> list[list[InlineKeyboardButton]]:
    """The offered categories of the section, two to a row."""
    offered = list(categories[:CATEGORY_LIMIT])
    return [
        [
            browse_button(name, start + shift)
            for shift, name in enumerate(offered[start : start + CATEGORY_COLUMNS])
        ]
        for start in range(0, len(offered), CATEGORY_COLUMNS)
    ]


def category_button(name: str, offered_index: int) -> InlineKeyboardButton:
    """One category the venue already uses.

    The caption is the category itself, and it is venue data rather than wording — the same
    arrangement `src/bot/keyboards/admin.py::timezone_rows` has for an IANA name. Nothing in
    `src/bot/texts/` names a category and nothing may: a list of them shipped in the code
    would be exactly the delivered data principle 1.4#6 refuses.

    `offered_index` is the place on the page the step drew, which is what the payload
    carries; the page itself lives in the FSM state (`src/bot/handlers/admin_recipes.py`).
    """
    return InlineKeyboardButton(
        text=name,
        callback_data=RecipeCategory(index=offered_index).pack(),
    )


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


def manage_button(hit: RecipeHit) -> InlineKeyboardButton:
    """One found card, on its way to the screen that edits it (TZ 5.8).

    `RecipeManage` and not `RecipeShow`: the same row, a different screen. What opens here
    carries the edit and delete buttons, so the press is checked against the manager's role
    and against the card being the venue's own (`src/bot/middlewares/resolver.py`).
    """
    return InlineKeyboardButton(
        text=texts.TTK_HIT_BUTTON_TEMPLATE.format(name=hit.name, category=hit.category),
        callback_data=RecipeManage(recipe_id=hit.recipe_id).pack(),
    )


def edit_button(field: RecipeField, recipe_id: int) -> InlineKeyboardButton:
    """One field of the open card, ready to be retyped."""
    return InlineKeyboardButton(
        text=texts.field_label(field),
        callback_data=RecipeEdit(recipe_id=recipe_id, field=field).pack(),
    )


def clear_button(recipe_id: int, field: RecipeField) -> InlineKeyboardButton:
    """Take an optional field off the card, without typing anything.

    Written as a button because the alternative does not exist: a screen cannot ask for an
    empty message, since Telegram refuses to send one. Offered only where clearing is
    allowed — `REQUIRED_FIELDS` is the service's own set, so this cannot promise what
    `RecipeService.update` would refuse.
    """
    return InlineKeyboardButton(
        text=texts.CARD_CLEAR_BUTTON,
        callback_data=RecipeEdit(recipe_id=recipe_id, field=field, is_clear=True).pack(),
    )


def delete_button(recipe_id: int) -> InlineKeyboardButton:
    """Opens the question, never the deletion: `is_confirmed=False` is the ask."""
    return InlineKeyboardButton(
        text=texts.CARD_DELETE_BUTTON,
        callback_data=RecipeDrop(recipe_id=recipe_id, is_confirmed=False).pack(),
    )


def confirm_delete_button(recipe_id: int) -> InlineKeyboardButton:
    """The answer to it. Both presses name the same row, so the question cannot drift."""
    return InlineKeyboardButton(
        text=texts.CARD_DELETE_YES_BUTTON,
        callback_data=RecipeDrop(recipe_id=recipe_id, is_confirmed=True).pack(),
    )


def open_management_button(recipe_id: int) -> InlineKeyboardButton:
    """Back to the card being worked on, from the question and from a step of an edit."""
    return InlineKeyboardButton(
        text=texts.BACK_BUTTON,
        callback_data=RecipeManage(recipe_id=recipe_id).pack(),
    )


def listing_pager(page: SearchPage) -> list[InlineKeyboardButton]:
    """Whichever ends of the listing there is more of; an empty row when neither.

    `CataloguePage` and not `RecipePage`: that one belongs to the bartender's search, whose
    router is included first and would answer this press with the employee's screen.
    """
    row: list[InlineKeyboardButton] = []
    if page.previous_offset is not None:
        row.append(
            InlineKeyboardButton(
                text=texts.TTK_EARLIER_BUTTON,
                callback_data=CataloguePage(offset=page.previous_offset).pack(),
            )
        )
    if page.next_offset is not None:
        row.append(
            InlineKeyboardButton(
                text=texts.TTK_MORE_BUTTON,
                callback_data=CataloguePage(offset=page.next_offset).pack(),
            )
        )
    return row


# --------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------


def section(categories: Sequence[str] = ()) -> InlineKeyboardMarkup:
    """The section itself: add a card, or open one of the categories that exist.

    A venue that has entered nothing gets the add button alone, which is the honest empty
    state of TZ 8.1 — there is no category to offer until a first card names one, and the
    screen says so in words. Finding a card by name needs no button: the section listens for
    a typed line.
    """
    return block([add_button()], *browse_rows(categories))


def listing(page: SearchPage) -> InlineKeyboardMarkup:
    """What a search or a category came back with: the cards, then the pager (TZ 5.5).

    Library hits are not drawn. The search reads the venue's rows *and* the shared BarPoint
    library (TZ 3.3), while every screen behind these buttons ends in a write, which a
    library row does not allow until question C4 is answered — the resolver would refuse the
    press, and a button whose only possible answer is a refusal is the dead button TZ 8.1
    forbids.
    Dropping it here and not in the service is deliberate: the same page feeds the
    bartender's list, where a library card is a perfectly good result.
    """
    rows = [[manage_button(hit)] for hit in page.hits if not hit.is_library]
    return block(*rows, listing_pager(page))


def card_actions(recipe_id: int) -> InlineKeyboardMarkup:
    """The open card: one button per field, then the deletion (TZ 5.8).

    Two to a row for the reason `CATEGORY_COLUMNS` gives — three captions are cut on a
    phone — and the delete button alone on its own row, away from the thumb that was
    aiming at the field above it.
    """
    fields = list(RecipeField)
    rows = [
        [edit_button(field, recipe_id) for field in fields[start : start + FIELD_COLUMNS]]
        for start in range(0, len(fields), FIELD_COLUMNS)
    ]
    return block(*rows, [delete_button(recipe_id)])


def delete_question(recipe_id: int) -> InlineKeyboardMarkup:
    """Yes, and the way out of the question — which is the card it was asked about."""
    return submenu(
        [confirm_delete_button(recipe_id)],
        [open_management_button(recipe_id)],
        back=False,
    )


def edit_step(recipe_id: int, field: RecipeField) -> InlineKeyboardMarkup:
    """A field waiting for a line of text, plus the way to empty it (TZ 8.2)."""
    rows = [] if field in REQUIRED_FIELDS else [[clear_button(recipe_id, field)]]
    return wizard_step(*rows, [open_management_button(recipe_id)])


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
    "CATEGORY_COLUMNS",
    "CATEGORY_LIMIT",
    "FIELD_COLUMNS",
    "add_button",
    "after_save",
    "back_to_catalogue",
    "browse_button",
    "browse_rows",
    "card_actions",
    "category_button",
    "category_rows",
    "category_step",
    "clear_button",
    "confirm_delete_button",
    "delete_button",
    "delete_question",
    "edit_button",
    "edit_step",
    "listing",
    "listing_pager",
    "manage_button",
    "open_card_button",
    "open_management_button",
    "optional_step",
    "section",
    "skip_button",
    "typed_step",
]
