"""The employee roster of one venue (plan, task 27 — service layer; TZ 2, 5.1, 5.8).

The screen this feeds is the employees list inside the management section (TZ 5.8): who
works here, who has blocked the bot, and the two lifecycle operations a manager actually
performs — hand out a code and deactivate someone who left. The wording of that screen
lives in ``src/bot/texts/``; not a single label is repeated here (TZ 8.2).

Two rules shape everything below.

**Deactivation is not deletion (TZ 5.1).** `venue_members.is_active` goes false and the row
stays: every checklist run, write-off and shift of that person must remain in the reports.
Nothing here deletes a membership, and the schema backs that up — `shifts.user_id` and
friends are `RESTRICT`, so the row could not be removed even by mistake.

**"Bot blocked" is read, never guessed (TZ 6).** The flag lives on `users.is_bot_blocked`
and is written by the notification worker when a delivery fails; this module only surfaces
it in the roster, because scanning `notifications` on every render would be both slow and
wrong.

Issuing invite codes is deliberately *not* here — it lives in `src/services/access.py`
together with activation and revocation, so that the whole life of a code is one file
(plan, task 15).

**Why this service takes a provider rather than a scoped repository.** Every other service
of stage 0 is built for one venue and holds a repository already scoped to it, exactly as
`src/db/repositories/protocols.py` describes. This one takes `MemberRepositories` and calls
`members(actor.venue_id)` on each method instead, because the roster is the one screen where
the scope must equal *the actor's* venue and nothing else: deriving it from the actor at call
time makes the two impossible to disagree, so a forged `callback_data` carrying a membership
id from another venue finds nothing rather than relying on a second check to catch it (TZ 9,
acceptance 11.3). The full boundary — which services take which shape, and why the exception
has exactly two members — is written down in the module docstring of `src/services/access.py`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from src.db.models import MemberRole, User, VenueMember
from src.db.repositories.protocols import UserRepository, VenueMemberRepository
from src.services.access import (
    AccessContext,
    AccessError,
    require_manager,
    require_not_self,
    require_outranks_target,
    require_owner,
    require_venue,
    role_rank,
)
from src.services.audit import SILENT, AuditEntity, AuditTrail, snapshot


class MemberError(AccessError):
    """Base class for the refusals of this module."""


class MemberNotFoundError(MemberError):
    """No such membership in the actor's venue.

    A membership of another venue lands here too, and on purpose: the repository is scoped,
    so a foreign id simply resolves to nothing (TZ 9, acceptance 11.3).
    """

    def __init__(self, member_id: int, venue_id: int) -> None:
        super().__init__(f"membership {member_id} does not belong to venue {venue_id}")
        self.member_id = member_id
        self.venue_id = venue_id


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One line of the employees list: the membership plus the person behind it."""

    member: VenueMember
    user: User | None

    @property
    def member_id(self) -> int:
        return self.member.id

    @property
    def user_id(self) -> int:
        return self.member.user_id

    @property
    def full_name(self) -> str:
        return self.user.full_name if self.user is not None else ""

    @property
    def role(self) -> MemberRole:
        return self.member.role

    @property
    def position(self) -> str | None:
        return self.member.position

    @property
    def is_active(self) -> bool:
        return self.member.is_active

    @property
    def is_bot_blocked(self) -> bool:
        """TZ 6: the manager sees an undelivered notification as this mark in the list."""
        return self.user is not None and self.user.is_bot_blocked


class MemberRepositories(Protocol):
    """`users` is global; `venue_members` is scoped, so it arrives as a factory (TZ 3.3)."""

    @property
    def users(self) -> UserRepository: ...

    def members(self, venue_id: int) -> VenueMemberRepository: ...


