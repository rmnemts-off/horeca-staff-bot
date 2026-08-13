"""Assembling the dispatcher: storage, middlewares, routers (plan, task 20).

**The FSM store is Redis, with a TTL** (TZ 3.1). Two properties come out of that, and both
are requirements rather than conveniences:

* a scenario **survives a restart of the bot**. A deploy in the middle of a shift must not
  drop the wizard a manager is halfway through, and `MemoryStorage` would (plan task 20
  lists it as a test: the state is written, the dispatcher is rebuilt, the state is still
  there);
* a scenario **does not survive forever**. An abandoned wizard is not a document; without a
  TTL its half-filled data waits in Redis until somebody notices, and the next `/start`
  from that person lands in a step they typed a fortnight ago. :data:`FSM_TTL` is a day —
  longer than the longest shift the customer runs (decision A2: 08:00-23:00), short enough
  that yesterday's abandoned form is gone.

Both the state and the data carry the TTL. Only one of them would be worse than neither:
the state key expiring while the data key stays leaves a scenario with no step, which is
the one combination no handler is written for.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from aiogram import Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage

from src.bot import middlewares, routers
from src.db.session import session_scope

#: How long an untouched FSM scenario lives; see the module docstring.
FSM_TTL: Final = dt.timedelta(days=1)

#: Prefix of every FSM key in Redis, so the store can be shared with a cache without the
#: two ever colliding (TZ 3.1 gives Redis both jobs).
FSM_KEY_PREFIX: Final = "barpoint:fsm"


def build_storage(redis_url: str, *, ttl: dt.timedelta = FSM_TTL) -> RedisStorage:
    """The FSM store: Redis, both keys expiring after `ttl` (TZ 3.1)."""
    return RedisStorage.from_url(
        redis_url,
        key_builder=DefaultKeyBuilder(prefix=FSM_KEY_PREFIX),
        state_ttl=ttl,
        data_ttl=ttl,
    )


def build_dispatcher(
    *,
    storage: BaseStorage,
    bootstrap_owner_ids: frozenset[int] = frozenset(),
    sessions: middlewares.Sessions = session_scope,
) -> Dispatcher:
    """The dispatcher of the bot process: storage, middlewares and routers.

    `bootstrap_owner_ids` is `OWNER_TELEGRAM_IDS` (decision A3) — a one-off trigger for the
    "create a venue" wizard, never a source of rights; it reaches
    :class:`~src.services.access.AccessService` and nothing else.
    """
    dispatcher = Dispatcher(storage=storage)
    middlewares.setup(
        dispatcher,
        sessions=sessions,
        bootstrap_owner_ids=bootstrap_owner_ids,
    )
    # The routers of TZ 5.1-5.8, in the order `src/bot/handlers/__init__.py` explains. The
    # main-menu press of TZ 5.2 is *not* among them: it is decided before routing starts,
    # by the middleware, and so cannot be outranked by an include in the wrong place.
    for router in routers.all_routers():
        dispatcher.include_router(router)
    return dispatcher


__all__ = [
    "FSM_KEY_PREFIX",
    "FSM_TTL",
    "build_dispatcher",
    "build_storage",
]
