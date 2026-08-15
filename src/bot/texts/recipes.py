"""«📖 ТТК и рецепты»: search, the list of hits, the card (TZ 5.5).

Constants here are prefixed `TTK_` — технико-технологическая карта, the word the trade
uses and the one TZ 5.2 puts on the button.

**The card is assembled from parts on purpose.** TZ 5.5 shows one line reading «Бокал:
хайбол · Метод: билд · Лёд: краш», but plan task 18 requires that an empty `glassware`,
`method`, `ice`, `garnish` or `instruction` is not rendered at all — a card with «Метод: —»
is worse than a card without the word. So each part is its own template and the renderer
joins whatever is present with `TTK_CARD_SEPARATOR`. A minimal card — a name and the
composition — is a valid card (test 41).

The unit labels are the only vocabulary in this file. `Unit` is fixed by code (TZ 4.4), so
its Russian short forms are interface wording; what is poured, in what quantity and into
what glass is the venue's own data and never appears here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from src.db.models import Unit

# --------------------------------------------------------------------------------------
# Search (TZ 5.5)
# --------------------------------------------------------------------------------------

TTK_TITLE: Final = "ТТК и рецепты"
#: The screen offers one way in and says so. It used to read «or choose a category», and
#: no screen of stage 0 draws a category button: the prompt was describing a feature that
#: does not exist, which for the bartender reads as a bot that lost a button. Browsing by
#: category needs a factory of its own — `RecipeCategory` belongs to the manager's form and
#: carries `minimum_role=MANAGER` — so it is stage 1, and the wording waits for it.
TTK_SEARCH_PROMPT: Final = "Напишите название — найду по первым буквам."
TTK_SEARCH_BUTTON: Final = "🔍 Найти"

#: TZ 8.1 verbatim, the screen a new venue sees first.
TTK_EMPTY: Final = "Рецептуры пока не заведены. Обратитесь к управляющему."

#: TZ 5.5: at most ten hits with pagination; the category is part of the caption so that
#: two drinks of the same name stay distinguishable (test 40).
TTK_HIT_BUTTON_TEMPLATE: Final = "{name} · {category}"
TTK_FOUND_TEMPLATE: Final = "Нашёл {count}. Выберите:"
TTK_PAGE_TEMPLATE: Final = "Страница {page}"
#: Pagination, deliberately not «Назад»: that caption belongs to navigation (TZ 5.2) and
#: two buttons with one word in the same message is a tap in the wrong place.
TTK_MORE_BUTTON: Final = "Ещё ▶️"
TTK_EARLIER_BUTTON: Final = "◀️ Выше"

#: TZ 5.5: «Не нашёл. Возможно, вы имели в виду: ...». Written so the sentence does not
#: start with a word made only of letters that look Latin.
TTK_NOTHING_FOUND_TEMPLATE: Final = "Ничего не нашёл по запросу «{query}»."
TTK_MAYBE_TITLE: Final = "Возможно, вы имели в виду:"
#: TZ 5.5: the button that tells the manager a recipe is missing.
TTK_REPORT_MISSING_BUTTON: Final = "Рецепта нет"
TTK_REPORTED: Final = "Передал управляющему."

# --------------------------------------------------------------------------------------
# The card (TZ 5.5)
# --------------------------------------------------------------------------------------

TTK_CARD_NAME_TEMPLATE: Final = "<b>{name}</b>"
TTK_CARD_GLASSWARE_TEMPLATE: Final = "Бокал: {value}"
TTK_CARD_METHOD_TEMPLATE: Final = "Метод: {value}"
TTK_CARD_ICE_TEMPLATE: Final = "Лёд: {value}"
#: Joins whichever of the three above are filled in.
TTK_CARD_SEPARATOR: Final = " · "
TTK_CARD_COMPOSITION_TITLE: Final = "Состав:"
#: A composition line with a quantity: «• Ром белый — 50 мл».
TTK_CARD_LINE_TEMPLATE: Final = "• {name} — {amount}"
#: TZ 5.5 and section 7: «Содовая — топ» has no number, so the line is the text as entered.
TTK_CARD_LINE_PLAIN_TEMPLATE: Final = "• {name}"
TTK_CARD_AMOUNT_TEMPLATE: Final = "{qty} {unit}"
TTK_CARD_INSTRUCTION_TEMPLATE: Final = "Приготовление: {value}"
TTK_CARD_GARNISH_TEMPLATE: Final = "Гарниш: {value}"
#: TZ 5.5: a composition line that points at a prep opens its card.
TTK_CARD_PREP_BUTTON_TEMPLATE: Final = "🧪 {name}"
#: TZ 5.5: «варианты выхода» — the switch between volumes.
TTK_CARD_YIELD_BUTTON_TEMPLATE: Final = "{value}"
#: TZ 5.5: seasonal positions come first in a list and say so on the card.
TTK_CARD_SEASONAL_MARK: Final = "🍂 сезон"

# --------------------------------------------------------------------------------------
# Inline mode (part IV of the stage 1 spec; TZ 5.5)
# --------------------------------------------------------------------------------------

#: The button that starts the inline search inside the chat with the bot. Telegram fills the
#: input field with «@бот » and the list then updates on every letter — which is the only way
#: a list *can* update while somebody types, because a bot is not told about a message until
#: it is sent.
TTK_INLINE_BUTTON: Final = "🔎 Быстрый поиск"
#: Shown above an empty inline answer when the query was typed somewhere other than the chat
#: with the bot. Pressing it opens that chat, where the search works (open question В-12).
TTK_INLINE_ELSEWHERE_BUTTON: Final = "Открыть в боте"
#: The second line of a row in the dropdown: the category, and as much of the composition as
#: fits. The category is not decoration — two drinks may share a name (decision D6).
TTK_INLINE_HINT_TEMPLATE: Final = "{category} · {composition}"
#: Between the ingredients of that line.
TTK_INLINE_HINT_SEPARATOR: Final = ", "
#: Appended when the composition did not fit into the line.
TTK_INLINE_HINT_MORE_MARK: Final = "…"

# --------------------------------------------------------------------------------------
# Units (TZ 4.4: the set is code, the numbers are the venue's)
# --------------------------------------------------------------------------------------

UNIT_ML: Final = "мл"
UNIT_L: Final = "л"
UNIT_G: Final = "г"
UNIT_KG: Final = "кг"
UNIT_PCS: Final = "шт"
UNIT_BOTTLE: Final = "бут"

_UNIT_LABELS: Final[Mapping[Unit, str]] = {
    Unit.ML: UNIT_ML,
    Unit.L: UNIT_L,
    Unit.G: UNIT_G,
    Unit.KG: UNIT_KG,
    Unit.PCS: UNIT_PCS,
    Unit.BOTTLE: UNIT_BOTTLE,
}


def unit_label(unit: Unit) -> str:
    """The short Russian form of a unit, as it is written on a card."""
    return _UNIT_LABELS[unit]


__all__ = [
    "TTK_CARD_AMOUNT_TEMPLATE",
    "TTK_CARD_COMPOSITION_TITLE",
    "TTK_CARD_GARNISH_TEMPLATE",
    "TTK_CARD_GLASSWARE_TEMPLATE",
    "TTK_CARD_ICE_TEMPLATE",
    "TTK_CARD_INSTRUCTION_TEMPLATE",
    "TTK_CARD_LINE_PLAIN_TEMPLATE",
    "TTK_CARD_LINE_TEMPLATE",
    "TTK_CARD_METHOD_TEMPLATE",
    "TTK_CARD_NAME_TEMPLATE",
    "TTK_CARD_PREP_BUTTON_TEMPLATE",
    "TTK_CARD_SEASONAL_MARK",
    "TTK_CARD_SEPARATOR",
    "TTK_CARD_YIELD_BUTTON_TEMPLATE",
    "TTK_EARLIER_BUTTON",
    "TTK_EMPTY",
    "TTK_FOUND_TEMPLATE",
    "TTK_HIT_BUTTON_TEMPLATE",
    "TTK_INLINE_BUTTON",
    "TTK_INLINE_ELSEWHERE_BUTTON",
    "TTK_INLINE_HINT_MORE_MARK",
    "TTK_INLINE_HINT_SEPARATOR",
    "TTK_INLINE_HINT_TEMPLATE",
    "TTK_MAYBE_TITLE",
    "TTK_MORE_BUTTON",
    "TTK_NOTHING_FOUND_TEMPLATE",
    "TTK_PAGE_TEMPLATE",
    "TTK_REPORTED",
    "TTK_REPORT_MISSING_BUTTON",
    "TTK_SEARCH_BUTTON",
    "TTK_SEARCH_PROMPT",
    "TTK_TITLE",
    "UNIT_BOTTLE",
    "UNIT_G",
    "UNIT_KG",
    "UNIT_L",
    "UNIT_ML",
    "UNIT_PCS",
    "unit_label",
]