class MemberService:
    """Roster of one venue: read it, and change who is in it (TZ 5.8).

    `audit` defaults to :data:`~src.services.audit.SILENT` so that a test may build the
    service with nothing behind it; the composition root always hands in a real one, and
    TZ 2 is why — "who switched Ivanov off and when" is the question the log is asked, and
    a deactivation is the one change of this service that is never visible anywhere else
    (the row stays exactly where it was, TZ 5.1).
    """

    def __init__(
        self,
        repositories: MemberRepositories,
        *,
        audit: AuditTrail = SILENT,
    ) -> None:
        self.repositories = repositories
        self._audit = audit

    # -- reading ------------------------------------------------------------------------

    async def list_roster(self, actor: AccessContext) -> tuple[RosterEntry, ...]:
        """Active employees of the venue, sorted by name (TZ 5.8)."""
        require_manager(actor)
        return await self._entries(await self.repositories.members(actor.venue_id).list_active())

    async def list_by_role(
        self,
        actor: AccessContext,
        role: MemberRole,
    ) -> tuple[RosterEntry, ...]:
        """Everyone holding one role — the owner's view of who may manage the venue."""
        require_manager(actor)
        found = await self.repositories.members(actor.venue_id).list_by_role(role)
        return await self._entries(member for member in found if member.is_active)

    async def list_assignable(self, actor: AccessContext) -> tuple[RosterEntry, ...]:
        """Who a shift may be given to: active members only (plan, task 29).

        Readable by any member, because "who else is on this shift" is a `staff` screen
        (TZ 5.3); only the *writing* side of the schedule is manager-only.
        """
        return await self._entries(await self.repositories.members(actor.venue_id).list_active())

    async def get(self, actor: AccessContext, member_id: int) -> RosterEntry | None:
        """One roster line, or `None` when the id belongs to another venue (TZ 9)."""
        member = await self.repositories.members(actor.venue_id).get(member_id)
        if member is None:
            return None
        return RosterEntry(member=member, user=await self.repositories.users.get(member.user_id))

    # -- lifecycle ----------------------------------------------------------------------

    async def deactivate(self, actor: AccessContext, member_id: int) -> RosterEntry:
        """The employee left: the row stays, the history stays, access stops (TZ 5.1).

        Two things this refuses, both of them one-way doors (TZ 2). Switching *yourself*
        off takes away every screen including this one, and the code that would let you
        back in can only be issued by an owner — of whom there is now one fewer. A manager
        switching the *owner* off arrives at the same place by the other road.
        """
        require_manager(actor)
        current = await self._require_member(actor, member_id)
        require_not_self(actor, current)
        require_outranks_target(actor, current)
        return await self._set_active(actor, current, is_active=False)

    async def reactivate(self, actor: AccessContext, member_id: int) -> RosterEntry:
        """The employee came back to the same venue and keeps their old row.

        No `require_not_self` here: bringing yourself back is unreachable — a switched-off
        member gets no screens at all — and it is not the direction that loses anything.
        """
        require_manager(actor)
        current = await self._require_member(actor, member_id)
        require_outranks_target(actor, current)
        return await self._set_active(actor, current, is_active=True)

    async def set_role(
        self,
        actor: AccessContext,
        member_id: int,
        role: MemberRole,
    ) -> RosterEntry:
        """Change a role. Handing out `manager` or `owner` is the owner's alone (TZ 2).

        Own role included in what nobody changes about themselves: an owner who demotes
        himself cannot promote himself back, because promoting needs an owner.
        """
        require_manager(actor)
        current = await self._require_member(actor, member_id)
        require_not_self(actor, current)
        require_outranks_target(actor, current)
        if role_rank(role) >= role_rank(MemberRole.MANAGER):
            require_owner(actor)
        return await self._update(actor, member_id, role=role)

    async def set_position(
        self,
        actor: AccessContext,
        member_id: int,
        position: str | None,
    ) -> RosterEntry:
        """Bartender / waiter / cook — free text, the venue names its own positions."""
        require_manager(actor)
        await self._require_member(actor, member_id)
        return await self._update(actor, member_id, position=position)

    async def rename(
        self,
        actor: AccessContext,
        member_id: int,
        full_name: str,
    ) -> RosterEntry:
        """Correct a full name (decision B8: the manager owns it, the employee confirms).

        The name lives on `users`, which is global, so the membership is resolved first —
        that scoped lookup is what stops a manager of one venue renaming a stranger.
        """
        require_manager(actor)
        member = await self._require_member(actor, member_id)
        require_venue(actor, member.venue_id)
        cleaned = full_name.strip()
        if not cleaned:
            raise MemberError("the full name cannot be empty")
        before = await self.repositories.users.get(member.user_id)
        user = await self.repositories.users.update(member.user_id, full_name=cleaned)
        await self._audit.updated(
            actor,
            AuditEntity.MEMBER,
            member.id,
            before=snapshot(before, "full_name"),
            after={"full_name": cleaned},
        )
        return RosterEntry(member=member, user=user)

    # -- internals ----------------------------------------------------------------------

    async def _require_member(self, actor: AccessContext, member_id: int) -> VenueMember:
        member = await self.repositories.members(actor.venue_id).get(member_id)
        if member is None:
            raise MemberNotFoundError(member_id, actor.venue_id)
        require_venue(actor, member.venue_id)
        return member

    async def _set_active(
        self,
        actor: AccessContext,
        current: VenueMember,
        *,
        is_active: bool,
    ) -> RosterEntry:
        """The row is already loaded and already checked: the guards need it, not the id."""
        member_id = current.id
        was_active = current.is_active
        member = await self.repositories.members(actor.venue_id).set_active(
            member_id,
            is_active=is_active,
        )
        if member is None:
            raise MemberNotFoundError(member_id, actor.venue_id)
        # TZ 5.1 keeps the row, so the row itself records nothing about the moment access
        # stopped. The log is the only place that fact exists (TZ 2).
        await self._audit.set_active(
            actor,
            AuditEntity.MEMBER,
            member.id,
            was_active=was_active,
            is_active=is_active,
        )
        return RosterEntry(member=member, user=await self.repositories.users.get(member.user_id))

    async def _update(
        self,
        actor: AccessContext,
        member_id: int,
        **fields: object,
    ) -> RosterEntry:
        before = await self.repositories.members(actor.venue_id).get(member_id)
        member = await self.repositories.members(actor.venue_id).update(member_id, **fields)
        if member is None:
            raise MemberNotFoundError(member_id, actor.venue_id)
        await self._audit.updated(
            actor,
            AuditEntity.MEMBER,
            member.id,
            before=snapshot(before, *fields),
            after=dict(fields),
        )
        return RosterEntry(member=member, user=await self.repositories.users.get(member.user_id))

    async def _entries(self, members: Iterable[VenueMember]) -> tuple[RosterEntry, ...]:
        entries = [
            RosterEntry(member=member, user=await self.repositories.users.get(member.user_id))
            for member in members
        ]
        entries.sort(key=lambda entry: (entry.full_name.casefold(), entry.member_id))
        return tuple(entries)


__all__ = [
    "MemberError",
    "MemberNotFoundError",
    "MemberRepositories",
    "MemberService",
    "RosterEntry",
]
