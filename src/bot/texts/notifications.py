"""Bodies of the messages the worker sends (TZ 6; plan tasks 19, 31, 32).

The types are not invented here. `src/services/notifications.py` declares them
(:class:`~src.services.notifications.NotificationType`) and writes rows whose payload is
already the snapshot the manager must see — for the two checklist alerts, the pending lines
in critical-first order (TZ 6), so nothing below sorts or decides what to mark. This module
supplies the wording those payloads are poured into, and `notification_headline` is the one
lookup from a queued type to its opening line: a type added at stage 1 without wording fails
`tests/bot/test_texts.py` instead of reaching a manager as an empty message.

Who receives what is answer A4 and lives in `AccessService.manager_recipient_ids`; not a
word about recipients here.

Placeholders, by type:

* `opening_checklist` — `{start}`; goes to the opener, followed by the checklist message
  itself (the renderer creates the run, see plan task 32);
* `checklist_template_empty` — `{checklist}`, `{full_name}` (payload: `checklist_type`,
  `template_id`, `shift_id`, `user_id`);
* `checklist_skipped` and `checklist_overdue` — `{checklist}`, `{full_name}`, then the
  pending lines and, for the first of the two, `skip_comment` (TZ 5.4);
* `recipe_missing` — `{full_name}`, `{query}` (payload: `query`, `reported_by`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from src.services.notifications import NotificationType

# --------------------------------------------------------------------------------------
# Headlines, one per type of TZ section 6
# --------------------------------------------------------------------------------------

#: To the opener, `opening_checklist_lead_minutes` before the shift (TZ 5.4, 6).
NOTIFY_OPENING_CHECKLIST_TEMPLATE: Final = "Смена в {start}. Вот чек-лист открытия."
#: TZ 8.1 and decision B1: the employee got nothing, and the manager is told why.
NOTIFY_CHECKLIST_NOT_FILLED_TEMPLATE: Final = (
    "{checklist} не ушёл: в шаблоне нет ни одного пункта. Смена у {full_name}."
)
#: TZ 5.4: finished with lines left unticked.
NOTIFY_CHECKLIST_SKIPPED_TEMPLATE: Final = "{full_name} закрыл(а) {checklist} не полностью."
#: TZ 6: `checklist_overdue_minutes` after the start of the shift, still unfinished.
NOTIFY_CHECKLIST_OVERDUE_TEMPLATE: Final = "{checklist} не завершён вовремя. Смена у {full_name}."
#: TZ 5.5: somebody looked for a card that is not there.
NOTIFY_MISSING_CARD_TEMPLATE: Final = "{full_name} искал(а) «{query}». Такой карты нет."

_HEADLINE_BY_TYPE: Final[Mapping[str, str]] = {
    NotificationType.OPENING_CHECKLIST: NOTIFY_OPENING_CHECKLIST_TEMPLATE,
    NotificationType.CHECKLIST_TEMPLATE_EMPTY: NOTIFY_CHECKLIST_NOT_FILLED_TEMPLATE,
    NotificationType.CHECKLIST_SKIPPED: NOTIFY_CHECKLIST_SKIPPED_TEMPLATE,
    NotificationType.CHECKLIST_OVERDUE: NOTIFY_CHECKLIST_OVERDUE_TEMPLATE,
    NotificationType.RECIPE_MISSING: NOTIFY_MISSING_CARD_TEMPLATE,
}


def notification_headline(notification_type: str) -> str:
    """The opening line of a queued notification.

    Raises :class:`KeyError` for a type nobody wrote wording for — the same shape of
    failure as `NotificationRegistry.spec`, and the worker turns it into `failed` rather
    than delivering a blank message (plan task 31).
    """
    return _HEADLINE_BY_TYPE[notification_type]


# --------------------------------------------------------------------------------------
# The body the two checklist alerts share (TZ 5.4, 6)
# --------------------------------------------------------------------------------------

#: The payload carries the lines critical-first; the renderer prints them in that order.
NOTIFY_PENDING_TITLE: Final = "Осталось не отмечено:"
NOTIFY_PENDING_LINE_TEMPLATE: Final = "• {text}"
#: TZ 6: a critical line is escalated, so it is marked where the manager cannot miss it.
NOTIFY_PENDING_CRITICAL_LINE_TEMPLATE: Final = "❗ {text}"
NOTIFY_PENDING_WITH_GROUP_TEMPLATE: Final = "• {text} ({group_name})"
#: TZ 5.4: the short reason the employee typed.
NOTIFY_SKIP_COMMENT_TEMPLATE: Final = "Причина: {comment}"

__all__ = [
    "NOTIFY_CHECKLIST_NOT_FILLED_TEMPLATE",
    "NOTIFY_CHECKLIST_OVERDUE_TEMPLATE",
    "NOTIFY_CHECKLIST_SKIPPED_TEMPLATE",
    "NOTIFY_MISSING_CARD_TEMPLATE",
    "NOTIFY_OPENING_CHECKLIST_TEMPLATE",
    "NOTIFY_PENDING_CRITICAL_LINE_TEMPLATE",
    "NOTIFY_PENDING_LINE_TEMPLATE",
    "NOTIFY_PENDING_TITLE",
    "NOTIFY_PENDING_WITH_GROUP_TEMPLATE",
    "NOTIFY_SKIP_COMMENT_TEMPLATE",
    "notification_headline",
]
