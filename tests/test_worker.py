"""The delivery loop (plan tasks 31 and 32; acceptance criterion 11.4, test 38).

Criterion 11.4 is one sentence — neither a lost nor a duplicated notification — and every
test below is a way of losing or duplicating one:

* a process that dies **after** claiming a row and before sending it. The row is
  ``sending`` and nobody is holding it; if the next worker leaves it alone the notification
  is lost, and if it sends without reclaiming first the employee gets it twice;
* a row that has been sitting in ``sending`` since some earlier life of the service;
* an employee who has blocked the bot, in the middle of a batch of other people's messages;
* Telegram asking for a pause, which is not a failure and must not consume the row;
* a row whose ``type`` this build has never heard of — the one case that is *not* about the
  row at all: a worker that dies on it stops delivering for every venue in the
  installation, so the requirement is that the loop survives (plan, task 31).

**No database and no Telegram.** The queue is a fake repository that reproduces
``NotificationRepo`` statement by statement — the claim token above all, because
``mark_sent`` / ``mark_failed`` matching on ``claimed_at`` is the whole of "a claim is
concluded only by the worker that holds it", and a fake that ignored it would make the
crash test green about a race production still has (CLAUDE.md: at a divergence the fake is
fixed, not the test). The bot is ``RecordingSession`` from ``tests/bot/test_middlewares.py``,
which answers locally and writes down what it was asked to send, so "exactly once" is a
length assertion over a list.

The clock is pinned with ``use_clock(FixedClock(...))`` and moved forward by hand, which is
also what keeps the claim token honest: two claims of one row carry two different instants
because two passes read the clock twice, exactly as a running worker does.

The last two tests go through the real renderers of ``src/bot/renderers.py`` with the real
``ChecklistService`` (over the fakes of ``tests/services/test_checklists.py``), because
task 32 has a half nothing else covers: the push *creates* the run and has to write the id
of the message it arrived in back onto it, and an empty template has to deliver nothing at
all while still concluding the row (TZ 8.1, decision B1).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from src.bot import texts
from src.bot.renderers import RenderDeps, stage_zero_registry
from src.db.models import ChecklistType, Notification, NotificationStatus, User
from src.db.models.service import ACTIVE_NOTIFICATION_STATUSES
from src.scheduler.worker import (
    ATTEMPTS_EXHAUSTED,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_STALE_AFTER,
    NotificationWorker,
    VenueQueue,
)
from src.services.notifications import (
    NotificationRegistry,
    NotificationType,
    OutgoingMessage,
    dedup_key,
)
from src.services.timezones import FixedClock, use_clock

from tests.bot.test_middlewares import CHAT_ID, make_bot, session_of
from tests.services.test_checklists import Harness, make_item, make_template

VENUE_ID: Final = 1
#: The bar next door: its rows live in the same table and must be claimed by nobody else.
OTHER_VENUE_ID: Final = 2
USER_ID: Final = 7
SHIFT_ID: Final = 42
TELEGRAM_ID: Final = CHAT_ID

NOW: Final = dt.datetime(2026, 8, 13, 5, 50, tzinfo=dt.UTC)
#: Long enough that a claim taken at `NOW` is stale: what a restart looks like to the queue.
AFTER_THE_CRASH: Final = DEFAULT_STALE_AFTER + dt.timedelta(minutes=1)

MESSAGE_TEXT: Final = "queued message"


class WorkerCrashError(RuntimeError):
    """The process dying between the claim and the send, as a test can stage it."""


# --------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------


def make_notification(
    notification_id: int,
    *,
    notification_type: str = NotificationType.RECIPE_MISSING,
    venue_id: int = VENUE_ID,
    status: NotificationStatus = NotificationStatus.SCHEDULED,
    scheduled_at: dt.datetime = NOW,
    claimed_at: dt.datetime | None = None,
    attempts: int = 0,
    user_id: int | None = USER_ID,
    chat_id: int | None = TELEGRAM_ID,
    payload: Mapping[str, Any] | None = None,
) -> Notification:
    return Notification(
        id=notification_id,
        venue_id=venue_id,
        user_id=user_id,
        chat_id=chat_id,
        type=str(notification_type),
        payload=dict(payload or {}),
        scheduled_at=scheduled_at,
        status=status,
        dedup_key=dedup_key(venue_id, str(notification_type), notification_id),
        attempts=attempts,
        claimed_at=claimed_at,
        entity=None,
        entity_id=None,
        sent_at=None,
        error=None,
    )


# --------------------------------------------------------------------------------------
# The queue, as `NotificationRepo` behaves
# --------------------------------------------------------------------------------------


class FakeNotifications:
    """`NotificationRepository`, scoped to one venue over the shared `notifications` table.

    Everything the worker touches is reproduced from the real statements:

    * ``claim_batch`` takes only ``scheduled`` rows that are due, **of this venue**, and
      stamps the claim instant into ``claimed_at``;
    * ``reclaim_stale`` takes only ``sending`` rows whose claim is older than the window,
      clears the token and counts the abandoned attempt;
    * ``mark_sent`` and ``mark_failed`` match on status *and* token, so a report from a
      worker whose claim was handed on changes nothing at all;
    * ``reschedule`` leaves ``sent`` and ``cancelled`` rows alone, refuses a row whose
      claim has been handed on, and clears the token on the one it does move.
    """

    def __init__(self, venue_id: int = VENUE_ID, store: list[Notification] | None = None) -> None:
        self.venue_id = venue_id
        self.rows: list[Notification] = store if store is not None else []

    # -- what the worker uses -----------------------------------------------------------

    async def claim_batch(self, *, now: dt.datetime, limit: int) -> Sequence[Notification]:
        if limit <= 0:
            return []
        due = sorted(
            (
                row
                for row in self.rows
                if row.venue_id == self.venue_id
                and row.status is NotificationStatus.SCHEDULED
                and row.scheduled_at <= now
            ),
            key=lambda row: (row.scheduled_at, row.id),
        )[:limit]
        for row in due:
            row.status = NotificationStatus.SENDING
            row.claimed_at = now
        return due

    async def reclaim_stale(self, *, older_than: dt.datetime) -> int:
        reclaimed = 0
        for row in self.rows:
            if row.venue_id != self.venue_id or row.status is not NotificationStatus.SENDING:
                continue
            if row.claimed_at is not None and row.claimed_at > older_than:
                continue
            row.status = NotificationStatus.SCHEDULED
            row.claimed_at = None
            row.attempts += 1
            reclaimed += 1
        return reclaimed

    async def mark_sent(
        self,
        notification_id: int,
        moment: dt.datetime,
        *,
        claimed_at: dt.datetime | None,
    ) -> None:
        row = self._claimed(notification_id, claimed_at)
        if row is None:
            return
        row.status = NotificationStatus.SENT
        row.sent_at = moment
        row.claimed_at = None
        row.error = None

    async def mark_failed(
        self,
        notification_id: int,
        error: str,
        *,
        claimed_at: dt.datetime | None,
    ) -> None:
        row = self._claimed(notification_id, claimed_at)
        if row is None:
            return
        row.status = NotificationStatus.FAILED
        row.error = error
        row.claimed_at = None
        row.attempts += 1

    async def reschedule(
        self,
        notification_id: int,
        scheduled_at: dt.datetime,
        *,
        claimed_at: dt.datetime | None = None,
    ) -> None:
        """The real predicate, token included (`src/db/repositories/notifications.py`).

        A fake that ignored the token would make the worker's `TelegramRetryAfter` branch
        green against a repository that refuses the same call — which is the divergence
        CLAUDE.md is about.
        """
        row = self._scoped(notification_id)
        if row is None or row.status in {NotificationStatus.SENT, NotificationStatus.CANCELLED}:
            return
        if (
            claimed_at is not None
            and row.status is NotificationStatus.SENDING
            and row.claimed_at != claimed_at
        ):
            # Somebody else holds this row now: the reclaim happened while we were sending.
            return
        row.status = NotificationStatus.SCHEDULED
        row.scheduled_at = scheduled_at
        row.claimed_at = None

    async def get(self, notification_id: int) -> Notification | None:
        return self._scoped(notification_id)

    # -- the scheduling half, which the worker never calls -------------------------------
    #
    # Declared because this class is handed to code typed `NotificationRepository`, so mypy
    # checks every signature against the protocol; the bodies stay unimplemented because a
    # fake that pretended to schedule would be one more thing able to disagree with
    # `NotificationRepo` in silence (`tests/services/test_notifications.py` owns that half).

    async def upsert_scheduled(
        self,
        *,
        dedup_key: str,
        notification_type: str,
        scheduled_at: dt.datetime,
        user_id: int | None,
        chat_id: int | None,
        payload: dict[str, Any],
        entity: str | None = None,
        entity_id: int | None = None,
    ) -> Notification:
        raise NotImplementedError

    async def cancel_for_entity(self, *, entity: str, entity_id: int) -> int:
        raise NotImplementedError

    async def list_for_entity(self, *, entity: str, entity_id: int) -> Sequence[Notification]:
        raise NotImplementedError

    # -- helpers -------------------------------------------------------------------------

    def _scoped(self, notification_id: int) -> Notification | None:
        return next(
            (
                row
                for row in self.rows
                if row.id == notification_id and row.venue_id == self.venue_id
            ),
            None,
        )

    def _claimed(
        self,
        notification_id: int,
        claimed_at: dt.datetime | None,
    ) -> Notification | None:
        """`WHERE id = ... AND venue_id = ... AND status = 'sending' AND claimed_at = :token`."""
        row = self._scoped(notification_id)
        if row is None or row.status is not NotificationStatus.SENDING:
            return None
        return row if row.claimed_at == claimed_at else None

    @property
    def live(self) -> list[Notification]:
        return [row for row in self.rows if row.status in ACTIVE_NOTIFICATION_STATUSES]


class FakeUsers:
    """`UserRepository`, of which the worker needs the "bot blocked" mark and a name."""

    def __init__(self, users: Sequence[User] = ()) -> None:
        self.users = {user.id: user for user in users}
        self.blocked: list[int] = []

    async def get(self, user_id: int) -> User | None:
        return self.users.get(user_id)

    async def set_bot_blocked(self, user_id: int, *, blocked: bool) -> None:
        self.blocked.append(user_id)
        user = self.users.get(user_id)
        if user is not None:
            user.is_bot_blocked = blocked

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        raise NotImplementedError

    async def create(
        self,
        *,
        telegram_id: int,
        full_name: str,
        username: str | None = None,
        phone: str | None = None,
    ) -> User:
        raise NotImplementedError

    async def update(self, user_id: int, **fields: Any) -> User | None:
        raise NotImplementedError

    async def touch_last_seen(self, user_id: int, moment: dt.datetime) -> None:
        raise NotImplementedError

    async def set_active_venue(self, user_id: int, venue_id: int | None) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------------------
# The unit of work
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakeQueue:
    """`VenueQueue`: one venue's queue, users and renderers, as one transaction offers them."""

    notifications: FakeNotifications
    users: FakeUsers
    registry: NotificationRegistry


