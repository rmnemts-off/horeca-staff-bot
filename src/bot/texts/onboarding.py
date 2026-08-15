"""Entering the system: `/start`, an invite code, the name and the position (TZ 5.1).

The first rule of 5.1 is the one that shapes this module: `/start` from a stranger answers
«Вас нет в системе. Попросите код у управляющего» **and nothing else** — no hint of what
the bot can do, no list of venues, no offer to write to anybody. So the refusal is a single
constant with no siblings, and every other string here is behind a valid code.

The name comes from the manager who issued the code (decision B8) and is only confirmed by
the employee: the schedule is matched to people by full name at import time (TZ 5.3), and a
Telegram profile holds nicknames.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------------------
# Without a code (TZ 5.1)
# --------------------------------------------------------------------------------------

#: TZ 5.1 verbatim, and the whole answer to a `/start` from an unknown user.
ONBOARDING_NO_ACCESS: Final = "Вас нет в системе. Попросите код у управляющего."

#: The code was typed by hand and did not match anything live.
ONBOARDING_CODE_UNKNOWN: Final = "Такого кода нет. Проверьте его у управляющего."
#: TZ 5.1: a code lives seven days and is used once.
ONBOARDING_CODE_EXPIRED: Final = "Срок кода вышел. Попросите новый."
ONBOARDING_CODE_USED: Final = "Этот код уже сработал. Попросите новый."
#: The person already works here, so the code was not theirs to use. It stays live for
#: whoever it was issued to, and the sentence says why nothing happened.
#: A rebind code typed by an account that already belongs to somebody else. The manager
#: has to work out whose card this is; a fresh code would not answer that.
ONBOARDING_ACCOUNT_TAKEN: Final = "Этот телеграм уже привязан к другому сотруднику."
#: The card the code pointed at is gone or switched off.
ONBOARDING_CARD_UNAVAILABLE: Final = "Карточка недоступна. Обратитесь к управляющему."
#: TZ 5.1: the second side of a rebind confirms before anything moves.
ONBOARDING_REBIND_CONFIRM_TEMPLATE: Final = (
    "Это карточка {full_name} в «{venue}». Привязать её к этому телеграму?"
)
ONBOARDING_REBIND_DONE_TEMPLATE: Final = "Готово. Теперь «{venue}» открывается отсюда."
ONBOARDING_CODE_ALREADY_MEMBER: Final = (
    "Вы уже в команде — код не понадобился. Роль меняет управляющий."
)

# --------------------------------------------------------------------------------------
# With a code
# --------------------------------------------------------------------------------------

ONBOARDING_WELCOME_TEMPLATE: Final = "Здравствуйте! Код от заведения «{venue}»."
ONBOARDING_NAME_CONFIRM_TEMPLATE: Final = "Записан(а) как {full_name}, {position}. Верно?"
ONBOARDING_NAME_CONFIRM_SHORT_TEMPLATE: Final = "Записан(а) как {full_name}. Верно?"
ONBOARDING_NAME_EDIT_BUTTON: Final = "Поправить имя"
ONBOARDING_NAME_PROMPT: Final = "Напишите имя и фамилию — так вас увидят в графике."
ONBOARDING_NAME_TOO_SHORT: Final = "Слишком коротко. Напишите имя и фамилию."
ONBOARDING_DONE_TEMPLATE: Final = "Готово, {full_name}. Вы в команде «{venue}»."
#: TZ 5.1: the row of a dismissed employee survives, so coming back is a return and not
#: a first day. Greeting somebody who worked here for a year as a newcomer reads as a
#: system that forgot them.
ONBOARDING_WELCOME_BACK_TEMPLATE: Final = "С возвращением, {full_name}. Вы снова в «{venue}»."

#: TZ 5.1: somebody who works in two places picks one, and it is remembered.
ONBOARDING_PICK_VENUE: Final = "Выберите заведение."

#: Decision A3: the first owner arrives before any venue exists.
ONBOARDING_CREATE_VENUE_OFFER: Final = "Заведения пока нет. Создадим?"
ONBOARDING_CREATE_VENUE_BUTTON: Final = "Создать заведение"

__all__ = [
    "ONBOARDING_ACCOUNT_TAKEN",
    "ONBOARDING_CARD_UNAVAILABLE",
    "ONBOARDING_CODE_ALREADY_MEMBER",
    "ONBOARDING_CODE_EXPIRED",
    "ONBOARDING_CODE_UNKNOWN",
    "ONBOARDING_CODE_USED",
    "ONBOARDING_CREATE_VENUE_BUTTON",
    "ONBOARDING_CREATE_VENUE_OFFER",
    "ONBOARDING_DONE_TEMPLATE",
    "ONBOARDING_NAME_CONFIRM_SHORT_TEMPLATE",
    "ONBOARDING_NAME_CONFIRM_TEMPLATE",
    "ONBOARDING_NAME_EDIT_BUTTON",
    "ONBOARDING_NAME_PROMPT",
    "ONBOARDING_NAME_TOO_SHORT",
    "ONBOARDING_NO_ACCESS",
    "ONBOARDING_PICK_VENUE",
    "ONBOARDING_REBIND_CONFIRM_TEMPLATE",
    "ONBOARDING_REBIND_DONE_TEMPLATE",
    "ONBOARDING_WELCOME_BACK_TEMPLATE",
    "ONBOARDING_WELCOME_TEMPLATE",
]
