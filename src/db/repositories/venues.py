"""Venues and their settings (plan, task 11; TZ 4.1, 3.3).

Two repositories that sit on opposite sides of the venue scope.

``venue_settings`` is an ordinary scoped table — one row per venue, ``venue_id`` is both the
primary key and the scope — so :class:`VenueSettingsRepo` is a plain
:class:`~src.db.repositories.base.BaseRepository` and every query goes through
``for_venue()``.

``venues`` is not scoped at all: it *is* the scope. The table owns no ``venue_id`` column to
filter on, and the two questions asked of it — "which venue is this id" and "where does this
person work" — are answered *before* a venue context exists, because the venue-context
middleware needs both to build one (TZ 5.1). Rule 3 of ``tests/test_static_repos.py``
therefore has nothing to check here, and the exemption is registered there by function name,
in the same form the guard's docstring prescribes for ``users``. The hole is kept one
function wide: :meth:`VenueRepo._fetch` is the only place in this module that builds or runs
a statement, so a second unscoped query cannot appear without its own line in the guard.

Nothing here decides who may see a venue. :meth:`VenueRepo.list_for_user` returns every venue
the person holds a membership row in, inactive memberships and inactive venues included;
whether such a membership still grants access is :mod:`src.services.access`'s judgement
(TZ 3.2), and it filters exactly that way.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import ColumnElement, Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Venue, VenueMember, VenueSettings
from src.db.repositories.base import BaseRepository


def _apply(row: object, fields: Mapping[str, Any]) -> None:
    """Write `fields` onto an ORM row; an unknown column is a typo, not a silent no-op.

    Plain ``setattr`` would happily create an instance attribute for ``update(id, hour=5)``
    and drop it on the next flush, leaving the caller convinced the write happened.
    """
    for name, value in fields.items():
        if not hasattr(type(row), name):
            raise AttributeError(f"{type(row).__name__} has no column {name!r}")
        setattr(row, name, value)


class VenueRepo:
    """`venues` — the scope itself, so this repository is addressed by venue id.

    It deliberately does not extend :class:`BaseRepository`: that class derives the venue
    filter from ``model.venue_id``, and this model has no such column. Building it on the
    base would mean overriding ``venue_column``, which the base seals for exactly the reason
    this class exists (see ``SEALED_SCOPE_ATTRIBUTES``).
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, venue_id: int) -> Venue | None:
        found = await self._fetch(Venue.id == venue_id)
        return found[0] if found else None

    async def list_for_user(self, user_id: int) -> Sequence[Venue]:
        """Every venue this person holds a membership row in (TZ 5.1: the venue picker)."""
        return await self._fetch(VenueMember.user_id == user_id, through_membership=True)

    async def create(self, *, name: str, city: str, timezone: str) -> Venue:
        """A new venue. `timezone` is an IANA name and is validated by the service (TZ 3.4)."""
        venue = Venue(name=name, city=city, timezone=timezone)
        self.session.add(venue)
        await self.session.flush()
        return venue

    async def update(self, venue_id: int, **fields: Any) -> Venue | None:
        venue = await self.get(venue_id)
        if venue is None:
            return None
        _apply(venue, fields)
        await self.session.flush()
        return venue

    async def _fetch(
        self,
        condition: ColumnElement[bool],
        *,
        through_membership: bool = False,
    ) -> Sequence[Venue]:
        """The only statement of this module, and the reason for its registered exemption."""
        statement: Select[tuple[Venue]] = select(Venue)
        if through_membership:
            statement = statement.join(VenueMember, VenueMember.venue_id == Venue.id)
        # venue-exempt: `venues` is the scope, not a scoped table — it has no venue_id column
        # to compare against, and the venue-context middleware calls this before any scope
        # exists (TZ 3.3, 5.1).
        statement = statement.where(condition).order_by(Venue.name, Venue.id)
        return (await self.session.scalars(statement)).all()


class VenueSettingsRepo(BaseRepository[VenueSettings]):
    """`venue_settings` — one row per venue, so the scope alone identifies it."""

    model = VenueSettings

    async def get(self) -> VenueSettings | None:
        """The settings of the active venue, or `None` while the wizard has not run (TZ 8.1)."""
        return (await self.session.scalars(self.for_venue())).first()

    async def create(
        self,
        *,
        default_shift_start: dt.time,
        default_shift_end: dt.time,
        **fields: Any,
    ) -> VenueSettings:
        """The two required columns are named because TZ 4.2 has no default for them.

        Everything else keeps its schema default (lead times, thresholds, escalation), so a
        venue that never opens the settings screen still behaves the way answer A2 fixes.
        """
        settings = VenueSettings(
            venue_id=self.venue_id,
            default_shift_start=default_shift_start,
            default_shift_end=default_shift_end,
            **fields,
        )
        self.session.add(settings)
        await self.session.flush()
        return settings

    async def update(self, **fields: Any) -> VenueSettings | None:
        settings = await self.get()
        if settings is None:
            return None
        _apply(settings, fields)
        await self.session.flush()
        return settings


__all__ = ["VenueRepo", "VenueSettingsRepo"]