@dataclass
class FakeWorkspace:
    """Opens a "transaction" per venue, and can die inside one.

    ``crash_at_open`` is how a process that stops between the claim and the send is staged:
    the claim is the first block a pass opens, the first delivery is the second, so
    ``crash_at_open=2`` leaves a committed claim behind and no message. That is precisely
    the state criterion 11.4 asks about.
    """

    queues: Mapping[int, FakeQueue]
    crash_at_open: int | None = None
    opened: int = field(default=0, init=False)

    def __call__(self, venue_id: int) -> Any:
        return self._open(venue_id)

    @asynccontextmanager
    async def _open(self, venue_id: int) -> AsyncIterator[VenueQueue]:
        self.opened += 1
        if self.crash_at_open is not None and self.opened >= self.crash_at_open:
            raise WorkerCrashError(f"the worker died while working on venue {venue_id}")
        yield self.queues[venue_id]


@dataclass(frozen=True, slots=True)
class FakeVenues:
    """`VenueDirectory`: the switched-on venues the worker walks (TZ 3.3)."""

    venue_ids: Sequence[int] = (VENUE_ID,)

    async def active_venue_ids(self) -> Sequence[int]:
        return self.venue_ids


#: Every type stage 0 declares, so the default registry below can render all of them.
ALL_TYPES: Final[tuple[str, ...]] = tuple(str(member) for member in NotificationType)


