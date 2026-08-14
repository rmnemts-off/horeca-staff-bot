"""Scheduler entry point: the second process, and the only one that delivers (task 31).

Nothing is decided here either (`src/bot/__main__.py` says the same about the bot): the
loop is `src/scheduler/worker.py`, the messages are `src/bot/renderers.py`, and this file
is the order in which they are assembled plus the shutdown that closes what it opened.

**Why a process of its own.** Acceptance criterion 11.4 asks for the scheduler to be
restarted on its own and for the notification to arrive exactly once anyway. If the loop
lived inside the bot, showing that would mean restarting long polling as well, and the two
failures — a dropped update and a duplicated notification — would be impossible to tell
apart. The bot never imports this module; this module imports the bot layer, because the
push the employee gets ten minutes before the shift *is* the checklist screen he taps on
(`src/bot/views/__init__.py`).

**The unit of work is a venue's transaction, and it is built per delivery.** Repositories
are venue-scoped by construction (TZ 3.3), so :func:`venue_queue` opens a session, builds
the same container the bot middleware builds, and hands the worker the queue, the users and
a registry whose renderers close over *those* services. The alternative — one registry for
the process — would mean renderers holding a session older than the transaction they write
in, and the opening-checklist renderer writes (it creates the run).

``bootstrap_owner_ids`` is empty here on purpose: it is the one-off trigger of the "create a
venue" wizard (decision A3) and has no meaning in a process that answers no update.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from src.bot.dispatcher import build_bot
from src.bot.middlewares.services import Container
from src.bot.renderers import RenderDeps, stage_zero_registry
from src.config import settings
from src.db.repositories.protocols import NotificationRepository, UserRepository
from src.db.repositories.venues import VenueRepo
from src.db.session import dispose_engine, session_scope
from src.logging import configure_logging, get_logger
from src.scheduler.worker import NotificationWorker, VenueQueue
from src.services.notifications import NotificationRegistry, UnknownVenueError
from src.services.overdue import OverdueService

logger = get_logger("scheduler")


@dataclass(frozen=True, slots=True)
class SessionQueue:
    """One venue's half of the queue, over the session that will commit the delivery."""

    notifications: NotificationRepository
    users: UserRepository
    overdue: OverdueService
    registry: NotificationRegistry


class ActiveVenues:
    """The venues the worker walks, read in a transaction of its own (TZ 3.3).

    ``list_active`` exists for this caller and for no other: the worker has no membership to
    derive a scope from, so it asks which venues are switched on and then claims inside each
    one. What it must never do is claim across all of them at once.
    """

    async def active_venue_ids(self) -> Sequence[int]:
        async with session_scope() as session:
            venues = await VenueRepo(session).list_active()
            return [venue.id for venue in venues]


@asynccontextmanager
async def venue_queue(venue_id: int) -> AsyncIterator[VenueQueue]:
    """One transaction over one venue: the queue, the users, and the renderers bound to it.

    The session commits when the block ends and rolls back when it raises
    (``session_scope``), which is what makes "one transaction per notification" true for the
    run the opening-checklist renderer creates as well as for the ``sent`` stamp.
    """
    async with session_scope() as session:
        container = Container(session, bootstrap_owner_ids=frozenset())
        venue = await container.venues.get(venue_id)
        if venue is None:
            # The venue was switched off or deleted between the listing and this call.
            raise UnknownVenueError(venue_id)
        services = container.for_venue(venue)
        yield SessionQueue(
            notifications=services.repositories.notifications,
            users=services.repositories.users,
            overdue=services.overdue,
            registry=stage_zero_registry(
                RenderDeps(
                    checklists=services.checklists,
                    people=services.repositories.users,
                )
            ),
        )


def install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM and SIGINT stop the loop between passes, not in the middle of a delivery.

    Setting the event rather than cancelling is the whole of the graceful shutdown: a pass
    already under way finishes its transaction, so no row is left claimed by a process that
    is no longer there — and even if one were, ``reclaim_stale`` would return it.
    """
    loop = asyncio.get_running_loop()
    for number in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            # Windows has no signal handlers on the event loop; the container runs Linux.
            loop.add_signal_handler(number, stop.set)


async def run() -> None:
    """Deliver until the process is asked to stop."""
    settings.require("bot_token", "database_url")

    bot = build_bot(settings.bot_token)
    worker = NotificationWorker(
        bot=bot,
        venues=ActiveVenues(),
        workspace=venue_queue,
    )
    stop = asyncio.Event()
    install_signal_handlers(stop)

    logger.info("scheduler process started", extra={"app_env": settings.app_env})
    try:
        await worker.run_forever(stop=stop)
    finally:
        await bot.session.close()
        await dispose_engine()
        logger.info("scheduler process stopped")


async def main() -> None:
    configure_logging()
    await run()


if __name__ == "__main__":
    asyncio.run(main())
