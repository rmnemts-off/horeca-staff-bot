"""Global error middleware: the user never sees a traceback (TZ 8.2, 9; plan task 9).

One outer middleware on `Dispatcher.update` does three things, in this order:

1. **Opens a request context.** A fresh `request_id` is bound for the whole update and put
   into the handler context under `REQUEST_ID_FIELD`, so a service that logs on its own
   stamps the same id without being handed anything.
2. **Logs the action at INFO** (TZ 9: "INFO for user actions"), before and after the
   handler, with the update type, the `telegram_id`, the chat and the duration -- and
   nothing else. The filter in `src/logging.py` would drop a name anyway; this module does
   not collect one in the first place, which is the cheaper half of the same rule.
3. **Catches everything the handler raises**, logs it at ERROR with the traceback and the
   `request_id`, and answers the user with the apology from `src/bot/texts/`. The
   exception does not propagate: an update dies here, the bot stays up (TZ 9,
   "available 24/7").

The wording of the apology is **not** in this file. TZ 8.2 puts every Russian string into
`src/bot/texts/`, and that module belongs to plan task 21; this one only names the key it
will read (`ERROR_MESSAGE_KEY`) and looks it up at call time. Until task 21 lands the key
resolves to nothing: the user then gets a closed callback spinner and no message, which is
still not a traceback, and the gap is logged at WARNING so it cannot pass unnoticed.

Why the reply goes through the event and not through `bot.send_message`: a callback query
must be answered or the client spins for a minute, and a message must be answered in its
own chat. `CallbackQuery.answer()` is called on every branch, with the apology as an alert
when there is one -- the guarantee plan task 20 asks for, held here for the failure path.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import CancelHandler, SkipHandler
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject, Update
from aiogram.types.update import UpdateTypeLookupError

from src.bot import texts
from src.bot.inline import answer_nothing
from src.logging import REQUEST_ID_FIELD, get_logger, request_context

#: Name of the constant in `src/bot/texts/` holding "Something went wrong, try again"
#: (TZ 8.2). The wording is written by plan task 21; naming the key here is what keeps
#: this module free of Russian literals.
ERROR_MESSAGE_KEY: Final = "ERROR_SOMETHING_WENT_WRONG"

#: Update type reported when aiogram cannot name one (a field this version does not know).
UNKNOWN_EVENT_TYPE: Final = "unknown"

logger = get_logger("bot.errors")

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def apology_text() -> str | None:
    """The user-facing error message, or None while `texts/` does not define it yet."""
    value = getattr(texts, ERROR_MESSAGE_KEY, None)
    return value if isinstance(value, str) and value.strip() else None


def inner_event(event: TelegramObject) -> TelegramObject:
    """The concrete event of an update (message, callback query, ...), or the event itself."""
    if not isinstance(event, Update):
        return event
    try:
        return event.event
    except UpdateTypeLookupError:
        return event


def event_fields(event: TelegramObject) -> dict[str, Any]:
    """The whole of what an update contributes to a log line (TZ 9).

    `telegram_id` is the one identifier of a person that may be written down; the chat and
    the update carry ids of their own, which are not personal data. A name, a username, a
    phone and the text of the message are never read here.
    """
    fields: dict[str, Any] = {}
    if isinstance(event, Update):
        fields["update_id"] = event.update_id
        try:
            fields["event_type"] = event.event_type
        except UpdateTypeLookupError:
            fields["event_type"] = UNKNOWN_EVENT_TYPE
    else:
        fields["event_type"] = type(event).__name__

    target = inner_event(event)
    user = getattr(target, "from_user", None)
    if user is not None:
        fields["telegram_id"] = user.id
    chat = getattr(target, "chat", None)
    if chat is None:
        message = getattr(target, "message", None)
        chat = getattr(message, "chat", None)
    if chat is not None:
        fields["chat_id"] = chat.id
    return fields


class ErrorsMiddleware(BaseMiddleware):
    """Outer middleware of `Dispatcher.update`; see the module docstring."""

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        with request_context() as request_id:
            data[REQUEST_ID_FIELD] = request_id
            fields = event_fields(event)
            logger.info("update received", extra=fields)
            started = time.perf_counter()
            try:
                result = await handler(event, data)
            except (SkipHandler, CancelHandler):
                # Routing control flow of aiogram, not a failure: it must reach the router.
                raise
            except Exception:
                logger.exception(
                    "update failed",
                    extra={**fields, **_elapsed(started), "outcome_status": "failed"},
                )
                await self._apologise(event)
                return None
            logger.info(
                "update handled",
                extra={**fields, **_elapsed(started), "outcome_status": "ok"},
            )
            return result

    async def _apologise(self, event: TelegramObject) -> None:
        """Say sorry in the user's own chat. Never raises, never shows the traceback."""
        text = apology_text()
        if text is None:
            logger.warning(
                "apology text is not defined in src/bot/texts (key %s)", ERROR_MESSAGE_KEY
            )
        target = inner_event(event)
        try:
            if isinstance(target, CallbackQuery):
                # Always answered: an unanswered callback spins on the client for a minute.
                await target.answer(text=text, show_alert=text is not None)
            elif isinstance(target, InlineQuery):
                # An inline query has no room for an apology and every reason to be closed:
                # left unanswered it loads until the client gives up (`src/bot/inline.py`).
                # The traceback is in the log either way.
                await answer_nothing(target)
            elif isinstance(target, Message) and text is not None:
                await target.answer(text)
        except Exception:
            # The apology itself failed (blocked bot, expired query). Nothing left to do
            # for this update, and it must not replace the original traceback.
            logger.exception("apology could not be delivered")


def _elapsed(started: float) -> dict[str, int]:
    return {"duration_ms": int((time.perf_counter() - started) * 1000)}


def setup(dispatcher: Any) -> ErrorsMiddleware:
    """Register the middleware as the outermost one on updates (plan, task 20 wires it)."""
    middleware = ErrorsMiddleware()
    dispatcher.update.outer_middleware(middleware)
    return middleware


__all__ = [
    "ERROR_MESSAGE_KEY",
    "ErrorsMiddleware",
    "apology_text",
    "event_fields",
    "inner_event",
    "setup",
]