def static_registry(
    message: OutgoingMessage | None = None,
    *,
    types: Sequence[str] = ALL_TYPES,
) -> NotificationRegistry:
    """A registry whose renderers all answer the same thing, attached to `types` only.

    Leaving a type without a renderer is how "the wiring is incomplete" is staged, and the
    registry itself is the real one, so an unknown type raises what the worker catches
    rather than what this test decided it should raise.
    """
    registry = NotificationRegistry.stage_zero()

    async def render(notification: Notification, /) -> OutgoingMessage | None:
        return message

    for notification_type in types:
        registry.attach_renderer(notification_type, render)
    return registry


def build(
    rows: Sequence[Notification],
    *,
    registry: NotificationRegistry | None = None,
    users: Sequence[User] = (),
    venue_ids: Sequence[int] = (VENUE_ID,),
    crash_at_open: int | None = None,
    bot: Bot | None = None,
) -> tuple[NotificationWorker, FakeNotifications, FakeUsers, Bot]:
    """One worker over one shared `notifications` table, with a queue per venue."""
    store = list(rows)
    prepared = registry if registry is not None else static_registry(_message())
    people = FakeUsers(users)
    queues = {
        venue_id: FakeQueue(
            notifications=FakeNotifications(venue_id, store),
            users=people,
            registry=prepared,
        )
        for venue_id in venue_ids
    }
    telegram = bot if bot is not None else make_bot()
    worker = NotificationWorker(
        bot=telegram,
        venues=FakeVenues(venue_ids),
        workspace=FakeWorkspace(queues, crash_at_open=crash_at_open),
    )
    return worker, queues[VENUE_ID].notifications, people, telegram


