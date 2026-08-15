"""Access service (plan, task 15; TZ 2, 5.1; answers A3 and A4; decision B8).

Fake repositories again, and for the same reason as everywhere in this package: the rules
being tested are rules, not queries. Two properties of the fakes carry weight on their own.

* **They are venue-scoped the way the contract says.** `FakeMembers` and `FakeInvites` are
  built for one venue and filter the shared store by it, so "somebody else's venue" is a row
  that exists and is unreachable — not a row that was never written (TZ 3.3, 11.3). That is
  what makes the invite code of another venue come back as *unknown* rather than as *valid*.
* **`list_active()` returns active rows only**, as the real repository does. The recipient
  resolver of answer A4 re-checks `is_active` anyway; both halves are exercised.
* **`update` is as narrow as the statement behind it.** `FakeMembers` and `FakeUsers` write
  through :func:`apply_fields`, the `_apply` of `src/db/repositories/members.py` and
  `users.py`: an unknown column raises instead of growing an instance attribute, so a
  misspelt field name fails here rather than in production.

Time is never read from a clock: every method takes `now` as an aware UTC argument (decision
D12), which is what lets the seven-day expiry be tested to the second.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from src.db.models import InviteCode, InvitePurpose, MemberRole, User, Venue, VenueMember
from src.services import access as access_module
from src.services.access import (
    INVITE_CODE_ATTEMPTS,
    INVITE_CODE_TTL,
    AccessContext,
    AccessService,
    InviteCodeGenerationError,
    InviteRejection,
    NaiveMomentError,
    PermissionDeniedError,
    UnknownMembershipError,
    VenueMismatchError,
    format_invite_code,
    invite_deeplink_payload,
    new_invite_secret,
    parse_invite_code,
    require_manager,
    require_owner,
    require_role,
    require_self_or_manager,
    require_venue,
    role_rank,
)
from src.services.notifications import ManagerRecipients

VENUE_ID = 1
OTHER_VENUE_ID = 2

STAFF_TG = 917_323_199  # answer A1: the test bartender
MANAGER_TG = 1_672_818_749  # answer A1: the test owner
NEWCOMER_TG = 555_000_111
#: The account a card is moved *to* — nobody the bot has seen before.
NEW_TG = 777_000_222
BOOTSTRAP_TG = 4_242_424

NOW = dt.datetime(2026, 8, 13, 9, 0, tzinfo=dt.UTC)
# The random half of an invite code, fixed so the expiry can be asserted to the second.
# The suppression below is deliberate and stays local to this line: S105 reads any literal
# bound to a name like SECRET as a leaked password, while this is a throwaway test constant
# for a single-use token that lives seven days (TZ 5.1). Real secrets come from the
# environment and never from the repository (TZ 9).
SECRET = "A7K9QX4M"  # noqa: S105


# --------------------------------------------------------------------------------------
# Stores (declared here on purpose: tests/factories.py belongs to task 5a)
# --------------------------------------------------------------------------------------


def apply_fields(row: object, fields: Mapping[str, Any]) -> None:
    """`_apply` of `src/db/repositories/members.py` and `users.py`, character for character.

    The generous version is a bare `setattr` loop: `update(user_id, fullname=...)` would
    invent an attribute, the fake would hand back a row that looks written, and the same
    call would fail against PostgreSQL. An unknown column is a typo and raises here.
    """
    for name, value in fields.items():
        if not hasattr(type(row), name):
            raise AttributeError(f"{type(row).__name__} has no column {name!r}")
        setattr(row, name, value)


class UserStore:
    def __init__(self) -> None:
        self.rows: list[User] = []
        self._next_id = 1
        self.active_venue_calls: list[tuple[int, int | None]] = []

    def add(
        self,
        *,
        telegram_id: int,
        full_name: str,
        username: str | None = None,
        is_active: bool = True,
        active_venue_id: int | None = None,
        is_bot_blocked: bool = False,
    ) -> User:
        user = User(
            id=self._next_id,
            telegram_id=telegram_id,
            full_name=full_name,
            username=username,
            is_active=is_active,
            active_venue_id=active_venue_id,
            is_bot_blocked=is_bot_blocked,
        )
        self._next_id += 1
        self.rows.append(user)
        return user


class FakeUsers:
    """`UserRepository` — global: one person may work in several venues (TZ 2)."""

    def __init__(self, store: UserStore) -> None:
        self.store = store

    async def get(self, user_id: int) -> User | None:
        return next((row for row in self.store.rows if row.id == user_id), None)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        return next((row for row in self.store.rows if row.telegram_id == telegram_id), None)

    async def create(
        self,
        *,
        telegram_id: int,
        full_name: str,
        username: str | None = None,
        phone: str | None = None,
    ) -> User:
        return self.store.add(
            telegram_id=telegram_id,
            full_name=full_name,
            username=username,
        )

    async def update(self, user_id: int, **fields: Any) -> User | None:
        row = await self.get(user_id)
        if row is None:
            return None
        apply_fields(row, fields)
        return row

    async def touch_last_seen(self, user_id: int, moment: dt.datetime) -> None:
        row = await self.get(user_id)
        if row is not None:
            row.last_seen_at = moment

    async def set_active_venue(self, user_id: int, venue_id: int | None) -> None:
        self.store.active_venue_calls.append((user_id, venue_id))
        row = await self.get(user_id)
        if row is not None:
            row.active_venue_id = venue_id

    async def set_bot_blocked(self, user_id: int, *, blocked: bool) -> None:
        row = await self.get(user_id)
        if row is not None:
            row.is_bot_blocked = blocked


class MemberStore:
    def __init__(self) -> None:
        self.rows: list[VenueMember] = []
        self._next_id = 1

    def add(
        self,
        *,
        venue_id: int,
        user_id: int,
        role: MemberRole,
        position: str | None = None,
        is_active: bool = True,
    ) -> VenueMember:
        member = VenueMember(
            id=self._next_id,
            venue_id=venue_id,
            user_id=user_id,
            role=role,
            position=position,
            is_active=is_active,
        )
        self._next_id += 1
        self.rows.append(member)
        return member


class FakeMembers:
    """`VenueMemberRepository`, scoped to one venue (TZ 3.3)."""

    def __init__(self, store: MemberStore, venue_id: int) -> None:
        self.store = store
        self.venue_id = venue_id

    def _scoped(self) -> list[VenueMember]:
        return [row for row in self.store.rows if row.venue_id == self.venue_id]

    async def get(self, member_id: int) -> VenueMember | None:
        return next((row for row in self._scoped() if row.id == member_id), None)

    async def get_for_user(self, user_id: int) -> VenueMember | None:
        return next((row for row in self._scoped() if row.user_id == user_id), None)

    async def list_active(self) -> Sequence[VenueMember]:
        return [row for row in self._scoped() if row.is_active]

    async def list_by_role(self, role: MemberRole) -> Sequence[VenueMember]:
        return [row for row in self._scoped() if row.role is role]

    async def add(
        self,
        *,
        user_id: int,
        role: MemberRole,
        position: str | None = None,
    ) -> VenueMember:
        return self.store.add(
            venue_id=self.venue_id,
            user_id=user_id,
            role=role,
            position=position,
        )

    async def update(self, member_id: int, **fields: Any) -> VenueMember | None:
        row = await self.get(member_id)
        if row is None:
            return None
        apply_fields(row, fields)
        return row

    async def set_active(self, member_id: int, *, is_active: bool) -> VenueMember | None:
        return await self.update(member_id, is_active=is_active)


class FakeVenues:
    """`VenueRepository` — addressed by venue id, because it *is* the scope."""

    def __init__(self, venues: Sequence[Venue], members: MemberStore) -> None:
        self.venues = {venue.id: venue for venue in venues}
        self.members = members

    async def get(self, venue_id: int) -> Venue | None:
        return self.venues.get(venue_id)

    async def list_for_user(self, user_id: int) -> Sequence[Venue]:
        # A join over `venue_members`, membership row included whether active or not: it is
        # the service that decides what an inactive membership means.
        ids = {row.venue_id for row in self.members.rows if row.user_id == user_id}
        return [venue for venue_id, venue in self.venues.items() if venue_id in ids]

    async def list_active(self) -> Sequence[Venue]:
        # `VenueRepo.list_active`: switched-on venues, no membership involved.
        return [venue for venue in self.venues.values() if venue.is_active]

    async def create(self, *, name: str, city: str, timezone: str) -> Venue:
        raise NotImplementedError

    async def update(self, venue_id: int, **fields: Any) -> Venue | None:
        raise NotImplementedError


class InviteStore:
    def __init__(self) -> None:
        self.rows: list[InviteCode] = []
        self._next_id = 1

    def add(
        self,
        *,
        venue_id: int,
        code: str,
        role: MemberRole = MemberRole.STAFF,
        expires_at: dt.datetime,
        position: str | None = None,
        full_name: str | None = None,
        created_by: int | None = None,
        used_by: int | None = None,
        used_at: dt.datetime | None = None,
        revoked_at: dt.datetime | None = None,
        purpose: InvitePurpose = InvitePurpose.JOIN,
        issued_to_member_id: int | None = None,
    ) -> InviteCode:
        # `purpose` defaults here exactly as it does in `InviteCodeRepo.create` and in the
        # column's server default. A fake that left it `None` would make every code look
        # like neither kind, and the guard that reads it would quietly stop guarding.
        invite = InviteCode(
            id=self._next_id,
            venue_id=venue_id,
            code=code,
            role=role,
            purpose=purpose,
            issued_to_member_id=issued_to_member_id,
            position=position,
            full_name=full_name,
            created_by=created_by,
            expires_at=expires_at,
            used_by=used_by,
            used_at=used_at,
            revoked_at=revoked_at,
        )
        self._next_id += 1
        self.rows.append(invite)
        return invite


class FakeInvites:
    """`InviteCodeRepository`, scoped to one venue.

    `invite_codes.code` is unique across the whole table, but the repository still filters
    by venue — which is exactly why a valid secret quoted with the wrong venue id resolves
    to nothing instead of letting somebody into the wrong bar.
    """

    def __init__(self, store: InviteStore, venue_id: int) -> None:
        self.store = store
        self.venue_id = venue_id

    def _scoped(self) -> list[InviteCode]:
        return [row for row in self.store.rows if row.venue_id == self.venue_id]

    async def get_by_code(self, code: str) -> InviteCode | None:
        return next((row for row in self._scoped() if row.code == code), None)

    async def list_pending(self) -> Sequence[InviteCode]:
        return [
            row
            for row in self._scoped()
            if row.used_at is None and row.used_by is None and row.revoked_at is None
        ]

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
        return self.store.add(
            venue_id=self.venue_id,
            code=code,
            role=role,
            expires_at=expires_at,
            position=position,
            purpose=purpose,
            issued_to_member_id=issued_to_member_id,
            full_name=full_name,
            created_by=created_by,
        )

    async def mark_used(
        self,
        code_id: int,
        *,
        used_by: int,
        used_at: dt.datetime,
    ) -> InviteCode | None:
        row = next((row for row in self._scoped() if row.id == code_id), None)
        if row is None:
            return None
        row.used_by = used_by
        row.used_at = used_at
        return row

    async def revoke(self, code_id: int, moment: dt.datetime) -> InviteCode | None:
        row = next((row for row in self._scoped() if row.id == code_id), None)
        if row is None:
            return None
        row.revoked_at = moment
        return row


class AuditStore:
    """Rows of `audit_log`, in the order they were written, of every venue.

    One store and not one per venue, exactly like the table: the scope is the predicate the
    sink was built with, so a row of the venue next door is written here and simply not
    attributed to this one (CLAUDE.md).
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def entries(self, venue_id: int) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["venue_id"] == venue_id]


