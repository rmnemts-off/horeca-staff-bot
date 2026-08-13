"""The gate: an unknown `telegram_id` reaches no handler at all (TZ 5.1, plan task 20).

TZ 5.1 is unusually blunt about it: a `/start` with no code from an unknown user is
answered with "you are not in the system, ask the manager for a code" -- **and nothing
else, not a hint of what the bot can do**. The bot has no public half: no help text, no
list of venues, no hint that a schedule exists. That is a property of the *framework* and
not of the handlers —
a handler added at stage 1 must not be able to answer a stranger by forgetting a check, so
the refusal happens before routing and the handler is never called.

**What gets through the gate**, and nothing else does:

* somebody with an active membership — the normal case, with an `AccessContext` in
  `data["actor"]`;
* somebody who works in two venues and has not picked one yet (TZ 5.1) — no actor, so only
  the venue-choice screen can serve them;
* the bootstrap owner of decision A3, who by definition belongs to no venue yet and is on
  his way into the "create a venue" wizard;
* **an invite code** — typed by hand or arriving as `/start inv_XXXX`. This one is the
  reason the gate is not simply "is this id in the database": TZ 5.1 lets a person the bot
  has never seen turn a code into a membership, so an update that carries one is let
  through with the parsed code in `data["invite_code"]`. Parsing is `parse_invite_code()`
  from the access service — the middleware recognises a code, it does not define what one
  is (TZ 3.2: no business logic in middleware).

Somebody whose membership was deactivated (TZ 5.1 keeps the row so the reports keep their
history) has no active context, so the gate answers them exactly as it answers a stranger.
That is the intended reading: the row is history, not access.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from src.bot import texts
from src.bot.middlewares.errors import inner_event
from src.bot.middlewares.services import ACCESS_KEY
from src.logging import get_logger
from src.services.access import Identity, parse_invite_code

#: Handler-context key holding the whole :class:`~src.services.access.Identity`.
IDENTITY_KEY: Final = "identity"

#: Handler-context key holding the `AccessContext` of the current venue, or `None` while
#: the person has not got one (venue choice, the bootstrap owner, an invite being typed).
ACTOR_KEY: Final = "actor"

#: Handler-context key holding the invite code an update carries, canonical form.
INVITE_CODE_KEY: Final = "invite_code"

#: The command a deep link arrives as: `t.me/<bot>?start=inv_XXXX` (TZ 5.1).
START_COMMAND: Final = "/start"

logger = get_logger("bot.auth")

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class IdentityResolver(Protocol):
    """What this middleware needs of `AccessService`, and nothing more."""

    async def resolve(self, telegram_id: int) -> Identity: ...


def invite_code_in(text: str | None) -> str | None:
    """The invite code an incoming message carries, canonical, or `None`.

    Accepts the deep link (`/start inv_12-A7K9QX4M`) and the bare code typed into the chat,
    in any case and with any surrounding whitespace — `parse_invite_code()` decides all of
    that, this only strips the command in front of it. A plain `/start` carries no code and
    is answered by the refusal, which is TZ 5.1 word for word.

    **The command is dropped as a whole word, not as the literal `/start`.** Telegram spells
    a command with the bot's username whenever the client thinks it might be ambiguous —
    `/start@barpoint_bot inv_12-A7K9QX4M`, which is what every group chat sends and what a
    client sends after a mention. Comparing against the bare string leaves `@barpoint_bot`
    glued to the payload, the code stops being a code, and the person who followed an
    invite link is told they are not in the system.
    """
    if not text:
        return None
    words = text.strip().split(maxsplit=1)
    if not words:
        return None
    candidate = text.strip()
    head = words[0].lower()
    # A whole word, not a prefix: `startswith` alone would also swallow `/startle`, which is
    # not the command. Telegram writes either the bare command or the command with the bot's
    # username glued on with `@`, and nothing else.
    if head == START_COMMAND or head.startswith(f"{START_COMMAND}@"):
        candidate = words[1] if len(words) > 1 else ""
    parsed = parse_invite_code(candidate)
    return None if parsed is None else parsed[1]


class AuthMiddleware(BaseMiddleware):
    """Outer middleware of `Dispatcher.update`; see the module docstring."""

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        target = inner_event(event)
        user = getattr(target, "from_user", None)
        if user is None:
            # An update with nobody behind it (a channel post, an edited business message
            # of a chat the bot was added to). There is nobody to authorise and nothing
            # this bot does with it (TZ 5.10 keeps group chats out of stage 0).
            return None

        access: IdentityResolver = data[ACCESS_KEY]
        identity = await access.resolve(user.id)
        invite = invite_code_in(getattr(target, "text", None))

        data[IDENTITY_KEY] = identity
        data[ACTOR_KEY] = identity.active
        data[INVITE_CODE_KEY] = invite

        if self._may_pass(identity, invite):
            return await handler(event, data)

        logger.info("update refused: no membership", extra={"telegram_id": user.id})
        await self._offer_a_code(target)
        return None

    @staticmethod
    def _may_pass(identity: Identity, invite: str | None) -> bool:
        return (
            identity.active is not None
            or identity.needs_venue_choice
            or identity.may_create_venue
            or invite is not None
        )

    @staticmethod
    async def _offer_a_code(target: TelegramObject) -> None:
        """TZ 5.1: the code prompt and nothing else.

        A callback query gets the neutral refusal instead: a button can only come from a
        keyboard this bot drew, so its presser is either a member who has just been
        deactivated or somebody replaying somebody else's message. Neither is told what the
        button did (TZ 9).
        """
        if isinstance(target, CallbackQuery):
            await target.answer(texts.ERROR_NOT_ALLOWED, show_alert=True)
        elif isinstance(target, Message):
            await target.answer(texts.ONBOARDING_NO_ACCESS)


def setup(dispatcher: Any) -> AuthMiddleware:
    """Register the gate on updates, after the container middleware."""
    middleware = AuthMiddleware()
    dispatcher.update.outer_middleware(middleware)
    return middleware


__all__ = [
    "ACTOR_KEY",
    "IDENTITY_KEY",
    "INVITE_CODE_KEY",
    "AuthMiddleware",
    "IdentityResolver",
    "invite_code_in",
    "setup",
]
