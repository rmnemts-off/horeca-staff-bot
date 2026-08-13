"""What the user sees when something did not work (TZ 8.2, 9).

Never a traceback and never a code: the apology is one sentence, and the `request_id` that
makes the failure findable goes to the log, not to the bartender (TZ 9).

`ERROR_SOMETHING_WENT_WRONG` is not a new name. `src/bot/middlewares/errors.py` was written
before this package and named the key it would read (`ERROR_MESSAGE_KEY`) instead of the
phrase, so that it could stay free of Russian; this constant is what that lookup resolves
to, and `tests/bot/test_texts.py` asserts the two still agree.
"""

from __future__ import annotations

from typing import Final

#: TZ 8.2 verbatim: «Что-то пошло не так, попробуйте ещё раз».
ERROR_SOMETHING_WENT_WRONG: Final = "Что-то пошло не так, попробуйте ещё раз."

#: TZ 9 and 3.3: rights are checked on the server, and a callback that names somebody
#: else's object is refused there. The wording says nothing about what was asked for —
#: whether the object exists at all is not this user's business.
ERROR_NOT_ALLOWED: Final = "Это действие недоступно."

#: The object is gone (deleted while the screen was open), not forbidden.
ERROR_GONE: Final = "Не получилось: этого больше нет."

#: A tap in a message that has been superseded — an old checklist message, a stale page
#: (TZ 5.4: «нажатие в устаревшем сообщении данные не меняет»).
ERROR_OUTDATED_SCREEN: Final = "Сообщение устарело, откройте раздел заново."

#: Rate limiting (TZ 9). Soft wording: the bartender did nothing wrong, just fast.
ERROR_TOO_FAST: Final = "Слишком часто, подождите секунду."

__all__ = [
    "ERROR_GONE",
    "ERROR_NOT_ALLOWED",
    "ERROR_OUTDATED_SCREEN",
    "ERROR_SOMETHING_WENT_WRONG",
    "ERROR_TOO_FAST",
]