class FakeAudit:
    """`AuditLogRepository` as `AuditSink` sees it: append-only, scoped to one venue."""

    def __init__(self, store: AuditStore, venue_id: int) -> None:
        self.store = store
        self.venue_id = venue_id

    async def record(
        self,
        *,
        user_id: int | None,
        entity: str,
        entity_id: int | None,
        action: str,
        diff: dict[str, Any] | None = None,
    ) -> None:
        self.store.rows.append(
            {
                "venue_id": self.venue_id,
                "user_id": user_id,
                "entity": entity,
                "entity_id": entity_id,
                "action": action,
                "diff": diff,
            }
        )


@dataclass(slots=True)
class FakeRepositories:
    """The `AccessRepositories` protocol: two globals and two factories."""

    user_store: UserStore
    member_store: MemberStore
    invite_store: InviteStore
    venue_rows: list[Venue]
    audit_store: AuditStore

    @property
    def users(self) -> FakeUsers:
        return FakeUsers(self.user_store)

    @property
    def venues(self) -> FakeVenues:
        return FakeVenues(self.venue_rows, self.member_store)

    def members(self, venue_id: int) -> FakeMembers:
        return FakeMembers(self.member_store, venue_id)

    def invites(self, venue_id: int) -> FakeInvites:
        return FakeInvites(self.invite_store, venue_id)

    def audit(self, venue_id: int) -> FakeAudit:
        return FakeAudit(self.audit_store, venue_id)


# --------------------------------------------------------------------------------------
# Stand
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Stand:
    repositories: FakeRepositories
    service: AccessService

    @property
    def users(self) -> UserStore:
        return self.repositories.user_store

    @property
    def members(self) -> MemberStore:
        return self.repositories.member_store

    @property
    def invites(self) -> InviteStore:
        return self.repositories.invite_store

    @property
    def audit(self) -> AuditStore:
        return self.repositories.audit_store


def build(*, bootstrap: Sequence[int] = ()) -> Stand:
    repositories = FakeRepositories(
        user_store=UserStore(),
        member_store=MemberStore(),
        invite_store=InviteStore(),
        audit_store=AuditStore(),
        venue_rows=[
            Venue(
                id=VENUE_ID,
                name="PIMS",
                city="Moscow",
                timezone="Europe/Moscow",
                is_active=True,
            ),
            Venue(
                id=OTHER_VENUE_ID,
                name="Invasion",
                city="Moscow",
                timezone="Europe/Moscow",
                is_active=True,
            ),
        ],
    )
    return Stand(
        repositories=repositories,
        service=AccessService(repositories, bootstrap_owner_ids=bootstrap),
    )


@pytest.fixture
def stand() -> Stand:
    return build()


def actor(
    role: MemberRole,
    *,
    user_id: int = 1,
    venue_id: int = VENUE_ID,
) -> AccessContext:
    return AccessContext(
        user_id=user_id,
        telegram_id=100_000 + user_id,
        venue_id=venue_id,
        member_id=user_id,
        role=role,
        full_name=f"user-{user_id}",
    )


