"""The delivery loop: the one process that talks to Telegram on its own (plan, task 31).

TZ 1.4#2 — "the bot comes to you" — is this file. Everything else writes rows into
``notifications`` (`src/services/notifications.py` sends nothing at all, on purpose); this
loop takes them out and delivers them, and acceptance criterion 11.4 is about what happens
when that goes wrong halfway.

**Claim, then deliver, in two transactions.** A pass over one venue commits the claim first
(``reclaim_stale`` + ``claim_batch``), and only then delivers, each row in a transaction of
its own. Both halves matter:

* the claim has to be *visible* before the network call, or a second worker picks the same
  row up while the first is still talking to Telegram. ``SKIP LOCKED`` inside
  ``claim_batch`` splits the queue between workers; committing is what makes the split hold
  for longer than the statement;
* one transaction per notification, never one per batch. A row that fails must not roll
  back the rows already delivered next to it — the delivery happened, the ``sent`` stamp
  has to survive; and a renderer that writes (the opening checklist *creates* the run) must
  have its write concluded together with the delivery it belongs to, or a run exists for a
  message nobody got.

**The claim token is carried, never rebuilt.** ``claim_batch`` stamps ``claimed_at`` and
hands the rows back carrying it; ``mark_sent`` / ``mark_failed`` match on it. So the worker
passes ``notification.claimed_at`` back exactly as it arrived: a row this worker no longer
owns — because it was slow and :meth:`reclaim_stale` handed the claim on — concludes
nothing, and the row goes out under whoever holds it now. Once. The repository docstring in
`src/db/repositories/notifications.py` is the long form of why nothing weaker works.

**What each failure does** (plan, task 31):

============================== ===============================================================
``TelegramForbiddenError``     ``failed``, and the user is marked as having blocked the bot.
``TelegramRetryAfter``         back to ``scheduled`` at ``now + retry_after``. Not a failure:
                               Telegram asked for a pause, so the attempt is not counted
                               either.
unknown type / no renderer     ``failed`` immediately. Retrying cannot help: nothing in this
                               build can turn the row into a message. **The loop does not
                               die** — that is the requirement, not a nicety: one row written
                               by a newer build would otherwise stop every other venue's
                               notifications.
anything else                  the claim is left where it is. The row stays ``sending`` and
                               :meth:`reclaim_stale` returns it to the queue with
                               ``attempts + 1`` after ``stale_after``; when ``attempts``
                               reaches ``max_attempts`` the next claim concludes it
                               ``failed``.
============================== ===============================================================

That last line is a deliberate use of the frozen repository contract rather than a gap in
it. ``attempts`` counts *concluded* attempts and is incremented in exactly two places —
``mark_failed`` (terminal) and ``reclaim_stale``. So a transient failure that wanted both
"count this" and "try again later" has one honest spelling: let the claim go stale, which
is also what a crashed worker leaves behind. The retry is slower than an immediate
reschedule by the width of ``stale_after``, and in exchange a lost process and a failed send
are the same story, told once.

**The worker is venue-agnostic, the repositories are not.** ``NotificationRepo`` is scoped
to one venue like every other repository (TZ 3.3): ``claim_batch`` carries
``AND venue_id = :venue_id``, and there is no cross-venue read anywhere in the data layer.
Handing the worker a repository without a venue would be the one convenient way to break
the isolation acceptance 11.3 is about, so it walks instead: ``VenueRepository.list_active``
(declared for this caller) gives the switched-on venues, and each gets its own claim, its
own transaction and its own services. The cost is one round trip per venue per minute; the
alternative costs the multitenancy.

**Time comes from ``utc_now()``** (decision D12), so a test pins the clock instead of
sleeping. One reading per pass, which is also what keeps the claim token unique: two claims
of the same row can never carry the same instant unless a caller hands ``claim_batch`` the
same ``now`` twice.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

from src.db.models import Notification
from src.db.repositories.protocols import NotificationRepository, UserRepository
from src.logging import get_logger
from src.services.notifications import (
    NotificationRegistry,
    OutgoingMessage,
    RendererNotRegisteredError,
    UnknownNotificationTypeError,
)
from src.services.timezones import utc_now

logger = get_logger("scheduler.worker")

#: How often the queue is looked at. TZ 6 schedules to the minute, so a finer tick buys
#: nothing and a coarser one makes "ten minutes before the shift" a lie.
DEFAULT_INTERVAL: Final = dt.timedelta(minutes=1)

#: How many rows one venue may claim per pass. A cap rather than a target: it bounds how
#: much work a single transaction can lose, and how long one venue can hold the loop.
DEFAULT_BATCH_LIMIT: Final = 100

#: How long a row may stay `sending` before the queue assumes the worker holding it is gone
#: (plan, task 31). Longer than any single send, shorter than a shift.
DEFAULT_STALE_AFTER: Final = dt.timedelta(minutes=5)

#: Concluded attempts after which a row is given up on. The count is the repository's, and
#: it grows on a failure and on an abandoned claim alike.
DEFAULT_MAX_ATTEMPTS: Final = 5

#: The reason written on a row that ran out of attempts. English, like every other value
#: that never reaches a person: `notifications.error` is read in a log, not in a chat.
ATTEMPTS_EXHAUSTED: Final = "delivery gave up after the maximum number of attempts"


# --------------------------------------------------------------------------------------
# What the loop is given
# --------------------------------------------------------------------------------------


@runtime_checkable
class RecordsDelivery(Protocol):
    """A rendered message that has something to do with the id Telegram gave it.

    The checklist message is the run (decision D11), so ``checklist_runs.message_id`` has to
    be written back — and the id exists only after the send, which the renderer does not do.
    The worker therefore asks the *message*, structurally, instead of asking what type the
    row was: the branch would have to grow by one line per type of stage 1, which is exactly
    what the renderer registry exists to prevent (`src/bot/renderers.py`).
    """

    async def record_delivery(self, chat_id: int, message_id: int, /) -> None: ...


class VenueDirectory(Protocol):
    """Which venues the worker walks (TZ 3.3: there is no cross-venue queue read)."""

    async def active_venue_ids(self) -> Sequence[int]: ...


class VenueQueue(Protocol):
    """One venue's queue and its renderers, over one transaction.

    The registry is part of it and not a constructor argument of the worker, because the
    renderer of the opening checklist writes: it creates the run inside the same unit of
    work the delivery is concluded in (`src/bot/renderers.py`).
    """

    @property
    def notifications(self) -> NotificationRepository: ...

    @property
    def users(self) -> UserRepository: ...

    @property
    def registry(self) -> NotificationRegistry: ...


class Workspace(Protocol):
    """Opens a transaction over one venue. `src/scheduler/__main__.py` is the real one."""

    def __call__(self, venue_id: int, /) -> AbstractAsyncContextManager[VenueQueue]: ...


@dataclass(slots=True)
class PassReport:
    """What one pass did, for the log line and for the tests of criterion 11.4."""

    reclaimed: int = 0
    claimed: int = 0
    sent: int = 0
    #: Rendered to nothing and concluded: an empty template has no message (TZ 8.1).
    empty: int = 0
    failed: int = 0
    #: Told to wait by Telegram and put back into the queue.
    rescheduled: int = 0
    #: Left claimed on purpose: a transient failure, retried when the claim goes stale.
    deferred: int = 0
    #: Passes over a venue that raised before reaching a row.
    errors: int = 0

    @property
    def delivered(self) -> int:
        return self.sent


# --------------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------------


class NotificationWorker:
    """Claims due rows venue by venue and delivers them, one transaction each."""

    __slots__ = (
        "_batch_limit",
        "_bot",
        "_interval",
        "_max_attempts",
        "_stale_after",
        "_venues",
        "_workspace",
    )

    def __init__(
        self,
        *,
        bot: Bot,
        venues: VenueDirectory,
        workspace: Workspace,
        interval: dt.timedelta = DEFAULT_INTERVAL,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        stale_after: dt.timedelta = DEFAULT_STALE_AFTER,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._bot = bot
        self._venues = venues
        self._workspace = workspace
        self._interval = interval
        self._batch_limit = batch_limit
        self._stale_after = stale_after
        self._max_attempts = max_attempts

    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        """Tick until asked to stop; a failed pass is logged, never fatal.

        The wait is on the stop event rather than on ``sleep``, so SIGTERM ends the process
        at once instead of after the rest of the minute (`src/scheduler/__main__.py`).
        """
        stopping = stop if stop is not None else asyncio.Event()
        while not stopping.is_set():
            try:
                report = await self.run_once()
            except Exception:
                # The loop outlives everything: a queue that stops being read is a bot that
                # stops coming on its own, and nothing in the product notices (TZ 1.4#2).
                logger.exception("notification pass failed")
            else:
                _log_pass(report)
            with suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=self._interval.total_seconds())

    async def run_once(self) -> PassReport:
        """One pass over every switched-on venue. Returns what it did."""
        now = utc_now()
        report = PassReport()
        for venue_id in await self._venues.active_venue_ids():
            try:
                await self._venue_pass(venue_id, now, report)
            except Exception:
                # One venue's database trouble is not the other venues' business.
                logger.exception("venue pass failed", extra={"venue_id": venue_id})
                report.errors += 1
        return report

    async def _venue_pass(self, venue_id: int, now: dt.datetime, report: PassReport) -> None:
        claimed = await self._claim(venue_id, now, report)
        for notification in claimed:
            try:
                await self._deliver(venue_id, notification, report)
            except Exception:
                # The row keeps its claim and is reclaimed later; the batch goes on. This is
                # the guard behind "an unknown type does not bring the worker down", and it
                # covers every other surprise a single row can hold as well.
                logger.exception(
                    "notification delivery failed",
                    extra={"venue_id": venue_id, "notification_id": notification.id},
                )
                report.deferred += 1

    async def _claim(
        self,
        venue_id: int,
        now: dt.datetime,
        report: PassReport,
    ) -> Sequence[Notification]:
        """Revive what was abandoned, then take what is due. One transaction, committed.

        Reviving first is what makes a crashed pass recoverable inside the next one instead
        of at the next restart: a row left ``sending`` by a process that died goes back into
        the queue and is claimed by the very same statement below.
        """
        async with self._workspace(venue_id) as queue:
            report.reclaimed += await queue.notifications.reclaim_stale(
                older_than=now - self._stale_after
            )
            claimed = await queue.notifications.claim_batch(now=now, limit=self._batch_limit)
            report.claimed += len(claimed)
            return claimed

    async def _deliver(
        self,
        venue_id: int,
        notification: Notification,
        report: PassReport,
    ) -> None:
        """One row: render, send, conclude. Nothing here is allowed to escape uncommitted."""
        async with self._workspace(venue_id) as queue:
            token = notification.claimed_at
            if notification.attempts >= self._max_attempts:
                await queue.notifications.mark_failed(
                    notification.id,
                    ATTEMPTS_EXHAUSTED,
                    claimed_at=token,
                )
                report.failed += 1
                return

            try:
                message = await queue.registry.render(notification)
            except (UnknownNotificationTypeError, RendererNotRegisteredError) as error:
                # Written by a build that knew more types, or delivered by one that was
                # wired incompletely. Neither is fixed by trying again.
                await queue.notifications.mark_failed(notification.id, str(error), claimed_at=token)
                report.failed += 1
                logger.warning(
                    "notification type cannot be rendered",
                    extra={
                        "venue_id": venue_id,
                        "notification_id": notification.id,
                        "notification_type": notification.type,
                        "error_type": type(error).__name__,
                    },
                )
                return

            if message is None:
                # "Nothing to deliver, mark the row sent" — the contract of
                # `NotificationRenderer`: an empty template produces no message (TZ 8.1).
                await queue.notifications.mark_sent(notification.id, utc_now(), claimed_at=token)
                report.empty += 1
                return

            try:
                sent = await self._send(message)
            except TelegramRetryAfter as error:
                await queue.notifications.reschedule(
                    notification.id,
                    utc_now() + dt.timedelta(seconds=error.retry_after),
                    # The claim may already be gone: `reclaim_stale` could have returned
                    # this row and a second worker taken it while Telegram was answering.
                    claimed_at=token,
                )
                report.rescheduled += 1
                return
            except TelegramForbiddenError as error:
                await queue.notifications.mark_failed(notification.id, str(error), claimed_at=token)
                if notification.user_id is not None:
                    # TZ 5.8 shows this mark on the employee's card: a manager who sees
                    # "the bot is blocked" stops waiting for an answer that cannot come.
                    await queue.users.set_bot_blocked(notification.user_id, blocked=True)
                report.failed += 1
                return

            await _record_delivery(message, sent)
            await queue.notifications.mark_sent(notification.id, utc_now(), claimed_at=token)
            report.sent += 1

    async def _send(self, message: OutgoingMessage) -> Message:
        """The only place in the project that hands a message to Telegram.

        ``markup`` is ``object`` on the service side (TZ 3.2 keeps aiogram out of the
        services), so the keyboard is recognised here rather than cast: a renderer that
        returns something else sends a message without buttons instead of a traceback.
        """
        markup = message.markup
        return await self._bot.send_message(
            chat_id=message.chat_id,
            text=message.text,
            reply_markup=markup if isinstance(markup, InlineKeyboardMarkup) else None,
        )


async def _record_delivery(message: OutgoingMessage, sent: Message) -> None:
    """Give a message that asked for it the id it was sent under (see :class:`RecordsDelivery`)."""
    if isinstance(message, RecordsDelivery):
        await message.record_delivery(sent.chat.id, sent.message_id)


def _log_pass(report: PassReport) -> None:
    """One line per pass, and only counters: TZ 9 keeps people out of the log."""
    if not (report.claimed or report.reclaimed or report.errors):
        return
    logger.info(
        "notification pass",
        extra={
            "claimed_count": report.claimed,
            "reclaimed_count": report.reclaimed,
            "sent_count": report.sent,
            "empty_count": report.empty,
            "failed_count": report.failed,
            "rescheduled_count": report.rescheduled,
            "deferred_count": report.deferred,
            "error_count": report.errors,
        },
    )


__all__ = [
    "ATTEMPTS_EXHAUSTED",
    "DEFAULT_BATCH_LIMIT",
    "DEFAULT_INTERVAL",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_STALE_AFTER",
    "NotificationWorker",
    "PassReport",
    "RecordsDelivery",
    "VenueDirectory",
    "VenueQueue",
    "Workspace",
]
