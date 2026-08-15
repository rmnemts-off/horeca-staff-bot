"""Invite codes — `invite_codes` (plan, tasks 11 and 15; TZ 4.1, 5.1).

A code is single use and lives seven days. Both facts are stored, neither is judged here: the
row carries `expires_at`, `used_at` and `revoked_at`, and :mod:`src.services.access` decides
what a given code means at a given moment (TZ 3.2). That split is why `get_by_code` returns a
spent or expired code instead of hiding it — the service has to tell the person *which* of the
three refusals applies (TZ 5.1), and a repository that filtered them out would make that
impossible.

The table owns `venue_id`, so every query is scoped: a code is looked up inside the venue its
prefix names, and a code of another venue is simply not found (TZ 9, acceptance 11.3).

Expiry and revocation are two different facts, not one column: `revoked_at` is written on
withdrawal rather than by moving `expires_at`, which would lose the reason the code stopped
working (`docs/schema-notes.md`, plan task 15). Every moment arrives as an argument — there is
no clock in this layer (D12).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from src.db.models import InviteCode, InvitePurpose, MemberRole
from src.db.repositories.base import BaseRepository


class InviteCodeRepo(BaseRepository[InviteCode]):
    """`invite_codes` of the active venue."""

    model = InviteCode

    async def get_by_code(self, code: str) -> InviteCode | None:
        """The code as stored, whatever state it is in — the service reads the state."""
        statement = self.for_venue().where(InviteCode.code == code)
        return (await self.session.scalars(statement)).first()

    async def list_pending(self) -> Sequence[InviteCode]:
        """Codes that have not been spent or withdrawn (TZ 5.8: the manager's list).

        Expiry is deliberately not part of the filter: an expired code is still a row the
        manager issued and may want to see, and "expired" is a comparison against a moment
        this layer has no business producing.
        """
        statement = (
            self.for_venue()
            .where(InviteCode.used_at.is_(None), InviteCode.revoked_at.is_(None))
            .order_by(InviteCode.created_at, InviteCode.id)
        )
        return (await self.session.scalars(statement)).all()

    async def create(
        self,
        *,
        code: str,
        role: MemberRole,
        expires_at: dt.datetime,
        position: str | None = None,
        full_name: str | None = None,
        created_by: int | None = None,
        purpose: InvitePurpose = InvitePurpose.JOIN,
        issued_to_member_id: int | None = None,
    ) -> InviteCode:
        """`full_name` is the manager's draft of the name (decision B8), not a fact yet.

        `purpose` and `issued_to_member_id` travel together and the table enforces it: a
        `rebind` code names the card it will move, a `join` code names none.
        """
        invite = InviteCode(
            venue_id=self.venue_id,
            code=code,
            role=role,
            expires_at=expires_at,
            position=position,
            full_name=full_name,
            created_by=created_by,
            purpose=purpose,
            issued_to_member_id=issued_to_member_id,
        )
        self.session.add(invite)
        await self.session.flush()
        return invite

    async def mark_used(
        self,
        code_id: int,
        *,
        used_by: int,
        used_at: dt.datetime,
    ) -> InviteCode | None:
        """Spend the code. Single use is the service's check; this only records the fact."""
        invite = await self._get(code_id)
        if invite is None:
            return None
        invite.used_by = used_by
        invite.used_at = used_at
        await self.session.flush()
        return invite

    async def revoke(self, code_id: int, moment: dt.datetime) -> InviteCode | None:
        """Withdraw a code, keeping `expires_at` intact (plan, task 15)."""
        invite = await self._get(code_id)
        if invite is None:
            return None
        invite.revoked_at = moment
        await self.session.flush()
        return invite

    async def _get(self, code_id: int) -> InviteCode | None:
        """By id, still scoped: an id from another venue resolves to nothing (TZ 9)."""
        return (await self.session.scalars(self.by_id(code_id))).first()


__all__ = ["InviteCodeRepo"]
