"""The global `users` table (plan, task 11; TZ 4.1, 5.1).

`users` is the one table of section 4 that has no `venue_id` and must not grow one: the same
person works in several venues (TZ 2), and their Telegram id, name and "bot blocked" mark
belong to the person rather than to any one venue. Everything venue-scoped about that person
lives in `venue_members`, which is an ordinary scoped table
(:mod:`src.db.repositories.members`).

That makes this module the documented hole in rule 3 of `tests/test_static_repos.py` — it
cannot filter on a column the table does not have. The hole is kept as small as the interface
allows: :meth:`UserRepo._fetch` is the only method here that builds or runs a statement, every
other one goes through it, and the exemption is registered in `ALLOWED_VENUE_EXEMPTIONS` so a
second one would show up in the diff of the guard itself.

Reading a person by id is therefore **not** an authorisation step, and nothing here pretends
otherwise. The caller has already resolved the membership inside its own venue — that scoped
lookup is what stops a manager of one venue touching a stranger (see
:mod:`src.services.members`) — and this repository only turns an id into a row.

Moments arrive as arguments, never from a clock: `last_seen_at` is written with the instant
the caller measured through :mod:`src.services.timezones` (D12).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import User


def _apply(row: object, fields: Mapping[str, Any]) -> None:
    """Write `fields` onto an ORM row; an unknown column is a typo, not a silent no-op."""
    for name, value in fields.items():
        if not hasattr(type(row), name):
            raise AttributeError(f"{type(row).__name__} has no column {name!r}")
        setattr(row, name, value)


class UserRepo:
    """`users` implementation: global, unscoped, and deliberately without judgement.

    Not a :class:`~src.db.repositories.base.BaseRepository`, and not because it would be
    inconvenient: that class derives its filter from ``model.venue_id``, and this model has
    none. A venue id passed to it would be a decoration on a query that cannot use it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: int) -> User | None:
        return await self._fetch(User.id == user_id)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """The entry point of every update: Telegram knows the person by this id alone."""
        return await self._fetch(User.telegram_id == telegram_id)

    async def create(
        self,
        *,
        telegram_id: int,
        full_name: str,
        username: str | None = None,
        phone: str | None = None,
    ) -> User:
        """A person, with no venue attached yet — membership is a separate row (TZ 5.1)."""
        user = User(
            telegram_id=telegram_id,
            full_name=full_name,
            username=username,
            phone=phone,
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def update(self, user_id: int, **fields: Any) -> User | None:
        user = await self._fetch(User.id == user_id)
        if user is None:
            return None
        _apply(user, fields)
        await self.session.flush()
        return user

    async def touch_last_seen(self, user_id: int, moment: dt.datetime) -> None:
        """`moment` is an aware UTC instant supplied by the caller — there is no clock here."""
        await self.update(user_id, last_seen_at=moment)

    async def set_active_venue(self, user_id: int, venue_id: int | None) -> None:
        """TZ 5.1: the venue a multi-venue employee picked outlives a restart and a TTL."""
        await self.update(user_id, active_venue_id=venue_id)

    async def set_bot_blocked(self, user_id: int, *, blocked: bool) -> None:
        """TZ 6: written by the notification worker when a delivery fails (TZ 5.8 renders it)."""
        await self.update(user_id, is_bot_blocked=blocked)

    async def _fetch(self, condition: ColumnElement[bool]) -> User | None:
        """The only statement of this module, and the reason for its registered exemption."""
        # venue-exempt: `users` is global by TZ 2 — one person, many venues — and carries no
        # venue_id column; the venue-scoped half of a person is `venue_members`.
        statement = select(User).where(condition)
        return (await self.session.scalars(statement)).first()


__all__ = ["UserRepo"]