def _message(chat_id: int = TELEGRAM_ID, text: str = MESSAGE_TEXT) -> OutgoingMessage:
    return OutgoingMessage(chat_id=chat_id, text=text)


def sent_count(bot: Bot) -> int:
    return len([call for call in session_of(bot).calls if isinstance(call, SendMessage)])


# --------------------------------------------------------------------------------------
# Criterion 11.4: neither lost nor duplicated
# --------------------------------------------------------------------------------------


async def test_a_crash_between_sending_and_sent_delivers_exactly_once() -> None:
    """Test 38 of the plan, in the order it happens.

    The first worker claims the row and dies before the send — the row is left ``sending``
    with a claim nobody holds. The second worker reclaims it, delivers it, and the message
    goes out **once**: once in total across both processes, which is the only reading of
    "exactly once" that is worth anything.
    """
    row = make_notification(1)
    clock = FixedClock(NOW)
    with use_clock(clock):
        crashing, queue, _, dead_bot = build([row], crash_at_open=2)
        report = await crashing.run_once()

        assert report.claimed == 1
        assert sent_count(dead_bot) == 0, "the process died before it could send"
        assert row.status is NotificationStatus.SENDING
        assert row.claimed_at == NOW

        # The restart: a new process over the same row, whose claim is now stale.
        assert queue.live == [row], "the row is neither delivered nor lost, it is claimed"
        clock.advance(AFTER_THE_CRASH)
        restarted, _, _, live_bot = build([row], bot=make_bot())

        recovery = await restarted.run_once()

        assert recovery.reclaimed == 1
        assert recovery.sent == 1
        assert sent_count(live_bot) == 1
        assert row.status is NotificationStatus.SENT
        assert row.attempts == 1, "the abandoned claim is counted, the delivery is not"

        # And nothing is delivered a second time on the next tick.
        clock.advance(AFTER_THE_CRASH)
        again = await restarted.run_once()

        assert again.claimed == 0
        assert sent_count(live_bot) == 1


async def test_a_row_left_in_sending_is_delivered_once() -> None:
    """A row abandoned by some earlier life of the service, found on an ordinary pass."""
    row = make_notification(
        1,
        status=NotificationStatus.SENDING,
        claimed_at=NOW - AFTER_THE_CRASH,
        attempts=0,
    )
    clock = FixedClock(NOW)
    with use_clock(clock):
        worker, _, _, bot = build([row])

        first = await worker.run_once()
        clock.advance(dt.timedelta(minutes=1))
        second = await worker.run_once()

    assert first.reclaimed == 1
    assert first.sent == 1
    assert second.sent == 0
    assert sent_count(bot) == 1
    assert row.status is NotificationStatus.SENT


