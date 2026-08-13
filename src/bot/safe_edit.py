"""Editing one message instead of writing forty (TZ 8.2, 9; plan task 20).

TZ 8.2 requires that a checklist, a pager and a wizard **edit one message** rather than fill
the chat with a new one per tap. Doing that against the Bot API means three outcomes, and
only one of them is the happy path:

* **the text is identical** — Telegram answers `Bad Request: message is not modified`.
  Ticking a line that is already ticked, or a double tap arriving twice, produces exactly
  this. It is not a failure: the screen already shows what we wanted it to show, so it is
  reported as :attr:`EditOutcome.UNCHANGED` and nobody is told anything;
* **the message is gone** — `Bad Request: message to edit not found`, because the user
  swiped it away or Telegram aged it out (TZ 5.4 mentions the case in so many words). The
  answer is to send the screen again and **write the new `message_id` down**, or the next
  tap edits the same ghost forever. The write is the caller's `remember` callback: this
  module knows about messages, not about which service owns the record;
* **anything else** propagates to the error middleware, which apologises and logs.

**The callback query is answered on every one of those branches, including the one that
raises.** TZ 9 gives an action one second to respond, and an unanswered callback leaves a
spinner on the button for a minute — the bartender's read on that is a broken bot, and they
press it again. The answer therefore lives in a `finally`, and a failure to deliver it is
swallowed: it is the consolation, not the work.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from src.logging import get_logger

logger = get_logger("bot.safe_edit")

#: Telegram's wording for "the new text equals the old one". Matched as a substring
#: because the API prefixes it with `Bad Request: ` and sometimes adds a suffix.
NOT_MODIFIED_MARKER: Final = "message is not modified"

#: Telegram's wording for "there is nothing at that message id any more".
NOT_FOUND_MARKER: Final = "message to edit not found"

#: Persists the id of a message that had to be sent anew: `(chat_id, message_id)`.
Remember = Callable[[int, int], Awaitable[None]]


class EditOutcome(enum.StrEnum):
    """What actually happened to the screen."""

    #: The message was rewritten.
    EDITED = "edited"
    #: The message already said this. A success (see the module docstring).
    UNCHANGED = "unchanged"
    #: The message was gone, so a new one carries the screen from now on.
    RESENT = "resent"


@dataclass(frozen=True, slots=True)
class EditResult:
    """Where the screen lives now, and how it got there."""

    outcome: EditOutcome
    chat_id: int
    message_id: int

    @property
    def is_resent(self) -> bool:
        return self.outcome is EditOutcome.RESENT


class CallbackAnswer:
    """The `answer()` of one callback query, called at most once.

    A protocol-shaped wrapper rather than the `CallbackQuery` itself, so that a caller with
    no callback (a scheduled edit, a message-driven wizard) passes nothing and the code has
    no branch for it.
    """

    def __init__(self, answer: Callable[..., Awaitable[object]] | None) -> None:
        self._answer = answer
        self._answered = False

    async def close(self, text: str | None = None) -> None:
        if self._answer is None or self._answered:
            return
        self._answered = True
        try:
            await self._answer(text)
        except Exception:
            # The query expired, or the network went away while we were answering. The
            # user's action already happened; this is the spinner, not the work.
            logger.warning("callback query could not be answered", exc_info=True)


def is_not_modified(error: TelegramBadRequest) -> bool:
    return NOT_MODIFIED_MARKER in error.message.lower()


def is_gone(error: TelegramBadRequest) -> bool:
    return NOT_FOUND_MARKER in error.message.lower()


async def safe_edit(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    answer: Callable[..., Awaitable[object]] | None = None,
    notice: str | None = None,
    remember: Remember | None = None,
) -> EditResult:
    """Rewrite a message, or replace it, and always close the spinner.

    `answer` is `callback.answer` when the edit is driven by a button press; `notice` is
    the short toast to show with it. `remember` is called with `(chat_id, message_id)` only
    when a new message had to be sent, so the caller can store the new id (TZ 5.4: the
    checklist keeps `chat_id`/`message_id` on the run).
    """
    spinner = CallbackAnswer(answer)
    try:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as error:
            if is_not_modified(error):
                return EditResult(EditOutcome.UNCHANGED, chat_id, message_id)
            if not is_gone(error):
                raise
            sent = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
            )
            new_id = _message_id_of(sent)
            if remember is not None:
                await remember(chat_id, new_id)
            logger.info(
                "screen was gone, sent a new message",
                extra={"chat_id": chat_id, "message_id": new_id},
            )
            return EditResult(EditOutcome.RESENT, chat_id, new_id)
        return EditResult(EditOutcome.EDITED, chat_id, message_id)
    finally:
        await spinner.close(notice)


def _message_id_of(sent: Message) -> int:
    return sent.message_id


__all__ = [
    "NOT_FOUND_MARKER",
    "NOT_MODIFIED_MARKER",
    "CallbackAnswer",
    "EditOutcome",
    "EditResult",
    "Remember",
    "is_gone",
    "is_not_modified",
    "safe_edit",
]
