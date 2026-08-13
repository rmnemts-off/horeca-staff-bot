"""The venue of the update, and the services scoped to it (TZ 3.3, plan task 20).

CLAUDE.md states the rule this middleware exists for: a middleware puts `venue_id` into the
context on every update, and **the repositories are required to filter by it**. It is
therefore the *only* place a venue is chosen: `data["venue_id"]` comes from the actor's
membership, never from anything a client sent, and every repository built here is
constructed around that number. A forged `callback_data` cannot move a query to another
venue because there is no argument through which a venue could be passed.

Updates with no actor — the venue choice of TZ 5.1, the bootstrap owner of decision A3, an
invite code being typed — pass through with no venue and no services. There is nothing to
scope yet, and the screens that serve those cases work through `AccessService`, which is
global by necessity.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from src.bot import texts
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.errors import inner_event
from src.bot.middlewares.services import CONTAINER_KEY, SERVICES_KEY, Container
from src.logging import get_logger
from src.services.access import AccessContext

#: Handler-context key of the venue id — the number every repository of the update carries.
VENUE_ID_KEY: Final = "venue_id"

#: Handler-context key of the `Venue` row itself: the name and the IANA timezone every
#: screen needs to render a moment (TZ 3.4).
VENUE_KEY: Final = "venue"

logger = get_logger("bot.venue")

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class VenueContextMiddleware(BaseMiddleware):
    """Outer middleware of `Dispatcher.update`, after the gate."""

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        actor: AccessContext | None = data.get(ACTOR_KEY)
        if actor is None:
            return await handler(event, data)

        container: Container = data[CONTAINER_KEY]
        venue = await container.venues.get(actor.venue_id)
        if venue is None or not venue.is_active:
            # The membership survived its venue (deleted, or switched off between the
            # keyboard being drawn and the button being pressed). Nothing can be scoped to
            # it, so the update ends here rather than reaching a handler without a venue.
            logger.info("update refused: venue is gone", extra={"venue_id": actor.venue_id})
            await self._refuse(inner_event(event))
            return None

        data[VENUE_ID_KEY] = venue.id
        data[VENUE_KEY] = venue
        data[SERVICES_KEY] = container.for_venue(venue)
        return await handler(event, data)

    @staticmethod
    async def _refuse(target: TelegramObject) -> None:
        """The same refusal on both roads in, because the update ends the same way.

        A button press was already answered here — without it the client spins for a
        minute (TZ 9). A *message* was not, and silence is the worst answer of the three:
        the employee writes to the bot, gets nothing back and reads it as a broken bot
        rather than as a venue that was switched off. The wording is the neutral refusal of
        TZ 9: whether the venue exists at all is not this update's business.
        """
        if isinstance(target, CallbackQuery):
            await target.answer(texts.ERROR_NOT_ALLOWED, show_alert=True)
        elif isinstance(target, Message):
            await target.answer(texts.ERROR_NOT_ALLOWED)


def setup(dispatcher: Any) -> VenueContextMiddleware:
    """Register the venue context on updates, after the gate."""
    middleware = VenueContextMiddleware()
    dispatcher.update.outer_middleware(middleware)
    return middleware


__all__ = ["VENUE_ID_KEY", "VENUE_KEY", "VenueContextMiddleware", "setup"]
