"""Rate limiting per user, with two budgets that must not be one (TZ 9, plan task 20).

TZ 9 asks for rate limiting per user and says nothing about how much. The number comes
from the work: a bartender opening a venue ticks forty markers of a checklist in under a
minute, one tap per line (TZ 5.4). A single limiter sized for commands throttles exactly
that — and the bartender does not read "slow down", they read "the bot is broken", stop
using it, and the shift goes back to paper. So there are two budgets, chosen by what the
update *is*:

* :data:`COMMAND_RATE` — messages: commands and the text a wizard asks for. A person types
  one line at a time; a burst of five covers a fumbled `/start` and the flood that follows
  a forwarded message.
* :data:`TAP_RATE` — callback queries: five a second sustained, twenty in reserve, which is
  the plan's "~5 a second, burst 20". Forty markers over twenty seconds never touch it, and
  the bucket refills faster than a thumb moves.

Every callback query gets the tap budget, not only the checklist ones. A classifier that
names the screens is a classifier somebody forgets to extend, and the failure mode is the
one this file exists to prevent; a callback always comes from a keyboard this bot drew,
which is the honest reason it can be trusted with the larger budget.

**A token bucket, in memory, keyed by `telegram_id`.** In memory because the limit protects
this process (one polling process at stage 0), and because a Redis round trip on every
update to decide whether to do work is itself the work. `time.monotonic` is injectable so
the tests can play twenty seconds of tapping without sleeping.

A refusal always answers a callback query — an unanswered one spins on the client for a
minute, and TZ 9 wants an answer within a second. A refused *message* is answered at most
once per :data:`NOTICE_INTERVAL`: replying to every message of a flood is joining it.

**Both ledgers have a ceiling** (:class:`LruDict`). A limiter that keeps a bucket per
`telegram_id` seen, and a row per `telegram_id` ever refused, grows for as long as the
process lives — and it grows fastest under the flood it was written for, which is the one
moment the process cannot afford it. So each map holds :data:`MAX_TRACKED_USERS` entries
and forgets the least recently used one to make room, in O(1) per update: no scan, because
a scan on the hot path of every update costs more than the work it protects.
"""

from __future__ import annotations

import enum
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from src.bot import texts
from src.bot.middlewares.errors import inner_event
from src.logging import get_logger

logger = get_logger("bot.throttling")

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
Clock = Callable[[], float]


class LruDict[K, V]:
    """A mapping with a ceiling: past it, the least recently used entry is dropped.

    One hash lookup and one move inside an `OrderedDict` per operation — O(1), which is the
    property this file needs: the ledgers below are read on *every* update, and eviction by
    scanning for something forgettable would put a walk over ten thousand entries in front
    of the work the limiter exists to protect.

    Least recently used is also the right thing to forget here: the entry untouched longest
    is the bucket that has had longest to refill, hence the one closest to full, and a full
    bucket refuses nothing. Dropping it changes no decision that was going to be made.
    """

    __slots__ = ("_items", "_max_size")

    def __init__(self, max_size: int) -> None:
        if max_size <= 0:
            raise ValueError("a bounded map holds at least one entry")
        self._max_size = max_size
        self._items: OrderedDict[K, V] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: K) -> bool:
        return key in self._items

    def get(self, key: K) -> V | None:
        """The value, counted as a use; `None` when it is not tracked (or was evicted)."""
        value = self._items.get(key)
        if value is None:
            return None
        self._items.move_to_end(key)
        return value

    def put(self, key: K, value: V) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        if len(self._items) > self._max_size:
            self._items.popitem(last=False)


class Budget(enum.StrEnum):
    """Which allowance an update is charged against."""

    #: Commands and typed input: the start of a scenario, not its middle.
    COMMANDS = "commands"
    #: Button presses: the inside of a checklist, a pager, a wizard step.
    TAPS = "taps"


@dataclass(frozen=True, slots=True)
class Rate:
    """A token bucket's shape: how fast it refills and how much it holds."""

    per_second: float
    burst: int

    def __post_init__(self) -> None:
        if self.per_second <= 0 or self.burst <= 0:
            raise ValueError("a rate refills at a positive speed and holds at least one token")


#: Typed input. One a second is faster than anybody writes a checklist comment.
COMMAND_RATE: Final = Rate(per_second=1.0, burst=5)

#: Button presses (plan, task 20: "about 5 a second, burst 20").
TAP_RATE: Final = Rate(per_second=5.0, burst=20)

#: How often a throttled *message* is answered at all. Seconds.
NOTICE_INTERVAL: Final = 5.0

#: How many users each ledger remembers. Past it the least recently used one is dropped —
#: the entry that waited longest is the bucket closest to full, and a full bucket says
#: nothing, so forgetting it changes no decision. Ten thousand is far above a venue's whole
#: staff and small enough to bound the memory of a process that never restarts.
MAX_TRACKED_USERS: Final = 10_000