STAFF = actor(MemberRole.STAFF)
MANAGER = actor(MemberRole.MANAGER, user_id=2)
OWNER = actor(MemberRole.OWNER, user_id=3)
STRANGER = actor(MemberRole.OWNER, user_id=4, venue_id=OTHER_VENUE_ID)


def seed_member(
    stand: Stand,
    *,
    telegram_id: int,
    role: MemberRole,
    venue_id: int = VENUE_ID,
    full_name: str = "Анна",
    active_venue_id: int | None = None,
    member_active: bool = True,
    user_active: bool = True,
) -> tuple[User, VenueMember]:
    user = stand.users.add(
        telegram_id=telegram_id,
        full_name=full_name,
        is_active=user_active,
        active_venue_id=active_venue_id,
    )
    member = stand.members.add(
        venue_id=venue_id,
        user_id=user.id,
        role=role,
        is_active=member_active,
    )
    return user, member


# --------------------------------------------------------------------------------------
# Identity: who is this and where do they work (TZ 2, 5.1)
# --------------------------------------------------------------------------------------


async def test_an_unknown_telegram_id_gets_nothing_but_the_code_prompt(stand: Stand) -> None:
    identity = await stand.service.resolve(NEWCOMER_TG)

    assert identity.is_known is False
    assert identity.contexts == ()
    assert identity.active is None
    assert identity.may_create_venue is False
    assert identity.needs_venue_choice is False


async def test_a_single_membership_is_selected_by_itself(stand: Stand) -> None:
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)

    identity = await stand.service.resolve(STAFF_TG)

    assert identity.is_known is True
    assert identity.active is not None
    assert identity.active.venue_id == VENUE_ID
    assert identity.active.role is MemberRole.STAFF
    assert identity.needs_venue_choice is False


async def test_two_venues_ask_the_person_to_choose(stand: Stand) -> None:
    """TZ 5.1: a person working in several venues picks one after `/start`."""
    user, _ = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)
    stand.members.add(venue_id=OTHER_VENUE_ID, user_id=user.id, role=MemberRole.MANAGER)

    identity = await stand.service.resolve(STAFF_TG)

    assert [context.venue_id for context in identity.contexts] == [VENUE_ID, OTHER_VENUE_ID]
    assert identity.active is None
    assert identity.needs_venue_choice is True


async def test_a_venue_the_person_does_not_work_in_is_not_offered(stand: Stand) -> None:
    """A stranger to the second venue is offered only the one they work in (TZ 5.1, TZ 2).

    The gate is here, in the service, not in the query: `VenueRepo.list_for_user` is
    documented as returning every venue the person holds a membership row in — inactive
    memberships and inactive venues included — and `_contexts_for` re-reads the membership
    and drops what does not hold up. So this test survives replacing the repository's join
    with a full directory dump, and that is the intended shape, not a gap: the answer to
    "may this person in" is a service judgement (TZ 3.2).

    What it does pin is that the service actually asks. Every other test here puts the
    person in both venues, so nothing is ever filtered out and a service that simply echoed
    the repository would look identical.
    """
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)

    identity = await stand.service.resolve(STAFF_TG)

    assert [context.venue_id for context in identity.contexts] == [VENUE_ID]
    assert identity.needs_venue_choice is False
    # The venue is there to be found — it is the membership that is missing, not the row.
    assert {venue.id for venue in stand.repositories.venue_rows} == {VENUE_ID, OTHER_VENUE_ID}


async def test_the_remembered_venue_wins(stand: Stand) -> None:
    """The choice lives in the database, so it outlives a restart and the FSM TTL."""
    user, _ = seed_member(
        stand,
        telegram_id=STAFF_TG,
        role=MemberRole.STAFF,
        active_venue_id=OTHER_VENUE_ID,
    )
    stand.members.add(venue_id=OTHER_VENUE_ID, user_id=user.id, role=MemberRole.MANAGER)

    identity = await stand.service.resolve(STAFF_TG)

    assert identity.active is not None
    assert identity.active.venue_id == OTHER_VENUE_ID
    assert identity.active.role is MemberRole.MANAGER


async def test_a_deactivated_membership_is_not_a_way_in(stand: Stand) -> None:
    """TZ 5.1: deactivation keeps the row and the history, and stops the access."""
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF, member_active=False)

    identity = await stand.service.resolve(STAFF_TG)

    assert identity.is_known is True
    assert identity.contexts == ()
    assert identity.active is None


async def test_a_deactivated_person_is_not_recognised(stand: Stand) -> None:
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF, user_active=False)

    assert (await stand.service.resolve(STAFF_TG)).is_known is False


async def test_a_closed_venue_drops_out_of_the_identity(stand: Stand) -> None:
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)
    stand.repositories.venue_rows[0].is_active = False

    assert (await stand.service.resolve(STAFF_TG)).contexts == ()


async def test_context_for_answers_only_about_the_venue_asked(stand: Stand) -> None:
    seed_member(stand, telegram_id=MANAGER_TG, role=MemberRole.OWNER)

    assert await stand.service.context_for(MANAGER_TG, VENUE_ID) is not None
    assert await stand.service.context_for(MANAGER_TG, OTHER_VENUE_ID) is None


async def test_selecting_a_venue_remembers_it(stand: Stand) -> None:
    user, member = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)

    context = await stand.service.select_venue(user.id, VENUE_ID)

    assert context.member_id == member.id
    assert context.role is MemberRole.STAFF
    assert stand.users.active_venue_calls == [(user.id, VENUE_ID)]


async def test_selecting_a_venue_one_does_not_work_in_is_refused(stand: Stand) -> None:
    """A forged venue id from `callback_data` dies here (TZ 9)."""
    user, _ = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)

    with pytest.raises(UnknownMembershipError):
        await stand.service.select_venue(user.id, OTHER_VENUE_ID)
    assert stand.users.active_venue_calls == []


async def test_selecting_a_venue_one_was_removed_from_is_refused(stand: Stand) -> None:
    user, _ = seed_member(
        stand,
        telegram_id=STAFF_TG,
        role=MemberRole.STAFF,
        member_active=False,
    )

    with pytest.raises(UnknownMembershipError):
        await stand.service.select_venue(user.id, VENUE_ID)


# --------------------------------------------------------------------------------------
# The bootstrap owner: a trigger, never a right (answer A3)
# --------------------------------------------------------------------------------------


async def test_the_bootstrap_id_unlocks_the_wizard_and_nothing_else() -> None:
    stand = build(bootstrap=[BOOTSTRAP_TG])

    identity = await stand.service.resolve(BOOTSTRAP_TG)

    assert identity.may_create_venue is True
    # No membership yet, so no rights: the wizard is all this id can reach.
    assert identity.is_known is False
    assert identity.contexts == ()


async def test_the_bootstrap_trigger_is_spent_once_a_membership_exists() -> None:
    """Answer A3: after the venue is created the rights are read only from the database."""
    stand = build(bootstrap=[BOOTSTRAP_TG])
    seed_member(stand, telegram_id=BOOTSTRAP_TG, role=MemberRole.OWNER)

    identity = await stand.service.resolve(BOOTSTRAP_TG)

    assert identity.may_create_venue is False
    assert identity.active is not None
    assert identity.active.role is MemberRole.OWNER