async def test_a_claim_that_was_handed_on_concludes_nothing() -> None:
    """The token, from the queue's side: the fence the worker's `mark_sent` relies on.

    A worker that was merely slow still holds the value it was given. By the time it reports,
    the row has been reclaimed and taken by somebody else — and its report must change
    nothing, or a message is stamped delivered before it goes out (criterion 11.4).
    """
    row = make_notification(1)
    queue = FakeNotifications(VENUE_ID, [row])

    claimed = await queue.claim_batch(now=NOW, limit=10)
    lost_token = claimed[0].claimed_at
    assert await queue.reclaim_stale(older_than=NOW) == 1
    later = NOW + dt.timedelta(minutes=6)
    await queue.claim_batch(now=later, limit=10)

    await queue.mark_sent(row.id, later, claimed_at=lost_token)

    assert row.status is NotificationStatus.SENDING
    assert row.claimed_at == later


# --------------------------------------------------------------------------------------
# Failures that must not spread
# --------------------------------------------------------------------------------------


async def test_a_blocked_bot_fails_one_row_and_marks_the_person() -> None:
    """TZ 5.8 and section 6: the batch goes on, and the manager's list learns why."""
    blocked = make_notification(1)
    ordinary = make_notification(2, scheduled_at=NOW + dt.timedelta(seconds=1))
    bot = make_bot()
    session_of(bot).fail_with["SendMessage"] = TelegramForbiddenError(
        method=SendMessage(chat_id=TELEGRAM_ID, text=MESSAGE_TEXT),
        message="Forbidden: bot was blocked by the user",
    )
    with use_clock(FixedClock(NOW + dt.timedelta(seconds=1))):
        worker, _, people, _ = build([blocked, ordinary], bot=bot)
        report = await worker.run_once()

    assert report.claimed == 2
    assert report.failed == 1
    assert report.sent == 1
    assert blocked.status is NotificationStatus.FAILED
    assert people.blocked == [USER_ID]
    assert ordinary.status is NotificationStatus.SENT, "one refusal does not stop the batch"


async def test_retry_after_puts_the_row_back_instead_of_failing_it() -> None:
    """Telegram asked for a pause: the row waits, it is not given up on and not counted."""
    row = make_notification(1)
    bot = make_bot()
    session_of(bot).fail_with["SendMessage"] = TelegramRetryAfter(
        method=SendMessage(chat_id=TELEGRAM_ID, text=MESSAGE_TEXT),
        message="Too Many Requests",
        retry_after=30,
    )
    clock = FixedClock(NOW)
    with use_clock(clock):
        worker, _, _, _ = build([row], bot=bot)
        first = await worker.run_once()

        assert first.rescheduled == 1
        assert row.status is NotificationStatus.SCHEDULED
        assert row.scheduled_at == NOW + dt.timedelta(seconds=30)
        assert row.claimed_at is None
        assert row.attempts == 0, "a pause is not a failed attempt"

        clock.advance(dt.timedelta(seconds=31))
        second = await worker.run_once()

    assert second.sent == 1
    assert row.status is NotificationStatus.SENT
    assert sent_count(bot) == 2, "the first call is the one Telegram refused"


async def test_a_pause_does_not_strip_a_claim_that_now_belongs_to_somebody_else() -> None:
    """The other side of `mark_sent`'s token check, on the one branch that also writes.

    While Telegram was answering "too many requests", this row could have been reclaimed —
    `reclaim_stale` returns a row abandoned by a dead process — and taken by a second
    worker. Moving it then would strip that worker's claim in the middle of its delivery,
    and the row would go out twice (criterion 11.4).
    """
    row = make_notification(1)
    bot = make_bot()
    session_of(bot).fail_with["SendMessage"] = TelegramRetryAfter(
        method=SendMessage(chat_id=TELEGRAM_ID, text=MESSAGE_TEXT),
        message="Too Many Requests",
        retry_after=30,
    )
    with use_clock(FixedClock(NOW)):
        worker, notifications, _, _ = build([row], bot=bot)
        # Somebody else's claim, taken between our claim and Telegram's answer.
        stolen = NOW + dt.timedelta(seconds=5)

        async def steal(*_: object, **__: object) -> None:
            row.status = NotificationStatus.SENDING
            row.claimed_at = stolen

        original = notifications.reschedule

        async def reschedule_after_the_theft(*args: object, **kwargs: object) -> None:
            await steal()
            await original(*args, **kwargs)  # type: ignore[arg-type]

        notifications.reschedule = reschedule_after_the_theft  # type: ignore[method-assign]
        await worker.run_once()

    assert row.status is NotificationStatus.SENDING, "the other worker still holds it"
    assert row.claimed_at == stolen, "its claim is untouched"


