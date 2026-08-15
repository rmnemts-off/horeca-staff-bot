"""Keyboards of the catalogue section, TZ 5.5 (plan task 25).

Three shapes, and every one of them is a list of ids: what a button *says* comes from
`src/bot/texts/`, what it *carries* from `src/bot/callbacks.py`. Nothing here formats venue
data beyond putting a name and a category into a caption template.

**A hit is captioned with its category** (decision D6, `TTK_HIT_BUTTON_TEMPLATE`). A bar
legitimately has two rows called *Americano* — the cocktail and the coffee — and a list of
two identical captions is a list nobody can choose from. The category is not decoration
here; it is the half of the key that tells the two apart.

**The query is not in the pager.** `RecipePage` carries an offset and nothing else: free
text in `callback_data` is refused by the scheme (rule 2 of `src/bot/callbacks.py`), so what
was searched for lives in the FSM state and the button says only "the next ten". That is
also why the pager captions read "more" and "further up" rather than "back" — the back
caption belongs to navigation (TZ 5.2), and two buttons reading the same word in one
message is a tap in the wrong place.

**Back leads to the section, not to the menu** (:func:`back_to_section`). A list of hits and
a card are second-level screens, and decision D10 spells the destination of a back button
into the payload: `Nav(BACK, section=CATALOGUE)`. `keyboards.menu.back_button()` packs
`Nav(BACK)` with no section, which means the main menu — right for a screen one step from
it (the section itself, :func:`section`), wrong for everything below. So the row is
assembled here out of that one button plus `navigation_row()`, instead of the home button
being spelled a second time.
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot import texts
from src.bot.callbacks import (
    MenuAction,
    Nav,
    NavTarget,
    RecipeMissing,
    RecipePage,
    RecipeShow,
)
from src.bot.keyboards.menu import navigation_row, submenu
from src.services.recipes import RecipeHit, SearchPage

#: One row of an inline keyboard.
Row = list[InlineKeyboardButton]


def back_to_section() -> InlineKeyboardButton:
    """The back button of a second-level screen of this section (D10; see the docstring)."""
    return InlineKeyboardButton(
        text=texts.BACK_BUTTON,
        callback_data=Nav(target=NavTarget.BACK, section=MenuAction.CATALOGUE).pack(),
    )


def hit_button(hit: RecipeHit) -> InlineKeyboardButton:
    """One search result, captioned «name · category» (decision D6)."""
    return InlineKeyboardButton(
        text=texts.TTK_HIT_BUTTON_TEMPLATE.format(name=hit.name, category=hit.category),
        callback_data=RecipeShow(recipe_id=hit.recipe_id).pack(),
    )


def report_missing_button() -> InlineKeyboardButton:
    """TZ 5.5: the employee says the recipe is not there and the manager hears about it."""
    return InlineKeyboardButton(
        text=texts.TTK_REPORT_MISSING_BUTTON,
        callback_data=RecipeMissing().pack(),
    )


def pager(page: SearchPage) -> Row:
    """Whichever ends of the list there is more of (TZ 5.5); an empty row when neither."""
    row: Row = []
    if page.previous_offset is not None:
        row.append(
            InlineKeyboardButton(
                text=texts.TTK_EARLIER_BUTTON,
                callback_data=RecipePage(offset=page.previous_offset).pack(),
            )
        )
    if page.next_offset is not None:
        row.append(
            InlineKeyboardButton(
                text=texts.TTK_MORE_BUTTON,
                callback_data=RecipePage(offset=page.next_offset).pack(),
            )
        )
    return row


def fast_search_button() -> InlineKeyboardButton:
    """The one honest form of "the list updates while I type" (part IV of the stage 1 spec).

    `switch_inline_query_current_chat` puts the bot's username into the input field of *this*
    chat, and from there every letter reaches the bot and redraws the list above the
    keyboard. A bot is told nothing about a message before it is sent, so this is not a
    shortcut to autocomplete — it is the only place autocomplete exists.

    It carries no `callback_data` and reaches no handler: the press is handled by the
    Telegram client itself. That is also why it is invisible to the assembly test that pairs
    factories with routers — there is no factory to pair.
    """
    return InlineKeyboardButton(
        text=texts.TTK_INLINE_BUTTON,
        switch_inline_query_current_chat="",
    )


def section(*, offer_search: bool = True) -> InlineKeyboardMarkup:
    """The section itself: one step from the menu, so a back button would repeat home.

    One button on it, and it is not a screen of ours: the fast search of part IV, which the
    client answers. A venue that has entered nothing does not get it — a search over an
    empty catalogue is the dead button TZ 8.1 forbids, and the screen already says the
    honest thing («no recipes yet, ask your manager»).

    Browsing by category needs a button that carries a category, and the scheme has no
    factory for one (see the module docstring of `src/bot/handlers/recipes.py`).
    """
    return submenu(*([fast_search_button()],) if offer_search else (), back=False)


def hits(page: SearchPage) -> InlineKeyboardMarkup:
    """At most ten drinks, then the pager, then the navigation (TZ 5.5)."""
    rows: list[Row] = [[hit_button(hit)] for hit in page.hits]
    rows.append(pager(page))
    return _screen(rows)


def nothing_found(suggestions: Sequence[RecipeHit] = ()) -> InlineKeyboardMarkup:
    """The suggestions of `TTK_MAYBE_TITLE`, and the one button that always leads on."""
    rows: list[Row] = [[hit_button(hit)] for hit in suggestions]
    rows.append([report_missing_button()])
    return _screen(rows)


def card() -> InlineKeyboardMarkup:
    """The card carries navigation only.

    A composition line pointing at a prep is printed and not linked: the preps are a
    section of their own (TZ 5.5) and stage 0 does not build it, so a button into it
    would lead nowhere. The volume switch of `yield_variants` is the same case.
    """
    return _screen(())


def _screen(rows: Sequence[Row]) -> InlineKeyboardMarkup:
    """The screen's own rows, then one navigation row: back to the section, then home."""
    body = [list(row) for row in rows if row]
    body.append([back_to_section(), *navigation_row(back=False, home=True)])
    return InlineKeyboardMarkup(inline_keyboard=body)


__all__ = [
    "Row",
    "back_to_section",
    "card",
    "fast_search_button",
    "hit_button",
    "hits",
    "nothing_found",
    "pager",
    "report_missing_button",
    "section",
]
