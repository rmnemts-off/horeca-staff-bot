"""The schedule — `shifts` (plan, task 12; TZ 4.2, 5.3).

`shifts` owns `venue_id`, so this is an ordinary
:class:`~src.db.repositories.base.BaseRepository`: every read starts from `for_venue()` or
`by_id()`, and a shift id from a forged `callback_data` resolves to `None` (TZ 9, acceptance
11.3).

**A night shift belongs to the date it starts on** (TZ 5.3), and that is a property of the
column, not of a query: `shift_date` *is* the date the shift opened, so `18:00-02:00` is one
row dated the evening it began and every filter below is a plain comparison on that column.
The two instants such a row spans are another matter entirely — they need the venue's
timezone and are computed by :func:`src.services.timezones.shift_window`, which is why
"the shift running right now" is not a method here: it cannot be expressed as a predicate on
`shift_date`. :meth:`ShiftRepo.list_for_user` over a window that starts one day early is what
the service turns into that answer (`ShiftService.current_shift`).

No invariant is checked here (TZ 3.2). "At most one opener and one closer per date" is
decision D8 and lives in :mod:`src.services.shifts`; `hours` is computed there too. This
module therefore offers :meth:`get_opener` and :meth:`get_closer` as plain lookups — they
report who currently holds the mark, and the service decides whether that is allowed to
change.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.orm import InstrumentedAttribute

from src.db.models import Shift
from src.db.repositories.base import BaseRepository


def _apply(row: object, fields: Mapping[str, Any]) -> None:
    """Write `fields` onto an ORM row; an unknown column is a typo, not a silent no-op."""
    for name, value in fields.items():
        if not hasattr(type(row), name):
            raise AttributeError(f"{type(row).__name__} has no column {name!r}")
        setattr(row, name, value)


class ShiftRepo(BaseRepository[Shift]):
    """`shifts` of the active venue, ordered the way a day runs."""

    model = Shift

    async def get(self, shift_id: int) -> Shift | None:
        return (await self.session.scalars(self.by_id(shift_id))).first()

    async def list_for_date(self, shift_date: dt.date) -> Sequence[Shift]:
        """Who works this date — the composition of the shift (TZ 5.3).

        Dated by the *start*: a shift that runs past midnight appears on the evening it
        opened and not on the morning it ends.
        """
        statement = (
            self.for_venue()
            .where(Shift.shift_date == shift_date)
            .order_by(Shift.start_time, Shift.id)
        )
        return (await self.session.scalars(statement)).all()

    async def list_for_user(
        self,
        user_id: int,
        *,
        date_from: dt.date,
        date_to: dt.date,
    ) -> Sequence[Shift]:
        """One person's shifts over an inclusive range of start dates (TZ 5.3)."""
        statement = (
            self.for_venue()
            .where(
                Shift.user_id == user_id,
                Shift.shift_date >= date_from,
                Shift.shift_date <= date_to,
            )
            .order_by(Shift.shift_date, Shift.start_time, Shift.id)
        )
        return (await self.session.scalars(statement)).all()

    async def list_between(self, *, date_from: dt.date, date_to: dt.date) -> Sequence[Shift]:
        """The whole venue's schedule over an inclusive range of start dates."""
        statement = (
            self.for_venue()
            .where(Shift.shift_date >= date_from, Shift.shift_date <= date_to)
            .order_by(Shift.shift_date, Shift.start_time, Shift.id)
        )
        return (await self.session.scalars(statement)).all()

    async def get_opener(self, shift_date: dt.date) -> Shift | None:
        """Who opens this date, if anyone does yet (TZ 4.2; the rule itself is D8)."""
        return await self._role_holder(shift_date, Shift.is_opener)

    async def get_closer(self, shift_date: dt.date) -> Shift | None:
        """Who closes this date, if anyone does yet (TZ 4.2; the rule itself is D8)."""
        return await self._role_holder(shift_date, Shift.is_closer)

    async def create(self, **fields: Any) -> Shift:
        """`venue_id` comes from the scope, never from the caller (TZ 3.3).

        `hours` is a required column and is *not* computed here: TZ 4.2 defines it as the
        cell of the venue's schedule sheet, and the only producer on the manual path is
        `src.services.shifts.shift_hours`.
        """
        shift = Shift(venue_id=self.venue_id, **fields)
        self.session.add(shift)
        await self.session.flush()
        return shift

    async def update(self, shift_id: int, **fields: Any) -> Shift | None:
        shift = await self.get(shift_id)
        if shift is None:
            return None
        _apply(shift, fields)
        await self.session.flush()
        return shift

    async def delete(self, shift_id: int) -> bool:
        """Remove a shift; `False` when the id is unknown here — including another venue's.

        A real delete, not a status change: TZ 5.3 removes a shift from the schedule, and the
        history that must survive it hangs on `SET NULL` foreign keys (`checklist_runs`,
        `writeoffs`), so nothing is lost with the row.
        """
        shift = await self.get(shift_id)
        if shift is None:
            return False
        await self.session.delete(shift)
        await self.session.flush()
        return True

    async def _role_holder(
        self,
        shift_date: dt.date,
        mark: InstrumentedAttribute[bool],
    ) -> Shift | None:
        """First shift of the date carrying `mark`. D8 keeps it to at most one — in the service."""
        statement = (
            self.for_venue()
            .where(Shift.shift_date == shift_date, mark.is_(True))
            .order_by(Shift.start_time, Shift.id)
        )
        return (await self.session.scalars(statement)).first()


__all__ = ["ShiftRepo"]
