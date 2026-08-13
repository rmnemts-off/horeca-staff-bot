"""Aiogram middlewares: errors, rate limit, session, authorisation, venue context (TZ 3.2).

The order below is the whole design of this package, and every step of it is there because
the next one cannot work without it:

1. `errors` — outermost, so that everything after it (a refusal included) happens inside
   one `request_id` and one `try` (TZ 8.2, 9);
2. `throttling` — before anything opens a database session: a flood has to be cheap to
   refuse, or the limiter costs what it saves (TZ 9);
3. `services` — opens the session and the transaction of this update, hands out
   `AccessService`, which is the only service that exists before a venue does;
4. `auth` — needs `AccessService`; an unknown `telegram_id` stops here (TZ 5.1);
5. `venue` — needs the actor; builds every repository of the update around one `venue_id`
   (TZ 3.3);
6. `activity` — needs the identity; stamps `users.last_seen_at`;
7. `menu` — needs the FSM context, and has to be **before every router**: a main-menu press
   ends the scenario it interrupts, and a router cannot do that (TZ 5.2, and the module
   docstring of `menu.py` for why);
8. `resolver` — on callback queries only, and after all of the above, because fetching the
   object a button names is a query through the venue's repositories (TZ 9).

**No business logic lives here** (TZ 3.2). A middleware decides who is asking, where they
work and whether they may ask this often; what the answer *is* belongs to `src/services/`.
The two places this comes close to the line are deliberate and thin: the gate calls
`parse_invite_code()` from the access service rather than deciding for itself what a code
looks like, and the resolver calls the permission guards of the same module rather than
comparing roles on its own.
"""

from __future__ import annotations

import time
from typing import Any

from src.bot.middlewares import (
    activity,
    auth,
    errors,
    menu,
    resolver,
    services,
    throttling,
    venue,
)
from src.bot.middlewares.activity import LastSeenMiddleware
from src.bot.middlewares.auth import ACTOR_KEY, IDENTITY_KEY, INVITE_CODE_KEY, AuthMiddleware
from src.bot.middlewares.errors import ErrorsMiddleware
from src.bot.middlewares.menu import MenuInterceptMiddleware
from src.bot.middlewares.resolver import PAYLOAD_KEY, SUBJECT_KEY, CallbackResolverMiddleware
from src.bot.middlewares.services import (
    ACCESS_KEY,
    CONTAINER_KEY,
    SERVICES_KEY,
    ServicesMiddleware,
    Sessions,
)
from src.bot.middlewares.throttling import Limiter, ThrottlingMiddleware
from src.bot.middlewares.venue import VENUE_ID_KEY, VENUE_KEY, VenueContextMiddleware
from src.db.session import session_scope


def setup(
    dispatcher: Any,
    *,
    sessions: Sessions = session_scope,
    bootstrap_owner_ids: frozenset[int] = frozenset(),
    limiter: Limiter | None = None,
    clock: throttling.Clock = time.monotonic,
) -> None:
    """Register every middleware of the bot, in the order of the table above."""
    errors.setup(dispatcher)
    throttling.setup(dispatcher, limiter=limiter, clock=clock)
    services.setup(dispatcher, sessions=sessions, bootstrap_owner_ids=bootstrap_owner_ids)
    auth.setup(dispatcher)
    venue.setup(dispatcher)
    activity.setup(dispatcher)
    menu.setup(dispatcher)
    resolver.setup(dispatcher)


__all__ = [
    "ACCESS_KEY",
    "ACTOR_KEY",
    "CONTAINER_KEY",
    "IDENTITY_KEY",
    "INVITE_CODE_KEY",
    "PAYLOAD_KEY",
    "SERVICES_KEY",
    "SUBJECT_KEY",
    "VENUE_ID_KEY",
    "VENUE_KEY",
    "AuthMiddleware",
    "CallbackResolverMiddleware",
    "ErrorsMiddleware",
    "LastSeenMiddleware",
    "MenuInterceptMiddleware",
    "ServicesMiddleware",
    "ThrottlingMiddleware",
    "VenueContextMiddleware",
    "setup",
]
