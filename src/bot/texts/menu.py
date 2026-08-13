"""Captions of the main menu (TZ 5.2).

Seven captions are declared and three of them are not rendered at stage 0: «Списание»,
«Заказ» and «База знаний» are switched off by the availability flag of the registry in
`src/bot/keyboards/menu.py` (decision B7). They live here anyway, because the flag is what
turns them on at stage 1 — the wording and the keyboard code are then already written, which
is principle 1.4#4 applied to our own delivery.

TZ 8.2 caps a button at 20 characters and `tests/bot/test_texts.py` measures every constant
whose name ends in `_BUTTON`, this file included. The emoji count: «📖 ТТК и рецепты» is
15 characters and there is no room for a longer word.
"""

from __future__ import annotations

from typing import Final

#: TZ 5.2, first row.
MENU_SHIFT_BUTTON: Final = "📋 Моя смена"
MENU_SCHEDULE_BUTTON: Final = "📅 График"
#: TZ 5.2, second row. «ТТК» is a Cyrillic abbreviation of the trade: технико-
#: технологическая карта.
MENU_CATALOGUE_BUTTON: Final = "📖 ТТК и рецепты"
MENU_KNOWLEDGE_BUTTON: Final = "❓ База знаний"
#: TZ 5.2, third row.
MENU_WRITEOFF_BUTTON: Final = "📉 Списание"
MENU_ORDER_BUTTON: Final = "📦 Заказ"
#: TZ 5.2: added for `manager` and `owner` (TZ 5.8).
MENU_MANAGEMENT_BUTTON: Final = "⚙️ Управление"

#: Shown once with the keyboard; the menu itself is permanent, so this is not repeated on
#: every return to it.
MENU_PROMPT: Final = "Чем помочь?"

__all__ = [
    "MENU_CATALOGUE_BUTTON",
    "MENU_KNOWLEDGE_BUTTON",
    "MENU_MANAGEMENT_BUTTON",
    "MENU_ORDER_BUTTON",
    "MENU_PROMPT",
    "MENU_SCHEDULE_BUTTON",
    "MENU_SHIFT_BUTTON",
    "MENU_WRITEOFF_BUTTON",
]
