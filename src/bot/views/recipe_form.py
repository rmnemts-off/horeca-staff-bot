"""Screens of the recipe form: the section, the eight steps, what follows a save (TZ 5.5).

Decision B5, plan task 30a. Pure functions in the sense `src/bot/views/__init__.py` gives
the word — a value in, a :class:`~src.bot.views.Screen` out, nothing sent and nothing
edited — so every screen below, the empty state of TZ 8.1 included, is an ordinary assert
in `tests/bot/test_admin_recipes.py` with no bot anywhere near it.

**The card is not drawn twice.** What a manager sees after saving is
`src/bot/views/recipes.py::card` with a line of confirmation above it, and the reason is in
`RecipeService.create` in so many words: the wizard shows the manager what was saved, and it
is the same structure the bartender's card is built from, so the confirmation screen and the
card are the same screen. A second renderer here would be the copy that drifts — the one the
manager reads while the shift reads the other.

**The section says what the venue has, and it usually has nothing** (TZ 8.1). Emptiness is
read off the categories, the way `views/recipes.py::section` reads it: a venue with no
category has no card, and the answer to that is `CARD_EDITOR_EMPTY` plus the one button that
ends it, not a bare line of text.

**Both empty states of the form are screens, not branches.** The category step of a venue
that has entered nothing offers no buttons and asks for the word (`category_step`), and a
card saved with nothing but a name and a category renders as exactly that (`saved`), because
that is what TZ 5.5 says a valid card is.

Venue text is quoted: these screens are HTML like every other (`src/bot/views/__init__.py`),
and a category called «Rum & Cola» must not arrive as a broken entity.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from src.bot import texts
from src.bot.keyboards import recipe_form as keyboards
from src.bot.views import Screen, quoted
from src.bot.views import recipes as catalogue
from src.services.recipes import RecipeCard

#: Between a screen's title and its body — one blank line, as in `src/bot/views/admin.py`.
PARAGRAPH: Final = "\n\n"

#: Between the categories of the section line.
GROUP_SEPARATOR: Final = ", "


def _titled(title: str, body: str) -> str:
    return f"{title}{PARAGRAPH}{body}"


# --------------------------------------------------------------------------------------
# The section (TZ 5.8, 8.1)
# --------------------------------------------------------------------------------------


def section(categories: Sequence[str]) -> Screen:
    """The manager's half of the catalogue: what is entered, and the way to enter more.

    The categories stand in for a count of cards, which no service answers yet — see the
    report of task 30a. They are not decoration either: a category is half the key of
    decision D6, so this line is what tells a manager whether the drink they are about to
    add already has a home.
    """
    body = (
        texts.CARD_EDITOR_GROUPS_TEMPLATE.format(
            groups=GROUP_SEPARATOR.join(quoted(name) for name in categories)
        )
        if categories
        else texts.CARD_EDITOR_EMPTY
    )
    return Screen(
        text=_titled(texts.CARD_EDITOR_TITLE, body),
        markup=keyboards.section(),
    )


# --------------------------------------------------------------------------------------
# The steps (TZ 5.5, decision B5)
# --------------------------------------------------------------------------------------


def name_step() -> Screen:
    """The one field TZ 5.5 cannot draw a card without."""
    return Screen(
        text=_titled(texts.CARD_EDITOR_TITLE, texts.CARD_NAME_PROMPT),
        markup=keyboards.typed_step(),
    )


def category_step(categories: Sequence[str]) -> Screen:
    """The other required field: half the key of decision D6.

    The categories the venue already uses are buttons, and a new one is typed over them.
    Nothing here is a fixed list — the bot ships with no category at all (principle 1.4#6),
    and a venue that has entered nothing yet simply gets the question.
    """
    return Screen(
        text=_titled(texts.CARD_EDITOR_TITLE, texts.CARD_GROUP_PROMPT),
        markup=keyboards.category_step(categories),
    )


def glassware_step() -> Screen:
    return _optional_step(texts.CARD_GLASSWARE_PROMPT)


def method_step() -> Screen:
    return _optional_step(texts.CARD_METHOD_PROMPT)


def ice_step() -> Screen:
    return _optional_step(texts.CARD_ICE_PROMPT)


def composition_step() -> Screen:
    """One ingredient per line, «name — amount» (decision B5; the parsing is the service's).

    Skippable like the rest, and deliberately so: a card is valid with nothing but a name
    (TZ 5.5), and a step with no way past it would be the dead end TZ 8.1 forbids for a
    manager who is entering a position that has no composition to write down.
    """
    return _optional_step(texts.CARD_COMPOSITION_PROMPT)


def garnish_step() -> Screen:
    return _optional_step(texts.CARD_GARNISH_PROMPT)


def instruction_step() -> Screen:
    return _optional_step(texts.CARD_INSTRUCTION_PROMPT)


def _optional_step(prompt: str) -> Screen:
    """A step TZ 5.5 renders a card without: it offers the skip rather than demanding a word."""
    return Screen(
        text=_titled(texts.CARD_EDITOR_TITLE, prompt),
        markup=keyboards.optional_step(),
    )


# --------------------------------------------------------------------------------------
# After the save (TZ 5.8, decision D6)
# --------------------------------------------------------------------------------------


def saved(card: RecipeCard) -> Screen:
    """`CARD_SAVED` and the card itself, rendered by the catalogue's own view.

    TZ 5.8 has an edited recipe available to the whole shift at once, so the manager is
    shown the thing the shift is now reading rather than a receipt for it.
    """
    return Screen(
        text=_titled(texts.CARD_SAVED, catalogue.card(card).text),
        markup=keyboards.after_save(
            name=card.name,
            category=card.category,
            recipe_id=card.recipe_id,
        ),
    )


def exists(*, name: str, category: str, recipe_id: int) -> Screen:
    """Decision D6: this venue already has this name in this category.

    The refusal is a screen and not a traceback, and it carries the card that is in the way:
    the manager either wanted that one or wanted a different category, and both answers are
    a button away.
    """
    return Screen(
        text=texts.CARD_EXISTS_TEMPLATE.format(name=quoted(name), category=quoted(category)),
        markup=keyboards.after_save(name=name, category=category, recipe_id=recipe_id),
    )


__all__ = [
    "GROUP_SEPARATOR",
    "PARAGRAPH",
    "category_step",
    "composition_step",
    "exists",
    "garnish_step",
    "glassware_step",
    "ice_step",
    "instruction_step",
    "method_step",
    "name_step",
    "saved",
    "section",
]
