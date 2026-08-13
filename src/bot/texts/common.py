"""Wording that belongs to no single screen: navigation, confirmation, formats.

TZ 5.2 puts «⬅️ Назад» and «🏠 В меню» into *every* submenu and TZ 8.2 an «Отмена» into
every step-by-step scenario. Declared once here and used by the keyboard constructor in
`src/bot/keyboards/menu.py`, they are a property the constructor can guarantee; spelled out
by each screen, they are three screens away from being forgotten in the fourth.

`TIME_RANGE_TEMPLATE` looks like over-abstraction for two braces and a dash, and is not: a
shift window is printed by «Моя смена», by «График», by the manager's schedule and inside a
notification, and three of the four writing it by hand is how «9:00–21:00» and «9:00-21:00»
end up on neighbouring screens.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------------------
# Navigation (TZ 5.2, 8.2)
# --------------------------------------------------------------------------------------

BACK_BUTTON: Final = "⬅️ Назад"
HOME_BUTTON: Final = "🏠 В меню"
CANCEL_BUTTON: Final = "Отмена"

# --------------------------------------------------------------------------------------
# Confirmation
# --------------------------------------------------------------------------------------

YES_BUTTON: Final = "Да"
NO_BUTTON: Final = "Нет"
SAVE_BUTTON: Final = "Сохранить"
CANCELLED: Final = "Отменил, ничего не сохранил."
SAVED: Final = "Готово, сохранил."

# --------------------------------------------------------------------------------------
# Leaving a half-filled scenario (TZ 5.2)
# --------------------------------------------------------------------------------------

#: TZ 5.2: a main-menu press ends an unfinished scenario, and asks first «only if there is
#: unsaved data». The TZ spells the question for one scenario («Прервать оформление
#: заказа?»); the router that asks it is the same for every scenario and does not know
#: which one is open, so the wording names none of them.
INTERRUPT_QUESTION: Final = "Прервать? Введённое не сохранится."
INTERRUPT_YES_BUTTON: Final = "Прервать"
INTERRUPT_NO_BUTTON: Final = "Продолжить"

# --------------------------------------------------------------------------------------
# Shared formats
# --------------------------------------------------------------------------------------

#: A shift window: «9:00–21:00». See the module docstring about the dash.
TIME_RANGE_TEMPLATE: Final = "{start}–{end}"
#: A date and the window on it: «14.08, 9:00–21:00».
DATE_AND_TIME_TEMPLATE: Final = "{date}, {time}"
#: One line of any bulleted list the bot prints.
BULLET_LINE_TEMPLATE: Final = "• {text}"

__all__ = [
    "BACK_BUTTON",
    "BULLET_LINE_TEMPLATE",
    "CANCELLED",
    "CANCEL_BUTTON",
    "DATE_AND_TIME_TEMPLATE",
    "HOME_BUTTON",
    "INTERRUPT_NO_BUTTON",
    "INTERRUPT_QUESTION",
    "INTERRUPT_YES_BUTTON",
    "NO_BUTTON",
    "SAVED",
    "SAVE_BUTTON",
    "TIME_RANGE_TEMPLATE",
    "YES_BUTTON",
]
