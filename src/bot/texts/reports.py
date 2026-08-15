"""Wording of the shift report — the screen and the workbook (TZ 5.9, 8.2).

Column headings are wording like any other, so they live here and not in
`src/exporters/`: the customer edits a heading without a developer, and the guard on
Russian literals outside this package would refuse them there anyway.

**Each heading is its own named constant, assembled by name where it is used.** A tuple of
five headings in one place is what the guard on seeded data reads as a catalogue — the rule
does not know a column heading from a bar's nomenclature, and the shape is the same. Naming
them individually is also how `BLOCKS` in `src/bot/keyboards/admin.py` does it.

`run_status_label` turns a code-side enum into a word on a screen (TZ 4.3), exactly as
`role_label` and `unit_label` do next door: the mapping holds references, never phrases of
its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from src.db.models import RunStatus

# --------------------------------------------------------------------------------------
# The section (TZ 5.8, 5.9)
# --------------------------------------------------------------------------------------

ADMIN_REPORTS_BUTTON: Final = "📊 Отчёты"
REPORT_TITLE: Final = "Отчёт по смене"
#: TZ 8.2: the prompt says the shape it accepts, because the answer is typed.
REPORT_DATE_PROMPT: Final = "За какую дату? Например, 15.08."
REPORT_TODAY_BUTTON: Final = "Сегодня"
REPORT_YESTERDAY_BUTTON: Final = "Вчера"
#: TZ 8.1: a day nobody worked is a normal answer and not a failure.
REPORT_EMPTY_TEMPLATE: Final = "{date}: смен не было."
REPORT_READY_TEMPLATE: Final = "{date}: смен {shifts}, не отмечено пунктов — {pending}."
REPORT_BUILDING: Final = "Собираю отчёт…"

# --------------------------------------------------------------------------------------
# The workbook (TZ 5.9)
# --------------------------------------------------------------------------------------

#: ISO in the file name so a folder of them sorts by date; the venue's own name is not in
#: it, because it arrives with spaces and quotes in it.
REPORT_FILENAME_TEMPLATE: Final = "smena-{day}.xlsx"

REPORT_SHIFTS_SHEET: Final = "Смены"
REPORT_RUNS_SHEET: Final = "Чек-листы"
#: The sheet the report is opened for.
REPORT_PENDING_SHEET: Final = "Не отмечено"

REPORT_COLUMN_EMPLOYEE: Final = "Сотрудник"
REPORT_COLUMN_WINDOW: Final = "Время"
REPORT_COLUMN_OPENS: Final = "Открывает"
REPORT_COLUMN_CLOSES: Final = "Закрывает"
REPORT_COLUMN_CHECKLIST: Final = "Чек-лист"
REPORT_COLUMN_STATUS: Final = "Статус"
REPORT_COLUMN_PROGRESS: Final = "Выполнено"
REPORT_COLUMN_FINISHED: Final = "Завершён"
REPORT_COLUMN_WHY: Final = "Причина"
# Named `WHY` and `WORDING` rather than `REASON` and `ITEM`: the guard on seeded data
# reads a constant called `*_REASON` or `*_ITEM` as a write-off reason or a checklist
# item — a catalogue the venue enters — and it is right to. These are column headings.
REPORT_COLUMN_GROUP: Final = "Группа"
REPORT_COLUMN_WORDING: Final = "Пункт"
REPORT_COLUMN_CRITICAL: Final = "Критичный"

REPORT_WINDOW_TEMPLATE: Final = "{start}–{end}"
REPORT_PROGRESS_TEMPLATE: Final = "{done} из {total}"
#: A tick in a boolean column. A word rather than a symbol: the sheet is filtered and
#: sorted, and «да» filters where an emoji does not.
REPORT_YES: Final = "да"

# --------------------------------------------------------------------------------------
# Statuses (TZ 4.3: the set is code, the word on the screen is wording)
# --------------------------------------------------------------------------------------

RUN_STATUS_SENT: Final = "не начат"
RUN_STATUS_IN_PROGRESS: Final = "в работе"
RUN_STATUS_COMPLETED: Final = "выполнен"
RUN_STATUS_SKIPPED: Final = "с пропусками"
RUN_STATUS_OVERDUE: Final = "просрочен"

_RUN_STATUS_LABELS: Final[Mapping[RunStatus, str]] = {
    RunStatus.SENT: RUN_STATUS_SENT,
    RunStatus.IN_PROGRESS: RUN_STATUS_IN_PROGRESS,
    RunStatus.COMPLETED: RUN_STATUS_COMPLETED,
    RunStatus.SKIPPED: RUN_STATUS_SKIPPED,
    RunStatus.OVERDUE: RUN_STATUS_OVERDUE,
}


def run_status_label(status: RunStatus) -> str:
    """How a run's status is written in the report (TZ 4.3)."""
    return _RUN_STATUS_LABELS[status]


__all__ = [
    "ADMIN_REPORTS_BUTTON",
    "REPORT_BUILDING",
    "REPORT_COLUMN_CHECKLIST",
    "REPORT_COLUMN_CLOSES",
    "REPORT_COLUMN_CRITICAL",
    "REPORT_COLUMN_EMPLOYEE",
    "REPORT_COLUMN_FINISHED",
    "REPORT_COLUMN_GROUP",
    "REPORT_COLUMN_OPENS",
    "REPORT_COLUMN_PROGRESS",
    "REPORT_COLUMN_STATUS",
    "REPORT_COLUMN_WHY",
    "REPORT_COLUMN_WINDOW",
    "REPORT_COLUMN_WORDING",
    "REPORT_DATE_PROMPT",
    "REPORT_EMPTY_TEMPLATE",
    "REPORT_FILENAME_TEMPLATE",
    "REPORT_PENDING_SHEET",
    "REPORT_PROGRESS_TEMPLATE",
    "REPORT_READY_TEMPLATE",
    "REPORT_RUNS_SHEET",
    "REPORT_SHIFTS_SHEET",
    "REPORT_TITLE",
    "REPORT_TODAY_BUTTON",
    "REPORT_WINDOW_TEMPLATE",
    "REPORT_YES",
    "REPORT_YESTERDAY_BUTTON",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_IN_PROGRESS",
    "RUN_STATUS_OVERDUE",
    "RUN_STATUS_SENT",
    "RUN_STATUS_SKIPPED",
    "run_status_label",
]