async def test_the_environment_grants_no_role_of_its_own() -> None:
    """The id is listed as a bootstrap owner, the database says `staff`. TZ 2: the database."""
    stand = build(bootstrap=[BOOTSTRAP_TG])
    seed_member(stand, telegram_id=BOOTSTRAP_TG, role=MemberRole.STAFF)

    identity = await stand.service.resolve(BOOTSTRAP_TG)

    assert identity.active is not None
    assert identity.active.role is MemberRole.STAFF
    assert identity.active.is_manager is False
    assert identity.may_create_venue is False


async def test_an_id_outside_the_list_never_reaches_the_wizard() -> None:
    stand = build(bootstrap=[BOOTSTRAP_TG])

    assert (await stand.service.resolve(NEWCOMER_TG)).may_create_venue is False


# --------------------------------------------------------------------------------------
# Guards: the only place a right is decided (TZ 2, 9)
# --------------------------------------------------------------------------------------


def test_roles_are_ordered_the_way_the_tz_describes_them() -> None:
    assert role_rank(MemberRole.STAFF) < role_rank(MemberRole.MANAGER) < role_rank(MemberRole.OWNER)
    assert MANAGER.is_manager is True
    assert OWNER.is_manager is True
    assert OWNER.is_owner is True
    assert STAFF.is_manager is False
    assert MANAGER.is_owner is False


def test_a_role_below_the_bar_is_refused_with_both_halves_of_the_reason() -> None:
    with pytest.raises(PermissionDeniedError) as refusal:
        require_manager(STAFF)

    assert refusal.value.role is MemberRole.STAFF
    assert refusal.value.required is MemberRole.MANAGER


def test_an_owner_may_do_everything_a_manager_may() -> None:
    require_manager(OWNER)
    require_owner(OWNER)
    require_role(OWNER, MemberRole.STAFF)

    with pytest.raises(PermissionDeniedError):
        require_owner(MANAGER)


def test_an_object_of_another_venue_is_refused() -> None:
    with pytest.raises(VenueMismatchError) as refusal:
        require_venue(MANAGER, OTHER_VENUE_ID)

    assert refusal.value.actor_venue_id == VENUE_ID
    assert refusal.value.venue_id == OTHER_VENUE_ID
    require_venue(STRANGER, OTHER_VENUE_ID)


def test_staff_sees_its_own_data_and_a_manager_sees_everyones() -> None:
    require_self_or_manager(STAFF, STAFF.user_id)
    require_self_or_manager(MANAGER, STAFF.user_id)

    with pytest.raises(PermissionDeniedError):
        require_self_or_manager(STAFF, MANAGER.user_id)


def test_a_naive_moment_is_a_bug_and_says_so() -> None:
    with pytest.raises(NaiveMomentError):
        access_module.as_utc(dt.datetime(2026, 8, 13, 9, 0))

    moscow = dt.timezone(dt.timedelta(hours=3))
    assert access_module.as_utc(dt.datetime(2026, 8, 13, 12, 0, tzinfo=moscow)) == NOW


# --------------------------------------------------------------------------------------
# The shape of a code
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "1-A7K9QX4M",
        "  1-a7k9qx4m  ",
        "inv_1-A7K9QX4M",
        "INV_1-a7k9qx4m",
    ],
)
def test_a_code_is_recognised_however_it_was_typed(raw: str) -> None:
    assert parse_invite_code(raw) == (VENUE_ID, format_invite_code(VENUE_ID, SECRET))


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "hello",
        "1-",
        "-A7K9QX4M",
        "0-A7K9QX4M",
        "abc-A7K9QX4M",
        # I, L, O, 0 and 1 are outside the alphabet: a code is read aloud and retyped.
        "1-A7K9QX4I",
        "1-A7K9QX4!",
    ],
)
def test_what_cannot_be_a_code_is_not_one(raw: str) -> None:
    assert parse_invite_code(raw) is None


def test_the_deeplink_payload_round_trips() -> None:
    code = format_invite_code(VENUE_ID, SECRET)
    payload = invite_deeplink_payload(code)

    assert payload == "inv_1-A7K9QX4M"
    assert parse_invite_code(payload) == (VENUE_ID, code)


def test_a_fresh_secret_avoids_the_glyphs_people_confuse() -> None:
    for _ in range(50):
        secret = new_invite_secret()
        assert len(secret) == access_module.INVITE_CODE_SECRET_LENGTH
        assert not set(secret) & set("ILO01")


# --------------------------------------------------------------------------------------
# Issuing and revoking (TZ 5.1, decision B8)
# --------------------------------------------------------------------------------------


@pytest.fixture
def fixed_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(access_module, "new_invite_secret", lambda *_args, **_kwargs: SECRET)
    return SECRET


async def test_a_manager_hands_out_a_single_use_code_that_lives_a_week(
    stand: Stand,
    fixed_secret: str,
) -> None:
    code = await stand.service.issue_invite_code(
        MANAGER,
        role=MemberRole.STAFF,
        now=NOW,
        position="бармен",
        full_name="Анна Иванова",
    )

    assert code.code == format_invite_code(VENUE_ID, fixed_secret)
    assert code.expires_at == NOW + INVITE_CODE_TTL
    assert code.expires_at - NOW == dt.timedelta(days=7)
    assert code.role is MemberRole.STAFF
    assert code.created_by == MANAGER.user_id
    # Decision B8: the manager types the name, the employee confirms it on activation.
    assert code.full_name == "Анна Иванова"
    assert code.position == "бармен"


async def test_staff_cannot_hand_out_codes(stand: Stand, fixed_secret: str) -> None:
    with pytest.raises(PermissionDeniedError):
        await stand.service.issue_invite_code(STAFF, role=MemberRole.STAFF, now=NOW)
    assert stand.invites.rows == []


@pytest.mark.parametrize("role", [MemberRole.MANAGER, MemberRole.OWNER])
async def test_only_an_owner_creates_managers(
    stand: Stand,
    fixed_secret: str,
    role: MemberRole,
) -> None:
    """TZ 2: the owner adds and removes managers."""
    with pytest.raises(PermissionDeniedError) as refusal:
        await stand.service.issue_invite_code(MANAGER, role=role, now=NOW)
    assert refusal.value.required is MemberRole.OWNER

    issued = await stand.service.issue_invite_code(OWNER, role=role, now=NOW)
    assert issued.role is role


async def test_issuing_a_code_needs_a_real_moment(stand: Stand, fixed_secret: str) -> None:
    with pytest.raises(NaiveMomentError):
        await stand.service.issue_invite_code(
            MANAGER,
            role=MemberRole.STAFF,
            now=dt.datetime(2026, 8, 13, 9, 0),
        )


async def test_a_collision_is_redrawn(stand: Stand, monkeypatch: pytest.MonkeyPatch) -> None:
    stand.invites.add(
        venue_id=VENUE_ID,
        code=format_invite_code(VENUE_ID, SECRET),
        expires_at=NOW + INVITE_CODE_TTL,
    )
    secrets_in_turn: Iterator[str] = iter([SECRET, "B8M2NPQR"])
    monkeypatch.setattr(
        access_module,
        "new_invite_secret",
        lambda *_args, **_kwargs: next(secrets_in_turn),
    )

    code = await stand.service.issue_invite_code(MANAGER, role=MemberRole.STAFF, now=NOW)

    assert code.code == format_invite_code(VENUE_ID, "B8M2NPQR")


