"""`users.last_seen_at` — when the person last did anything (TZ 4.1, plan task 20).

The column exists so that a manager's roster can say who has actually opened the bot, and
so that "the bot is blocked" (TZ 5.8) can be told from "nobody has looked at it since
March". Writing it belongs to the framework rather than to a handler: every update is
activity, and a handler that has to remember to record it records it in eight places out of
nine.

**Not one `UPDATE` per tap.** A checklist is forty presses in a few minutes, and stamping a
row forty times to answer a question measured in days is forty write-locks for nothing. The
write happens when the stored value is older than :data:`LAST_SEEN_RESOLUTION`, which is
the resolution the answer is read at anyway.

The moment comes from `utc_now()` of `src/services/timezones.py` — the one clock of the
project (decision D12), which a test pins and a venue in another timezone converts from.
The column holds instants; the venue's wall clock is a rendering concern (TZ 3.4).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from src.bot.middlewares.auth import IDENTITY_KEY
from src.bot.middlewares.services import CONTAINER_KEY, Container
from src.services.access import Identity
from src.services.timezones import utc_now

#: How stale `last_seen_at` may get. Five minutes keeps "was here today" exact and turns a
#: whole shift of tapping into a handful of writes.
LAST_SEEN_RESOLUTION = dt.timedelta(minutes=5)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
Clock = Callable[[], dt.datetime]


def is_stale(last_seen_at: dt.datetime | None, now: dt.datetime, resolution: dt.timedelta) -> bool:
    """Whether the stored moment is old enough to be worth a write.

    A value that arrived without a timezone is read as UTC: the column is
    `TIMESTAMP WITH TIME ZONE` and everything written to it is UTC, so a naive value can
    only be one that lost its tzinfo on the way (a fixture, a driver without the type).
    """
    if last_seen_at is None:
        return True
    stored = last_seen_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=dt.UTC)
    return now - stored >= resolution


class LastSeenMiddleware(BaseMiddleware):
    """Outer middleware of `Dispatcher.update`, after the gate."""

    def __init__(
        self,
        *,
        clock: Clock = utc_now,
        resolution: dt.timedelta = LAST_SEEN_RESOLUTION,
    ) -> None:
        self._clock = clock
        self._resolution = resolution

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        identity: Identity | None = data.get(IDENTITY_KEY)
        user = identity.user if identity is not None else None
        if user is not None:
            now = self._clock()
            if is_stale(user.last_seen_at, now, self._resolution):
                container: Container = data[CONTAINER_KEY]
                await container.users.touch_last_seen(user.id, now)
                # The row in this session is the one the handler will render from; leaving
                # it stale would make a second update of the same minute write again.
                user.last_seen_at = now
        return await handler(event, data)


def setup(
    dispatcher: Any,
    *,
    clock: Clock = utc_now,
    resolution: dt.timedelta = LAST_SEEN_RESOLUTION,
) -> LastSeenMiddleware:
    """Register the activity stamp on updates, after the gate."""
    middleware = LastSeenMiddleware(clock=clock, resolution=resolution)
    dispatcher.update.outer_middleware(middleware)
    return middleware


__all__ = [
    "LAST_SEEN_RESOLUTION",
    "Clock",
    "LastSeenMiddleware",
    "is_stale",
    "setup",
    "utc_now",
]
