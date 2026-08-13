"""«📋 Моя смена» and «📅 График» (TZ 5.3).

Two screens, and both of them are empty on the day a venue starts (TZ 8.1), so the empty
state is the first constant of each and not an afterthought: a new bar sees «Смен пока нет»
before it ever sees a shift.

The window itself is `common.TIME_RANGE_TEMPLATE` — one dash for the whole product.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------------------
# «📋 Моя смена» (TZ 5.3)
# --------------------------------------------------------------------------------------

#: TZ 8.1: the venue has no schedule yet, and this is a normal screen, not an error.
SHIFT_NONE: Final = "Смен пока нет. Появятся в графике — покажу здесь."

#: The shift is on right now (the window covers the current moment).
SHIFT_NOW_TITLE: Final = "Вы в смене"
#: Nothing is on right now, so this is the next one.
SHIFT_NEXT_TITLE: Final = "Ближайшая смена"
SHIFT_WHEN_TEMPLATE: Final = "{date}, {window}"

#: TZ 5.3: «открывает он или нет».
SHIFT_YOU_OPEN: Final = "Открываете вы."
SHIFT_YOU_CLOSE: Final = "Закрываете вы."

#: TZ 5.3: «кто ещё в смене».
SHIFT_TEAM_TITLE: Final = "Вместе с вами:"
SHIFT_TEAM_LINE_TEMPLATE: Final = "• {full_name}, {window}"
SHIFT_TEAM_ALONE: Final = "Больше в эту смену никого."

#: TZ 5.4: «сотрудник может открыть текущий чек-лист в любой момент через 📋 Моя смена».
SHIFT_OPEN_CHECKLIST_BUTTON: Final = "Чек-лист открытия"

# --------------------------------------------------------------------------------------
# «📅 График» (TZ 5.3, two weeks ahead)
# --------------------------------------------------------------------------------------

SCHEDULE_TITLE: Final = "Ваши смены на две недели"
SCHEDULE_EMPTY: Final = "На две недели вперёд смен нет."
SCHEDULE_LINE_TEMPLATE: Final = "{date} · {window}{mark}"
#: Appended to the line above; empty for an ordinary shift.
SCHEDULE_OPENER_MARK: Final = " · открытие"
SCHEDULE_CLOSER_MARK: Final = " · закрытие"

__all__ = [
    "SCHEDULE_CLOSER_MARK",
    "SCHEDULE_EMPTY",
    "SCHEDULE_LINE_TEMPLATE",
    "SCHEDULE_OPENER_MARK",
    "SCHEDULE_TITLE",
    "SHIFT_NEXT_TITLE",
    "SHIFT_NONE",
    "SHIFT_NOW_TITLE",
    "SHIFT_OPEN_CHECKLIST_BUTTON",
    "SHIFT_TEAM_ALONE",
    "SHIFT_TEAM_LINE_TEMPLATE",
    "SHIFT_TEAM_TITLE",
    "SHIFT_WHEN_TEMPLATE",
    "SHIFT_YOU_CLOSE",
    "SHIFT_YOU_OPEN",
]