async def test_a_code_that_will_not_come_up_free_is_an_error_not_a_duplicate(
    stand: Stand,
    fixed_secret: str,
) -> None:
    stand.invites.add(
        venue_id=VENUE_ID,
        code=format_invite_code(VENUE_ID, SECRET),
        expires_at=NOW + INVITE_CODE_TTL,
    )

    with pytest.raises(InviteCodeGenerationError):
        await stand.service.issue_invite_code(MANAGER, role=MemberRole.STAFF, now=NOW)
    assert len(stand.invites.rows) == 1
    assert INVITE_CODE_ATTEMPTS >= 1


async def test_pending_codes_are_a_manager_screen(stand: Stand, fixed_secret: str) -> None:
    await stand.service.issue_invite_code(MANAGER, role=MemberRole.STAFF, now=NOW)

    assert len(await stand.service.list_pending_invite_codes(MANAGER)) == 1
    with pytest.raises(PermissionDeniedError):
        await stand.service.list_pending_invite_codes(STAFF)


async def test_revoking_a_code_records_the_fact_separately_from_the_expiry(
    stand: Stand,
    fixed_secret: str,
) -> None:
    code = await stand.service.issue_invite_code(MANAGER, role=MemberRole.STAFF, now=NOW)

    revoked = await stand.service.revoke_invite_code(MANAGER, code.id, now=NOW)

    assert revoked is not None
    assert revoked.revoked_at == NOW
    # The expiry is untouched: the two are different facts about the same code.
    assert revoked.expires_at == NOW + INVITE_CODE_TTL
    preview = await stand.service.preview_invite_code(code.code, now=NOW)
    assert preview.rejection is InviteRejection.REVOKED


async def test_a_code_of_another_venue_cannot_be_revoked(stand: Stand) -> None:
    foreign = stand.invites.add(
        venue_id=OTHER_VENUE_ID,
        code=format_invite_code(OTHER_VENUE_ID, SECRET),
        expires_at=NOW + INVITE_CODE_TTL,
    )

    assert await stand.service.revoke_invite_code(MANAGER, foreign.id, now=NOW) is None
    assert foreign.revoked_at is None


async def test_staff_cannot_revoke(stand: Stand, fixed_secret: str) -> None:
    code = await stand.service.issue_invite_code(MANAGER, role=MemberRole.STAFF, now=NOW)

    with pytest.raises(PermissionDeniedError):
        await stand.service.revoke_invite_code(STAFF, code.id, now=NOW)
    assert code.revoked_at is None


# --------------------------------------------------------------------------------------
# Preview: check the code without spending it (TZ 5.1)
# --------------------------------------------------------------------------------------


async def test_a_valid_code_previews_what_it_promises(stand: Stand) -> None:
    stand.invites.add(
        venue_id=VENUE_ID,
        code=format_invite_code(VENUE_ID, SECRET),
        role=MemberRole.STAFF,
        position="бармен",
        full_name="Анна Иванова",
        expires_at=NOW + INVITE_CODE_TTL,
    )

    preview = await stand.service.preview_invite_code(f"inv_1-{SECRET}", now=NOW)

    assert preview.is_valid is True
    assert preview.venue_id == VENUE_ID
    assert preview.role is MemberRole.STAFF
    assert preview.position == "бармен"
    assert preview.suggested_full_name == "Анна Иванова"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param(
            {"expires_at": NOW - dt.timedelta(seconds=1)},
            InviteRejection.EXPIRED,
            id="expired",
        ),
        pytest.param(
            {"expires_at": NOW, "revoked_at": None},
            InviteRejection.EXPIRED,
            id="expires exactly now",
        ),
        pytest.param(
            {"expires_at": NOW + INVITE_CODE_TTL, "revoked_at": NOW},
            InviteRejection.REVOKED,
            id="revoked",
        ),
        pytest.param(
            {"expires_at": NOW + INVITE_CODE_TTL, "used_at": NOW, "used_by": 42},
            InviteRejection.ALREADY_USED,
            id="already used",
        ),
    ],
)
async def test_a_code_that_no_longer_works_says_why(
    stand: Stand,
    kwargs: dict[str, Any],
    expected: InviteRejection,
) -> None:
    stand.invites.add(venue_id=VENUE_ID, code=format_invite_code(VENUE_ID, SECRET), **kwargs)

    preview = await stand.service.preview_invite_code(f"1-{SECRET}", now=NOW)

    assert preview.rejection is expected
    assert preview.is_valid is False


async def test_a_code_expiring_a_second_later_still_works(stand: Stand) -> None:
    stand.invites.add(
        venue_id=VENUE_ID,
        code=format_invite_code(VENUE_ID, SECRET),
        expires_at=NOW + dt.timedelta(seconds=1),
    )

    assert (await stand.service.preview_invite_code(f"1-{SECRET}", now=NOW)).is_valid is True


async def test_nonsense_and_unknown_codes_are_told_apart(stand: Stand) -> None:
    malformed = await stand.service.preview_invite_code("хочу войти", now=NOW)
    unknown = await stand.service.preview_invite_code(f"1-{SECRET}", now=NOW)

    assert malformed.rejection is InviteRejection.MALFORMED
    assert unknown.rejection is InviteRejection.UNKNOWN


async def test_a_secret_quoted_with_the_wrong_venue_finds_nothing(stand: Stand) -> None:
    """Acceptance 11.3: the lookup stays scoped even before there is a venue context."""
    stand.invites.add(
        venue_id=OTHER_VENUE_ID,
        code=format_invite_code(OTHER_VENUE_ID, SECRET),
        expires_at=NOW + INVITE_CODE_TTL,
    )

    stolen = await stand.service.preview_invite_code(f"{VENUE_ID}-{SECRET}", now=NOW)
    honest = await stand.service.preview_invite_code(f"{OTHER_VENUE_ID}-{SECRET}", now=NOW)

    assert stolen.rejection is InviteRejection.UNKNOWN
    assert honest.is_valid is True
    assert honest.venue_id == OTHER_VENUE_ID


# --------------------------------------------------------------------------------------
# Activation (TZ 5.1, decision B8)
# --------------------------------------------------------------------------------------


def seed_code(
    stand: Stand,
    *,
    venue_id: int = VENUE_ID,
    role: MemberRole = MemberRole.STAFF,
    position: str | None = "бармен",
    full_name: str | None = "Анна Иванова",
    expires_at: dt.datetime | None = None,
    **kwargs: Any,
) -> InviteCode:
    return stand.invites.add(
        venue_id=venue_id,
        code=format_invite_code(venue_id, SECRET),
        role=role,
        position=position,
        full_name=full_name,
        expires_at=expires_at if expires_at is not None else NOW + INVITE_CODE_TTL,
        **kwargs,
    )


async def test_activating_a_code_puts_the_person_into_the_venue(stand: Stand) -> None:
    code = seed_code(stand)

    activation = await stand.service.activate_invite_code(
        f"inv_{code.code}",
        telegram_id=NEWCOMER_TG,
        full_name="Анна Ивановна Иванова",
        now=NOW,
        username="anna",
    )

    assert activation.is_activated is True
    assert activation.venue_id == VENUE_ID
    assert activation.member is not None
    assert activation.member.role is MemberRole.STAFF
    assert activation.member.position == "бармен"
    assert activation.member.is_active is True
    assert activation.user is not None
    # The employee corrected the name the manager typed, and the correction is what is kept.
    assert activation.user.full_name == "Анна Ивановна Иванова"
    assert activation.user.username == "anna"
    # A single venue becomes the active one straight away.
    assert stand.users.active_venue_calls == [(activation.user.id, VENUE_ID)]
    # The code is spent.
    assert code.used_by == activation.user.id
    assert code.used_at == NOW