class TokenBucket:
    """One user's allowance under one budget."""

    __slots__ = ("_rate", "_tokens", "_updated_at")

    def __init__(self, rate: Rate, now: float) -> None:
        self._rate = rate
        self._tokens = float(rate.burst)
        self._updated_at = now

    def take(self, now: float) -> bool:
        """Spend one token; `False` when there is none, which is a refusal."""
        tokens = self._refill(now)
        if tokens < 1.0:
            self._tokens = tokens
            return False
        self._tokens = tokens - 1.0
        return True

    def _refill(self, now: float) -> float:
        elapsed = max(0.0, now - self._updated_at)
        self._updated_at = now
        return min(float(self._rate.burst), self._tokens + elapsed * self._rate.per_second)


class Limiter:
    """The buckets of every user, one set per budget."""

    def __init__(
        self,
        *,
        rates: dict[Budget, Rate] | None = None,
        clock: Clock = time.monotonic,
        max_tracked: int = MAX_TRACKED_USERS,
    ) -> None:
        self._rates = (
            dict(rates)
            if rates is not None
            else {
                Budget.COMMANDS: COMMAND_RATE,
                Budget.TAPS: TAP_RATE,
            }
        )
        self._clock = clock
        self._buckets: LruDict[tuple[Budget, int], TokenBucket] = LruDict(max_tracked)

    @property
    def tracked(self) -> int:
        """How many buckets are being kept; the ceiling made observable for the tests."""
        return len(self._buckets)

    def allow(self, budget: Budget, telegram_id: int) -> bool:
        now = self._clock()
        key = (budget, telegram_id)
        bucket = self._buckets.get(key)
        if bucket is None:
            # Either a user nobody has seen, or one whose bucket was evicted; both start
            # full, which is what an unknown user gets anyway.
            bucket = TokenBucket(self._rates[budget], now)
            self._buckets.put(key, bucket)
        return bucket.take(now)


def budget_for(event: TelegramObject) -> Budget:
    """Which allowance this update is charged against; see the module docstring."""
    return Budget.TAPS if isinstance(event, CallbackQuery) else Budget.COMMANDS


class ThrottlingMiddleware(BaseMiddleware):
    """Outer middleware of `Dispatcher.update`, before anything opens a session."""

    def __init__(
        self,
        *,
        limiter: Limiter | None = None,
        clock: Clock = time.monotonic,
        notice_interval: float = NOTICE_INTERVAL,
        max_tracked: int = MAX_TRACKED_USERS,
    ) -> None:
        # `max_tracked` has to reach the limiter too: capping only the notice ledger leaves
        # the buckets — the larger of the two dictionaries — growing without a ceiling, which
        # is exactly the leak this argument exists to close.
        self._limiter = (
            limiter if limiter is not None else Limiter(clock=clock, max_tracked=max_tracked)
        )
        self._clock = clock
        self._notice_interval = notice_interval
        self._noticed_at: LruDict[int, float] = LruDict(max_tracked)

    @property
    def noticed(self) -> int:
        """How many users the notice ledger remembers; the ceiling, made observable."""
        return len(self._noticed_at)

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        target = inner_event(event)
        user = getattr(target, "from_user", None)
        if user is None:
            return await handler(event, data)

        budget = budget_for(target)
        if self._limiter.allow(budget, user.id):
            return await handler(event, data)

        logger.info(
            "update throttled",
            extra={"telegram_id": user.id, "outcome_status": budget.value},
        )
        await self._notify(target, user.id)
        return None

    async def _notify(self, target: TelegramObject, telegram_id: int) -> None:
        if isinstance(target, CallbackQuery):
            # Always: the spinner is the user's only feedback, and it lasts a minute.
            await target.answer(texts.ERROR_TOO_FAST)
            return
        if not isinstance(target, Message):
            return
        now = self._clock()
        last = self._noticed_at.get(telegram_id)
        if last is not None and now - last < self._notice_interval:
            return
        self._noticed_at.put(telegram_id, now)
        await target.answer(texts.ERROR_TOO_FAST)


def setup(
    dispatcher: Any,
    *,
    limiter: Limiter | None = None,
    clock: Clock = time.monotonic,
) -> ThrottlingMiddleware:
    """Register the limiter on updates; it is registered before the session is opened."""
    middleware = ThrottlingMiddleware(limiter=limiter, clock=clock)
    dispatcher.update.outer_middleware(middleware)
    return middleware


__all__ = [
    "COMMAND_RATE",
    "MAX_TRACKED_USERS",
    "NOTICE_INTERVAL",
    "TAP_RATE",
    "Budget",
    "Clock",
    "Limiter",
    "LruDict",
    "Rate",
    "ThrottlingMiddleware",
    "TokenBucket",
    "budget_for",
    "setup",
]