async def test_an_unknown_type_fails_the_row_and_not_the_loop() -> None:
    """The requirement of task 31 spelled out: the row is concluded, the pass carries on.

    A row of a type this build never declared can only come from a newer build writing into
    the same queue. Dying on it would stop delivery for every venue in the installation, so
    it is `failed` and the batch continues.
    """
    unknown = make_notification(1, notification_type="shift_reminder")
    ordinary = make_notification(2, scheduled_at=NOW + dt.timedelta(seconds=1))
    with use_clock(FixedClock(NOW + dt.timedelta(seconds=1))):
        worker, _, _, bot = build([unknown, ordinary])
        report = await worker.run_once()

    assert report.failed == 1
    assert report.sent == 1
    assert unknown.status is NotificationStatus.FAILED
    assert unknown.error is not None and "shift_reminder" in unknown.error
    assert unknown.attempts == 1
    assert ordinary.status is NotificationStatus.SENT
    assert sent_count(bot) == 1


async def test_a_type_without_a_renderer_fails_the_row() -> None:
    """The neighbouring failure: the type is declared, the bot layer forgot to wire it."""
    row = make_notification(1, notification_type=NotificationType.CHECKLIST_OVERDUE)
    registry = static_registry(_message(), types=(NotificationType.RECIPE_MISSING,))
    with use_clock(FixedClock(NOW)):
        worker, _, _, bot = build([row], registry=registry)
        report = await worker.run_once()

    assert report.failed == 1
    assert row.status is NotificationStatus.FAILED
    assert sent_count(bot) == 0


async def test_a_row_that_ran_out_of_attempts_is_given_up_on() -> None:
    """`attempts` is the repository's count of concluded tries; the worker stops at N."""
    row = make_notification(1, attempts=DEFAULT_MAX_ATTEMPTS)
    with use_clock(FixedClock(NOW)):
        worker, _, _, bot = build([row])
        report = await worker.run_once()

    assert report.failed == 1
    assert row.status is NotificationStatus.FAILED
    assert row.error == ATTEMPTS_EXHAUSTED
    assert sent_count(bot) == 0


async def test_a_transient_failure_leaves_the_claim_to_go_stale() -> None:
    """Neither `failed` nor rescheduled: the row is retried the way a crash is retried.

    The queue counts a concluded attempt in two places only — `mark_failed`, which is
    terminal, and `reclaim_stale`. So the honest spelling of "count this and try again" is
    to leave the claim alone and let it go stale, which is also what a dead process leaves
    behind: one recovery path instead of two.
    """
    row = make_notification(1)
    bot = make_bot()
    session_of(bot).fail_with["SendMessage"] = RuntimeError("the network went away")
    clock = FixedClock(NOW)
    with use_clock(clock):
        worker, _, _, _ = build([row], bot=bot)
        first = await worker.run_once()

        assert first.deferred == 1
        assert row.status is NotificationStatus.SENDING
        assert row.attempts == 0

        clock.advance(AFTER_THE_CRASH)
        second = await worker.run_once()

    assert second.reclaimed == 1
    assert second.sent == 1
    assert row.attempts == 1
    assert row.status is NotificationStatus.SENT


# --------------------------------------------------------------------------------------
# One installation, many venues (TZ 3.3, acceptance 11.3)
# --------------------------------------------------------------------------------------


async def test_a_row_of_another_venue_is_not_claimed() -> None:
    """The worker walks the venues; it never claims across them.

    The neighbour's row is genuinely in the table — the same list the claim reads — and the
    only thing keeping it out of this delivery is the venue predicate the fake carries from
    `NotificationRepo`. Without this test that predicate is code no assertion depends on.
    """
    mine = make_notification(1)
    theirs = make_notification(2, venue_id=OTHER_VENUE_ID)
    with use_clock(FixedClock(NOW)):
        worker, _, _, bot = build([mine, theirs], venue_ids=(VENUE_ID,))
        report = await worker.run_once()

    assert report.claimed == 1
    assert mine.status is NotificationStatus.SENT
    assert theirs.status is NotificationStatus.SCHEDULED
    assert sent_count(bot) == 1