async def test_the_managers_name_is_used_when_the_employee_offers_none(stand: Stand) -> None:
    """Decision B8: the name comes from the manager, never from the Telegram profile."""
    code = seed_code(stand)

    activation = await stand.service.activate_invite_code(
        code.code,
        telegram_id=NEWCOMER_TG,
        full_name="",
        now=NOW,
    )

    assert activation.user is not None
    assert activation.user.full_name == "Анна Иванова"


async def test_a_code_works_exactly_once(stand: Stand) -> None:
    """TZ 5.1: single use. Typing it again is the case that has to be refused politely."""
    code = seed_code(stand)
    await stand.service.activate_invite_code(
        code.code,
        telegram_id=NEWCOMER_TG,
        full_name="Анна",
        now=NOW,
    )

    again = await stand.service.activate_invite_code(
        code.code,
        telegram_id=STAFF_TG,
        full_name="Кто-то другой",
        now=NOW + dt.timedelta(minutes=1),
    )

    assert again.rejection is InviteRejection.ALREADY_USED
    assert again.member is None
    assert len(stand.members.rows) == 1
    assert len(stand.users.rows) == 1


async def test_a_code_older_than_a_week_is_refused(stand: Stand) -> None:
    code = seed_code(stand, expires_at=NOW + INVITE_CODE_TTL)

    late = await stand.service.activate_invite_code(
        code.code,
        telegram_id=NEWCOMER_TG,
        full_name="Анна",
        now=NOW + INVITE_CODE_TTL + dt.timedelta(seconds=1),
    )

    assert late.rejection is InviteRejection.EXPIRED
    assert late.venue_id == VENUE_ID
    assert stand.members.rows == []
    assert stand.users.rows == []


async def test_a_revoked_code_is_refused(stand: Stand) -> None:
    code = seed_code(stand, revoked_at=NOW)

    activation = await stand.service.activate_invite_code(
        code.code,
        telegram_id=NEWCOMER_TG,
        full_name="Анна",
        now=NOW + dt.timedelta(minutes=1),
    )

    assert activation.rejection is InviteRejection.REVOKED
    assert stand.members.rows == []


async def test_nonsense_typed_instead_of_a_code_is_refused(stand: Stand) -> None:
    activation = await stand.service.activate_invite_code(
        "хочу войти",
        telegram_id=NEWCOMER_TG,
        full_name="Анна",
        now=NOW,
    )

    assert activation.rejection is InviteRejection.MALFORMED
    assert activation.venue_id is None


async def test_a_code_of_another_venue_does_not_open_this_one(stand: Stand) -> None:
    seed_code(stand, venue_id=OTHER_VENUE_ID)

    stolen = await stand.service.activate_invite_code(
        format_invite_code(VENUE_ID, SECRET),
        telegram_id=NEWCOMER_TG,
        full_name="Анна",
        now=NOW,
    )

    assert stolen.rejection is InviteRejection.UNKNOWN
    assert stand.members.rows == []


async def test_a_returning_employee_keeps_the_row_that_carries_their_history(
    stand: Stand,
) -> None:
    """TZ 5.1: deactivation must not lose history, so activation must not create a twin."""
    user, member = seed_member(
        stand,
        telegram_id=STAFF_TG,
        role=MemberRole.STAFF,
        member_active=False,
    )
    code = seed_code(stand, role=MemberRole.STAFF, position="старший бармен")

    activation = await stand.service.activate_invite_code(
        code.code,
        telegram_id=STAFF_TG,
        full_name="Анна",
        now=NOW,
    )

    assert activation.is_activated is True
    assert activation.was_reinstated is True, "the row was here all along and came back on"
    assert len(stand.members.rows) == 1
    assert activation.member is not None
    assert activation.member.id == member.id
    assert activation.member.is_active is True
    assert activation.member.position == "старший бармен"
    assert activation.user is not None
    assert activation.user.id == user.id


async def test_an_active_member_typing_a_fresh_code_is_refused_and_the_code_survives(
    stand: Stand,
) -> None:
    """The incident this exists for, in five lines.

    An owner opened the invite link he had just issued — to check that it opened. The
    single use was spent on him, and the colleague he had already forwarded it to was told
    the code had been used. Nobody could see why: the code looked fine on the manager's
    screen right up to the moment it did not exist.

    So the refusal must leave the code **exactly** as it found it. `used_at` and `used_by`
    are asserted, not just the outcome — the outcome could be right while the row is
    already burnt.
    """
    _, member = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.MANAGER)
    code = seed_code(stand, role=MemberRole.STAFF)

    activation = await stand.service.activate_invite_code(
        code.code,
        telegram_id=STAFF_TG,
        full_name="Кто-то другой",
        now=NOW,
    )

    assert activation.is_activated is False
    assert activation.rejection is InviteRejection.ALREADY_MEMBER
    assert code.used_at is None, "the invitation belongs to somebody else and is still live"
    assert code.used_by is None
    assert member.role is MemberRole.MANAGER, "and the role it did not ask about is untouched"


async def test_the_refusal_reaches_the_person_before_they_confirm_anything(
    stand: Stand,
) -> None:
    """Preview refuses too, so nobody walks three screens to be turned away at the end."""
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)
    code = seed_code(stand)

    preview = await stand.service.preview_invite_code(
        code.code,
        now=NOW,
        telegram_id=STAFF_TG,
    )

    assert preview.rejection is InviteRejection.ALREADY_MEMBER
    assert preview.is_valid is False


async def test_a_stranger_still_gets_the_code_the_preview_promised(stand: Stand) -> None:
    """The new refusal is aimed at one case and must not catch anybody else."""
    code = seed_code(stand)

    preview = await stand.service.preview_invite_code(code.code, now=NOW, telegram_id=STAFF_TG)

    assert preview.is_valid is True


async def test_a_member_of_another_venue_is_not_an_existing_member_here(stand: Stand) -> None:
    """TZ 3.3: the question is asked through this venue's repository, not about the person.

    Somebody who works next door is a stranger here, and the invitation is genuinely theirs
    to use — refusing it would make a second venue impossible to join.
    """
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF, venue_id=OTHER_VENUE_ID)
    code = seed_code(stand)

    preview = await stand.service.preview_invite_code(code.code, now=NOW, telegram_id=STAFF_TG)
    activation = await stand.service.activate_invite_code(
        code.code,
        telegram_id=STAFF_TG,
        full_name="Анна",
        now=NOW,
    )

    assert preview.is_valid is True
    assert activation.is_activated is True


async def test_a_dismissed_manager_is_not_demoted_by_the_code_that_brings_them_back(
    stand: Stand,
) -> None:
    """TZ 2 and TZ 5.1: a code adds access and never takes any away.

    The returning employee keeps the row carrying their history, and the role is the higher
    of the two — otherwise handing a returning manager an ordinary staff code would quietly
    strip them, which is the same defect that demoted the owner.
    """
    _, member = seed_member(
        stand,
        telegram_id=STAFF_TG,
        role=MemberRole.MANAGER,
        member_active=False,
    )
    code = seed_code(stand, role=MemberRole.STAFF)

    activation = await stand.service.activate_invite_code(
        code.code,
        telegram_id=STAFF_TG,
        full_name="Анна",
        now=NOW,
    )

    assert activation.is_activated is True
    assert member.is_active is True
    assert member.role is MemberRole.MANAGER


