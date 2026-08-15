"""What the dropdown above the keyboard shows (part IV of the stage 1 spec; TZ 5.5).

A view like every other in this package — a service view in, something renderable out,
nothing sent — except that what comes out is not a :class:`~src.bot.views.Screen`. Telegram
does not give an inline answer a text and a keyboard; it gives it a *list of results*, each
of which is a row in the dropdown plus the message that row sends. So this module returns
`InlineQueryResultArticle` objects and lives apart from `views/recipes.py`, which returns
screens.

**The message a row sends is the card, drawn by the card's own view.** That is the point of
the whole feature: the bartender picks a name out of the dropdown and the recipe lands in
the chat, identical to the one the section shows. A second renderer here would be the copy
that drifts — and the one that drifts is always the one somebody reads at six in the morning.

**Which also means it is HTML** (`src/bot/dispatcher.py::build_bot`), so a drink called
«Rum & Cola» is quoted before it reaches the message body, exactly as everywhere else. The
*row* is not: a title and a description are plain text to Telegram, and quoting them would
show the bartender `&amp;` in the list.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from aiogram.types import (
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
)

from src.bot import texts
from src.bot.views import recipes as catalogue
from src.services.recipes import RecipeCard

#: How many ingredients the second line of a row names before it gives up. A dropdown row is
#: one short line on a phone; three names and a mark say "this is the one" without wrapping.
HINT_INGREDIENTS: Final = 3

#: `start_parameter` of the button that leads out of a foreign chat and into the bot's own.
#: Telegram requires one (`A-Z a-z 0-9 _ -`), and it arrives as `/start ttk`, which the
#: onboarding handler reads as a plain `/start` — no code, so the member simply gets the menu.
OPEN_BOT_PARAMETER: Final = "ttk"


def results(cards: Sequence[RecipeCard]) -> list[InlineQueryResultArticle]:
    """One row per card, each carrying the whole card as the message it sends."""
    return [_article(card) for card in cards]


def elsewhere_button() -> InlineQueryResultsButton:
    """Shown above an empty answer when the search was typed outside the bot's chat.

    An empty dropdown with no explanation reads as "there is no such drink". This says
    what actually happened, and pressing it opens the chat where the search works
    (open question 12 of the stage 1 spec).
    """
    return InlineQueryResultsButton(
        text=texts.TTK_INLINE_ELSEWHERE_BUTTON,
        start_parameter=OPEN_BOT_PARAMETER,
    )


def _article(card: RecipeCard) -> InlineQueryResultArticle:
    """One row of the dropdown.

    `id` is the recipe's own id: Telegram requires the results of one answer to be uniquely
    identified, and this identifies them by the thing they are.
    """
    return InlineQueryResultArticle(
        id=str(card.recipe_id),
        title=card.name,
        description=_hint(card),
        # No `parse_mode` here on purpose: it defaults to the bot's own (HTML), which is
        # what the card is written in. Spelling it a second time would be a second place to
        # forget it (`src/bot/dispatcher.py`).
        input_message_content=InputTextMessageContent(message_text=catalogue.card(card).text),
    )


def _hint(card: RecipeCard) -> str:
    """The second line of a row: the category, and the composition if there is one.

    The category alone is not decoration — decision D6 lets two drinks share a name, and
    without it the list would offer two identical rows.
    """
    if not card.has_ingredients:
        return card.category
    return texts.TTK_INLINE_HINT_TEMPLATE.format(
        category=card.category,
        composition=_composition(card),
    )


def _composition(card: RecipeCard) -> str:
    names = [line.name for line in card.ingredients[:HINT_INGREDIENTS]]
    listed = texts.TTK_INLINE_HINT_SEPARATOR.join(names)
    if len(card.ingredients) > HINT_INGREDIENTS:
        return listed + texts.TTK_INLINE_HINT_MORE_MARK
    return listed


__all__ = ["HINT_INGREDIENTS", "OPEN_BOT_PARAMETER", "elsewhere_button", "results"]
