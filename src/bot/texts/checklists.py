"""The checklist message and the wording around finishing it (TZ 5.4).

**Not a single line of a checklist is here.** What a bar checks at opening is entered by
that bar and lives in `checklist_items`; this module holds the frame the entries are drawn
into — the group heading, the `[ ]` / `[✓]` marks, the counter, the «Готово» button.

The layout follows decision D11: the list is the *text* of the message, and the buttons are
compact numeric markers for the current group only. Forty inline buttons captioned «Наличие
гостевых салфеток» fit neither the 20-character rule of TZ 8.2 nor a phone screen.

`checklist_title` turns the type into a heading. The three types are code (TZ 4.3 plus
decision D1), so a label per type is interface wording and not venue content; the mapping
holds references to the constants above it, never phrases of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from src.db.models import ChecklistType

# --------------------------------------------------------------------------------------
# Headings
# --------------------------------------------------------------------------------------

CHECKLIST_OPENING_TITLE: Final = "Чек-лист открытия"
CHECKLIST_CLOSING_TITLE: Final = "Чек-лист закрытия"
CHECKLIST_STOCK_TITLE: Final = "Проверка наличия"

_TITLE_BY_TYPE: Final[Mapping[ChecklistType, str]] = {
    ChecklistType.OPENING: CHECKLIST_OPENING_TITLE,
    ChecklistType.CLOSING: CHECKLIST_CLOSING_TITLE,
    ChecklistType.STOCK: CHECKLIST_STOCK_TITLE,
}


def checklist_title(checklist_type: ChecklistType) -> str:
    """The heading of a checklist of this type, for a screen or a notification."""
    return _TITLE_BY_TYPE[checklist_type]


# --------------------------------------------------------------------------------------
# The message (TZ 5.4, decision D11)
# --------------------------------------------------------------------------------------

#: A group heading. `group_name` is the venue's own word; the number is ours.
CHECKLIST_GROUP_TEMPLATE: Final = "{index}. {group_name}"
#: A group the venue left unnamed still needs a heading to break up forty lines.
CHECKLIST_GROUP_UNNAMED_TEMPLATE: Final = "Группа {index}"
CHECKLIST_LINE_DONE_TEMPLATE: Final = "[✓] {text}"
CHECKLIST_LINE_PENDING_TEMPLATE: Final = "[ ] {text}"
#: TZ 5.4: `is_critical` is marked in place, so a skipped one is visible before «Готово».
CHECKLIST_CRITICAL_MARK: Final = "❗"
#: TZ 5.4 verbatim: «Выполнено 3 из 8».
CHECKLIST_PROGRESS_TEMPLATE: Final = "Выполнено {done} из {total}"
#: The compact marker of one line, decision D11. The caption is a number, so the 20-char
#: rule is never in danger however long the venue's own wording is.
CHECKLIST_MARK_BUTTON_TEMPLATE: Final = "{number}"
CHECKLIST_MARK_DONE_BUTTON_TEMPLATE: Final = "✓{number}"
#: Switching between groups without leaving the message.
CHECKLIST_GROUP_BUTTON_TEMPLATE: Final = "{index}. {group_name}"
CHECKLIST_FINISH_BUTTON: Final = "Готово"

#: TZ 5.4: a line with `requires_photo` is ticked only after the photo arrives.
CHECKLIST_PHOTO_PROMPT: Final = "Пришлите фото — тогда отмечу."
CHECKLIST_COMMENT_PROMPT: Final = "Напишите пару слов по этому пункту."

# --------------------------------------------------------------------------------------
# Finishing (TZ 5.4)
# --------------------------------------------------------------------------------------

#: TZ 5.4 verbatim: everything ticked, the employee hears this and the manager nothing.
CHECKLIST_COMPLETED: Final = "Смена открыта, хорошего дня!"
CHECKLIST_CLOSING_COMPLETED: Final = "Всё отмечено, спокойной ночи!"

#: TZ 5.4: «Не выполнено: <список>. Отправить как есть?»
CHECKLIST_PENDING_QUESTION_TEMPLATE: Final = "Осталось не отмечено:\n{lines}\nОтправить как есть?"
CHECKLIST_SEND_AS_IS_BUTTON: Final = "Отправить как есть"
CHECKLIST_KEEP_GOING_BUTTON: Final = "Дочекать"
#: TZ 5.4: «просит короткий комментарий-причину».
CHECKLIST_SKIP_COMMENT_PROMPT: Final = "Коротко: почему не отметили?"
CHECKLIST_SKIP_SENT: Final = "Отправил управляющему. Хорошей смены!"

CHECKLIST_ALREADY_FINISHED: Final = "Этот чек-лист уже завершён."
#: TZ 8.1 and decision B1: the template exists but has no lines, so nothing was sent to
#: the employee at all — this is what «Моя смена» says if he looks for it anyway.
CHECKLIST_NOT_FILLED_IN: Final = "Чек-лист ещё не заполнен. Управляющий уже знает."

__all__ = [
    "CHECKLIST_ALREADY_FINISHED",
    "CHECKLIST_CLOSING_COMPLETED",
    "CHECKLIST_CLOSING_TITLE",
    "CHECKLIST_COMMENT_PROMPT",
    "CHECKLIST_COMPLETED",
    "CHECKLIST_CRITICAL_MARK",
    "CHECKLIST_FINISH_BUTTON",
    "CHECKLIST_GROUP_BUTTON_TEMPLATE",
    "CHECKLIST_GROUP_TEMPLATE",
    "CHECKLIST_GROUP_UNNAMED_TEMPLATE",
    "CHECKLIST_KEEP_GOING_BUTTON",
    "CHECKLIST_LINE_DONE_TEMPLATE",
    "CHECKLIST_LINE_PENDING_TEMPLATE",
    "CHECKLIST_MARK_BUTTON_TEMPLATE",
    "CHECKLIST_MARK_DONE_BUTTON_TEMPLATE",
    "CHECKLIST_NOT_FILLED_IN",
    "CHECKLIST_OPENING_TITLE",
    "CHECKLIST_PENDING_QUESTION_TEMPLATE",
    "CHECKLIST_PHOTO_PROMPT",
    "CHECKLIST_PROGRESS_TEMPLATE",
    "CHECKLIST_SEND_AS_IS_BUTTON",
    "CHECKLIST_SKIP_COMMENT_PROMPT",
    "CHECKLIST_SKIP_SENT",
    "CHECKLIST_STOCK_TITLE",
    "checklist_title",
]