async def test_a_code_never_renames_somebody_the_system_already_knows(stand: Stand) -> None:
    """`users.full_name` is global, so a code carrying another name rewrites every venue.

    Observed live: the owner's name flipped between two codes he opened himself. Correcting
    a known person's name is the manager's own act (decision B8, `MemberService.rename`).
    """
    user, _ = seed_member(
        stand,
        telegram_id=STAFF_TG,
        role=MemberRole.STAFF,
        full_name="Анна Иванова",
        member_active=False,
    )
    code = seed_code(stand, full_name="Мария Сидорова")

    await stand.service.activate_invite_code(
        code.code,
        telegram_id=STAFF_TG,
        full_name="Мария Сидорова",
        now=NOW,
    )

    assert user.full_name == "Анна Иванова"


async def test_activation_does_not_move_somebody_who_already_chose_a_venue(
    stand: Stand,
) -> None:
    user = stand.users.add(
        telegram_id=STAFF_TG,
        full_name="Анна",
        active_venue_id=OTHER_VENUE_ID,
    )
    stand.members.add(venue_id=OTHER_VENUE_ID, user_id=user.id, role=MemberRole.STAFF)
    code = seed_code(stand)

    await stand.service.activate_invite_code(
        code.code,
        telegram_id=STAFF_TG,
        full_name="Анна",
        now=NOW,
    )

    assert stand.users.active_venue_calls == []
    assert user.active_venue_id == OTHER_VENUE_ID


# --------------------------------------------------------------------------------------
# Who "a notification to the manager" means — answer A4, in one place
# --------------------------------------------------------------------------------------


async def test_the_managers_of_the_venue_are_the_recipients(stand: Stand) -> None:
    seed_member(stand, telegram_id=MANAGER_TG, role=MemberRole.MANAGER, full_name="Виктор")
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF, full_name="Анна")
    seed_member(stand, telegram_id=BOOTSTRAP_TG, role=MemberRole.OWNER, full_name="Олег")

    recipients = await stand.service.manager_recipients(VENUE_ID)

    assert [member.role for member in recipients] == [MemberRole.MANAGER]
    assert await stand.service.manager_recipient_ids(VENUE_ID) == (recipients[0].user_id,)


async def test_without_managers_the_owners_are_the_recipients(stand: Stand) -> None:
    """Answer A4, second half: a venue on stage 0 has an owner and no manager at all."""
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)
    owner, _ = seed_member(stand, telegram_id=MANAGER_TG, role=MemberRole.OWNER)

    assert await stand.service.manager_recipient_ids(VENUE_ID) == (owner.id,)


async def test_a_deactivated_manager_does_not_receive_and_hands_over_to_the_owner(
    stand: Stand,
) -> None:
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.MANAGER, member_active=False)
    owner, _ = seed_member(stand, telegram_id=MANAGER_TG, role=MemberRole.OWNER)

    assert await stand.service.manager_recipient_ids(VENUE_ID) == (owner.id,)


async def test_a_venue_with_nobody_to_tell_returns_nobody(stand: Stand) -> None:
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)

    assert await stand.service.manager_recipients(VENUE_ID) == ()
    assert await stand.service.manager_recipient_ids(VENUE_ID) == ()


async def test_recipients_never_cross_venues(stand: Stand) -> None:
    mine, _ = seed_member(stand, telegram_id=MANAGER_TG, role=MemberRole.MANAGER)
    theirs, _ = seed_member(
        stand,
        telegram_id=STAFF_TG,
        role=MemberRole.MANAGER,
        venue_id=OTHER_VENUE_ID,
    )

    assert await stand.service.manager_recipient_ids(VENUE_ID) == (mine.id,)
    assert await stand.service.manager_recipient_ids(OTHER_VENUE_ID) == (theirs.id,)


# --------------------------------------------------------------------------------------
# The fakes themselves, held to the statements they stand for
# --------------------------------------------------------------------------------------
#
# Every test above that says "another venue's row is unreachable" rests on the predicate
# inside these two fakes. Asserted here directly, because a fake that quietly lost it would
# leave all of them green and the leak visible in production only (decision D9, TZ 3.3).


async def test_the_scoped_fakes_carry_the_venue_in_every_where(stand: Stand) -> None:
    user, theirs = seed_member(
        stand,
        telegram_id=STAFF_TG,
        role=MemberRole.STAFF,
        venue_id=OTHER_VENUE_ID,
    )
    foreign = stand.invites.add(
        venue_id=OTHER_VENUE_ID,
        code=format_invite_code(OTHER_VENUE_ID, SECRET),
        expires_at=NOW + INVITE_CODE_TTL,
    )
    members = stand.repositories.members(VENUE_ID)
    invites = stand.repositories.invites(VENUE_ID)

    assert await members.get(theirs.id) is None
    assert await members.get_for_user(user.id) is None
    assert list(await members.list_active()) == []
    assert list(await members.list_by_role(MemberRole.STAFF)) == []
    assert await members.update(theirs.id, position="бармен") is None
    assert await members.set_active(theirs.id, is_active=False) is None
    assert await invites.get_by_code(foreign.code) is None
    assert list(await invites.list_pending()) == []
    assert await invites.mark_used(foreign.id, used_by=user.id, used_at=NOW) is None
    assert await invites.revoke(foreign.id, NOW) is None

    # Hidden by the predicate, not by an empty table: its own venue reads all of it back.
    assert await stand.repositories.members(OTHER_VENUE_ID).get(theirs.id) is theirs
    neighbour = stand.repositories.invites(OTHER_VENUE_ID)
    assert await neighbour.get_by_code(foreign.code) is foreign
    assert (theirs.is_active, theirs.position) == (True, None)
    assert (foreign.used_at, foreign.revoked_at) == (None, None)


async def test_the_fakes_refuse_a_column_the_table_does_not_have(stand: Stand) -> None:
    """`_apply` in both real repositories: a misspelt field is a typo, not a silent no-op."""
    user, member = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)

    with pytest.raises(AttributeError):
        await stand.repositories.members(VENUE_ID).update(member.id, positon="бармен")
    with pytest.raises(AttributeError):
        await stand.repositories.users.update(user.id, fullname="Анна Иванова")

    assert member.position is None
    assert user.full_name == "Анна"


def test_this_service_is_the_recipient_resolver_the_queue_asks(stand: Stand) -> None:
    """The other end of answer A4, checked by mypy as much as by pytest.

    `notifications.py` declares `ManagerRecipients` — a hole exactly one method wide — so
    that the rule is not restated where notifications are written. This is the assertion
    that the hole and this service still fit: the annotation is the test, and a signature
    that drifted on either side would fail the type check rather than at run time.
    """
    resolver: ManagerRecipients = stand.service
    assert resolver is stand.service


# --------------------------------------------------------------------------------------
# The trail of an activation (TZ 2, decision B13)
# --------------------------------------------------------------------------------------
#
# Until these existed, the most sensitive change in the product left no trace at all. Who
# became a manager, when, and on whose invitation was answerable only from
# `invite_codes.used_by`; a role overwritten by an activation was answerable by nothing.
# `MemberService` has recorded its own edits since it was written — this is the same events
# arriving by the other road, and they were the road that was missing.


