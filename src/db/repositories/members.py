"""Venue membership — `venue_members` (plan, task 11; TZ 4.1, 5.1, 5.8).

The scoped half of a person: `users` is global, and everything that is true only inside one
venue — the role, the position, whether they still work here — lives in this table. It owns
`venue_id`, so this is an ordinary :class:`~src.db.repositories.base.BaseRepository` and every
query below is built from `for_venue()` or `by_id()`. A membership id arriving from a forged
`callback_data` therefore resolves to `None` rather than to somebody else's roster line
(TZ 9, acceptance 11.3).

No rule is decided here (TZ 3.2). Deactivation is `is_active = false` and never a delete —
TZ 5.1 requires the history to survive, and the schema agrees (`shifts.user_id` is
`RESTRICT`) — but *when* to deactivate, and who may hand out which role, is
:mod:`src.services.members` and :mod:`src.services.access`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select

from src.db.models import MemberRole, VenueMember
from src.db.repositories.base import BaseRepository


def _apply(row: object, fields: Mapping[str, Any]) -> None:
    """Write `fields` onto an ORM row; an unknown column is a typo, not a silent no-op."""
    for name, value in fields.items():
        if not hasattr(type(row), name):
            raise AttributeError(f"{type(row).__name__} has no column {name!r}")
        setattr(row, name, value)


class VenueMemberRepo(BaseRepository[VenueMember]):
    """`venue_members` of the active venue."""

    model = VenueMember

    async def get(self, member_id: int) -> VenueMember | None:
        return (await self.session.scalars(self.by_id(member_id))).first()

    async def get_for_user(self, user_id: int) -> VenueMember | None:
        """The membership of one person here, active or not.

        `uq_venue_members_user_venue` makes the answer unique, so the caller may treat it as
        a single row; whether an inactive membership still counts is the service's call.
        """
        statement = self.for_venue().where(VenueMember.user_id == user_id)
        return (await self.session.scalars(statement)).first()

    async def list_active(self) -> Sequence[VenueMember]:
        """Who works here now — the employees list of TZ 5.8, unsorted by name.

        Ordering is by id on purpose: the screen sorts by full name, and the name lives on
        `users`, which this scoped query cannot join without leaving its venue.
        """
        statement = self.for_venue().where(VenueMember.is_active.is_(True)).order_by(VenueMember.id)
        return (await self.session.scalars(statement)).all()

    async def list_by_role(self, role: MemberRole) -> Sequence[VenueMember]:
        """Everyone holding one role, active or not (TZ 6 resolves notification recipients)."""
        statement = self.for_venue().where(VenueMember.role == role).order_by(VenueMember.id)
        return (await self.session.scalars(statement)).all()

    async def count_active_by_role(self, role: MemberRole, *, for_update: bool = False) -> int:
        """How many people hold this role and still work here (TZ 2).

        `for_update` locks the rows it counted. The invariant this serves — a venue is never
        left without an owner — is a read followed by a write, and two owners demoting each
        other at the same moment would both read "there are two of us" and both proceed.
        The lock is on the rows of the role being counted, so ordinary work does not meet it.
        """
        statement = (
            select(VenueMember.id)
            .where(VenueMember.venue_id == self.venue_id)
            .where(VenueMember.role == role)
            .where(VenueMember.is_active.is_(True))
        )
        if for_update:
            statement = statement.with_for_update()
        return len((await self.session.scalars(statement)).all())

    async def add(
        self,
        *,
        user_id: int,
        role: MemberRole,
        position: str | None = None,
    ) -> VenueMember:
        member = VenueMember(
            venue_id=self.venue_id,
            user_id=user_id,
            role=role,
            position=position,
        )
        self.session.add(member)
        await self.session.flush()
        return member

    async def update(self, member_id: int, **fields: Any) -> VenueMember | None:
        member = await self.get(member_id)
        if member is None:
            return None
        _apply(member, fields)
        await self.session.flush()
        return member

    async def set_active(self, member_id: int, *, is_active: bool) -> VenueMember | None:
        """TZ 5.1: the employee left, the row and the whole history stay."""
        return await self.update(member_id, is_active=is_active)


__all__ = ["VenueMemberRepo"]