async def test_every_venue_is_walked() -> None:
    """Both venues deliver — each through its own queue, in its own transaction."""
    mine = make_notification(1)
    theirs = make_notification(2, venue_id=OTHER_VENUE_ID)
    with use_clock(FixedClock(NOW)):
        worker, _, _, bot = build([mine, theirs], venue_ids=(VENUE_ID, OTHER_VENUE_ID))
        report = await worker.run_once()

    assert report.sent == 2
    assert mine.status is NotificationStatus.SENT
    assert theirs.status is NotificationStatus.SENT
    assert sent_count(bot) == 2


# --------------------------------------------------------------------------------------
# The opening checklist, end to end (plan task 32, TZ 5.4, 8.1)
# --------------------------------------------------------------------------------------


def opening_row(notification_id: int = 1) -> Notification:
    return make_notification(
        notification_id,
        notification_type=NotificationType.OPENING_CHECKLIST,
        payload={
            "shift_id": SHIFT_ID,
            "user_id": USER_ID,
            "shift_date": "2026-08-13",
            "start_time": "10:00:00",
            "checklist_type": ChecklistType.OPENING.value,
        },
    )


def opening_worker(
    harness: Harness,
    rows: Sequence[Notification],
) -> tuple[NotificationWorker, Bot]:
    people = FakeUsers([User(id=USER_ID, telegram_id=TELEGRAM_ID, full_name="Anna Ivanova")])
    registry = stage_zero_registry(RenderDeps(checklists=harness.service, people=people))
    worker, _, _, bot = build(rows, registry=registry)
    return worker, bot


async def test_the_opening_checklist_creates_the_run_and_remembers_its_message() -> None:
    """Task 32: the push *is* the checklist, and the run has to know which message it is.

    Without the write-back the employee's next entry from «Моя смена» would edit a message
    the run does not know about — a second checklist under the first one (decision D11).
    """
    harness = Harness(
        templates=[make_template(1)],
        items=[make_item(10, 1, text="Проверить лёд")],
    )
    row = opening_row()
    with use_clock(FixedClock(NOW)):
        worker, bot = opening_worker(harness, [row])
        report = await worker.run_once()

    assert report.sent == 1
    assert row.status is NotificationStatus.SENT

    runs = list(harness.runs.runs.values())
    assert len(runs) == 1, "one shift, one run (TZ 5.4)"
    run = runs[0]
    assert run.shift_id == SHIFT_ID
    assert run.chat_id == CHAT_ID
    assert run.message_id is not None, "the id Telegram gave the message is written back"

    sent = [call for call in session_of(bot).calls if isinstance(call, SendMessage)]
    assert len(sent) == 1
    text = str(sent[0].text)
    assert texts.NOTIFY_OPENING_CHECKLIST_TEMPLATE.format(start="10:00") in text
    assert "Проверить лёд" in text, "the checklist itself, drawn by the same view as the screen"
    assert sent[0].reply_markup is not None, "the markers are pressable straight from the push"


async def test_an_empty_template_delivers_nothing_and_concludes_the_row() -> None:
    """TZ 8.1 and decision B1: no run, no message to the employee, and no row left hanging.

    The manager has already been told by the service, so retrying this row forever would
    only keep one line of the queue alive for a venue that has not entered its checklist yet.
    """
    harness = Harness(templates=[make_template(1)], items=[])
    row = opening_row()
    with use_clock(FixedClock(NOW)):
        worker, bot = opening_worker(harness, [row])
        report = await worker.run_once()

    assert report.empty == 1
    assert report.sent == 0
    assert sent_count(bot) == 0
    assert row.status is NotificationStatus.SENT, "concluded, not retried"
    assert harness.runs.runs == {}
    assert len(harness.notifier.empty) == 1, "the manager hears about it instead"


@pytest.mark.parametrize("scheduled_in", [dt.timedelta(minutes=1), dt.timedelta(hours=3)])
async def test_a_row_that_is_not_due_is_left_alone(scheduled_in: dt.timedelta) -> None:
    """The queue is read by the clock, so a push does not arrive before its moment."""
    row = make_notification(1, scheduled_at=NOW + scheduled_in)
    with use_clock(FixedClock(NOW)):
        worker, _, _, bot = build([row])
        report = await worker.run_once()

    assert report.claimed == 0
    assert row.status is NotificationStatus.SCHEDULED
    assert sent_count(bot) == 0