async def test_joining_a_venue_is_written_down(stand: Stand) -> None:
    code = seed_code(stand, role=MemberRole.MANAGER, position="бармен")

    activation = await stand.service.activate_invite_code(
        code.code,
        telegram_id=STAFF_TG,
        full_name="Анна",
        now=NOW,
    )

    assert activation.member is not None
    entries = stand.audit.entries(VENUE_ID)
    membership = next(entry for entry in entries if entry["entity"] == "venue_members")
    assert membership["action"] == "create"
    assert membership["entity_id"] == activation.member.id
    assert membership["diff"]["role"]["to"] == MemberRole.MANAGER
    assert membership["user_id"] == activation.member.user_id, (
        "audit_log.user_id is a users.id and never a telegram_id"
    )


async def test_spending_a_code_is_written_down(stand: Stand) -> None:
    """Who used which invitation, and when — the question `used_by` alone cannot answer."""
    code = seed_code(stand)

    await stand.service.activate_invite_code(
        code.code,
        telegram_id=STAFF_TG,
        full_name="Анна",
        now=NOW,
    )

    spent = next(
        entry for entry in stand.audit.entries(VENUE_ID) if entry["entity"] == "invite_codes"
    )
    assert spent["entity_id"] == code.id
    assert spent["diff"]["used_by"]["to"] == code.used_by


async def test_a_refused_activation_writes_nothing_anywhere(stand: Stand) -> None:
    """The refusal of an active member must leave the venue exactly as it found it."""
    seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)
    code = seed_code(stand)

    await stand.service.activate_invite_code(
        code.code,
        telegram_id=STAFF_TG,
        full_name="Анна",
        now=NOW,
    )

    assert stand.audit.rows == []


async def test_the_trail_of_one_venue_does_not_land_in_another(stand: Stand) -> None:
    """TZ 3.3: the sink is built for a venue, so the row cannot be attributed elsewhere."""
    code = seed_code(stand, venue_id=OTHER_VENUE_ID)

    await stand.service.activate_invite_code(
        code.code,
        telegram_id=STAFF_TG,
        full_name="Анна",
        now=NOW,
    )

    assert stand.audit.entries(VENUE_ID) == []
    assert len(stand.audit.entries(OTHER_VENUE_ID)) == 2


# --------------------------------------------------------------------------------------
# Rebinding a card to another Telegram account (TZ 5.1)
# --------------------------------------------------------------------------------------
#
# The gap this closes: somebody changes their Telegram account. An ordinary invite created a
# *second* `users` row and a second membership — two identical names in the roster — and
# every shift, checklist and write-off stayed on the first one, because they hang off
# `users.id`. Moving `telegram_id` instead means the history never moves at all.


def seed_rebind_code(stand: Stand, member: VenueMember, **kwargs: Any) -> InviteCode:
    return seed_code(
        stand,
        purpose=InvitePurpose.REBIND,
        issued_to_member_id=member.id,
        **kwargs,
    )


async def test_rebinding_moves_the_account_and_nothing_else(stand: Stand) -> None:
    boss = OWNER
    user, member = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.MANAGER)
    code = await stand.service.issue_rebind_code(boss, member_id=member.id, now=NOW)

    outcome = await stand.service.activate_rebind_code(
        code.code,
        telegram_id=NEW_TG,
        now=NOW,
    )

    assert outcome.is_rebound
    assert user.telegram_id == NEW_TG, "the account moved"
    assert user.id == member.user_id, "and the person did not"
    assert member.role is MemberRole.MANAGER
    assert len(stand.members.rows) == 1, "no second membership appeared"


async def test_the_old_account_stops_opening_the_card(stand: Stand) -> None:
    """The point of moving `telegram_id`: a lost or stolen account loses access."""
    boss = OWNER
    _, member = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)
    code = await stand.service.issue_rebind_code(boss, member_id=member.id, now=NOW)

    await stand.service.activate_rebind_code(code.code, telegram_id=NEW_TG, now=NOW)
    identity = await stand.service.resolve(STAFF_TG)

    assert identity.contexts == ()


async def test_an_account_that_is_somebody_else_is_refused_and_the_code_survives(
    stand: Stand,
) -> None:
    """Whose card is this? The manager has to answer that, not receive a fresh code."""
    boss = OWNER
    _, member = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)
    seed_member(stand, telegram_id=NEW_TG, role=MemberRole.STAFF, full_name="Другой")
    code = await stand.service.issue_rebind_code(boss, member_id=member.id, now=NOW)

    outcome = await stand.service.activate_rebind_code(code.code, telegram_id=NEW_TG, now=NOW)

    assert outcome.rejection is InviteRejection.ACCOUNT_TAKEN
    assert code.used_at is None, "the manager still has a live code to work with"


async def test_a_switched_off_card_has_nothing_to_point_at(stand: Stand) -> None:
    boss = OWNER
    _, member = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)
    code = await stand.service.issue_rebind_code(boss, member_id=member.id, now=NOW)
    member.is_active = False

    outcome = await stand.service.activate_rebind_code(code.code, telegram_id=NEW_TG, now=NOW)

    assert outcome.rejection is InviteRejection.CARD_UNAVAILABLE
    assert code.used_at is None


async def test_a_joining_code_is_not_a_rebind_code(stand: Stand) -> None:
    """The two are told apart by the column and never by what they look like."""
    code = seed_code(stand)

    outcome = await stand.service.activate_rebind_code(code.code, telegram_id=NEW_TG, now=NOW)

    assert outcome.rejection is InviteRejection.UNKNOWN


async def test_a_rebind_code_is_not_a_joining_code(stand: Stand) -> None:
    boss = OWNER
    _, member = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)
    code = await stand.service.issue_rebind_code(boss, member_id=member.id, now=NOW)

    activation = await stand.service.activate_invite_code(
        code.code,
        telegram_id=NEW_TG,
        full_name="Кто-то",
        now=NOW,
    )

    assert activation.is_activated is False


async def test_a_manager_does_not_move_the_owners_account(stand: Stand) -> None:
    """TZ 2: taking over the owner's card is the owner's business."""
    manager = MANAGER
    _, chief = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.OWNER)

    with pytest.raises(PermissionDeniedError):
        await stand.service.issue_rebind_code(manager, member_id=chief.id, now=NOW)


async def test_a_card_of_another_venue_cannot_be_rebound(stand: Stand) -> None:
    """Acceptance 11.3: a forged id dies on the scope before anything else looks at it."""
    boss = OWNER
    _, theirs = seed_member(
        stand, telegram_id=STAFF_TG, role=MemberRole.STAFF, venue_id=OTHER_VENUE_ID
    )

    with pytest.raises(UnknownMembershipError):
        await stand.service.issue_rebind_code(boss, member_id=theirs.id, now=NOW)


async def test_the_move_is_written_down(stand: Stand) -> None:
    """TZ 2 and TZ 9: `telegram_id` is the key to somebody's access, so a move is logged."""
    boss = OWNER
    _, member = seed_member(stand, telegram_id=STAFF_TG, role=MemberRole.STAFF)
    code = await stand.service.issue_rebind_code(boss, member_id=member.id, now=NOW)

    await stand.service.activate_rebind_code(code.code, telegram_id=NEW_TG, now=NOW)

    moved = next(
        entry
        for entry in stand.audit.entries(VENUE_ID)
        if entry["entity"] == "venue_members" and entry["action"] == "update"
    )
    assert moved["diff"]["telegram_id"] == {"from": STAFF_TG, "to": NEW_TG}
