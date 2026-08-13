"""The employee roster of one venue (plan, task 27; TZ 2, 5.1, 5.8).

Fake repositories, like everywhere in this package: what is under test is a set of rules —
who may change whom, and what survives a dismissal — not a set of queries.

Two properties of the fakes below carry weight and are worth stating before the tests use
them silently.

* **They are venue-scoped exactly as the contract says.** `FakeMembers` is built for one
  venue and filters the shared store by it, so a membership of another venue is a row that
  *exists and is unreachable* rather than a row nobody wrote (TZ 3.3, acceptance 11.3). That
  is what makes "a forged callback_data from the neighbouring bar" a real test here instead
  of a tautology: without the scope the same id would resolve happily.
* **`update` mutates the stored row in place**, the way SQLAlchemy's identity map does. It
  matters: a service that reads a field *after* writing it reads back what it just wrote,
  and that is precisely the class of bug this style of fake is able to catch. It writes
  through :func:`apply_fields` — the `_apply` of `src/db/repositories/members.py` — so an
  unknown column raises instead of quietly growing an instance attribute the table has no
  room for.

Nothing here touches `users.is_bot_blocked` — TZ 6 says the flag is written by the delivery
worker and only read on this screen, and the tests assert that reading, not a write.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from src.db.models import MemberRole, User, VenueMember
from src.services.access import AccessContext, PermissionDeniedError, VenueMismatchError
from src.services.members import (
    MemberError,
    MemberNotFoundError,
    MemberRepositories,
    MemberService,
    RosterEntry,
)

VENUE_ID = 1
OTHER_VENUE_ID = 2


# --------------------------------------------------------------------------------------
# Stores (declared here on purpose: tests/factories.py belongs to task 5a)
# --------------------------------------------------------------------------------------


def apply_fields(row: object, fields: Mapping[str, Any]) -> None:
    """`_apply` of `src/db/repositories/members.py` and `users.py`, character for character.

    A bare `setattr` loop is the generous version: `update(member_id, positon="bar")` would
    grow an instance attribute, the fake would answer with a row that looks updated, and the
    same call would fail against PostgreSQL. An unknown column is a typo and says so here.
    """
    for name, value in fields.items():
        if not hasattr(type(row), name):
            raise AttributeError(f"{type(row).__name__} has no column {name!r}")
        setattr(row, name, value)


class UserStore:
    def __init__(self) -> None:
        self.rows: list[User] = []
        self._next_id = 1

    def add(
        self,
        *,
        full_name: str,
        telegram_id: int,
        is_active: bool = True,
        is_bot_blocked: bool = False,
    ) -> User:
        user = User(
            id=self._next_id,
            telegram_id=telegram_id,
            full_name=full_name,
            username=None,
            is_active=is_active,
            active_venue_id=None,
            is_bot_blocked=is_bot_blocked,
        )
        self._next_id += 1
        self.rows.append(user)
        return user


class FakeUsers:
    """`UserRepository`, global: the name of a person is not the property of a venue."""

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
        return self.store.add(telegram_id=telegram_id, full_name=full_name)

    async def update(self, user_id: int, **fields: Any) -> User | None:
        row = await self.get(user_id)
        if row is None:
            return None
        apply_fields(row, fields)
        return row

    async def touch_last_seen(self, user_id: int, moment: Any) -> None:
        row = await self.get(user_id)
        if row is not None:
            row.last_seen_at = moment

    async def set_active_venue(self, user_id: int, venue_id: int | None) -> None:
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
    """`VenueMemberRepository`, scoped to one venue and to nothing else (TZ 3.3)."""

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


class FakeRepositories:
    """The provider `MemberService` asks for: one global table and one scoped factory."""

    def __init__(self, user_store: UserStore, member_store: MemberStore) -> None:
        self.user_store = user_store
        self.member_store = member_store
        self._users = FakeUsers(user_store)
        #: Every venue whose repository was ever built, in order — the scoping assertion.
        self.scopes: list[int] = []

    @property
    def users(self) -> FakeUsers:
        return self._users

    def members(self, venue_id: int) -> FakeMembers:
        self.scopes.append(venue_id)
        return FakeMembers(self.member_store, venue_id)


# --------------------------------------------------------------------------------------
# Test stand
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Stand:
    repositories: FakeRepositories
    service: MemberService

    @property
    def users(self) -> UserStore:
        return self.repositories.user_store

    @property
    def members(self) -> MemberStore:
        return self.repositories.member_store


@pytest.fixture
def stand() -> Stand:
    repositories = FakeRepositories(UserStore(), MemberStore())
    return Stand(repositories=repositories, service=MemberService(repositories))


_TELEGRAM_IDS = iter(range(700_000_000, 700_001_000))


def seed(
    stand: Stand,
    *,
    full_name: str,
    role: MemberRole = MemberRole.STAFF,
    position: str | None = None,
    venue_id: int = VENUE_ID,
    member_active: bool = True,
    bot_blocked: bool = False,
) -> tuple[User, VenueMember]:
    user = stand.users.add(
        full_name=full_name,
        telegram_id=next(_TELEGRAM_IDS),
        is_bot_blocked=bot_blocked,
    )
    member = stand.members.add(
        venue_id=venue_id,
        user_id=user.id,
        role=role,
        position=position,
        is_active=member_active,
    )
    return user, member


def actor(user: User, member: VenueMember) -> AccessContext:
    return AccessContext(
        user_id=user.id,
        telegram_id=user.telegram_id,
        venue_id=member.venue_id,
        member_id=member.id,
        role=member.role,
        full_name=user.full_name,
        position=member.position,
    )


def manager_of(stand: Stand, venue_id: int = VENUE_ID) -> AccessContext:
    return actor(*seed(stand, full_name="Mira", role=MemberRole.MANAGER, venue_id=venue_id))


def owner_of(stand: Stand, venue_id: int = VENUE_ID) -> AccessContext:
    return actor(*seed(stand, full_name="Oleg", role=MemberRole.OWNER, venue_id=venue_id))


def staff_of(stand: Stand, venue_id: int = VENUE_ID) -> AccessContext:
    return actor(*seed(stand, full_name="Sam", role=MemberRole.STAFF, venue_id=venue_id))


# --------------------------------------------------------------------------------------
# Reading the roster (TZ 5.8)
# --------------------------------------------------------------------------------------


async def test_the_roster_is_the_active_members_sorted_by_name(stand: Stand) -> None:
    boss = manager_of(stand)
    seed(stand, full_name="Zoya")
    seed(stand, full_name="anna")
    seed(stand, full_name="Boris")

    names = [entry.full_name for entry in await stand.service.list_roster(boss)]

    # Case-folded, so a name typed in lower case does not fall to the end of the list.
    assert names == ["anna", "Boris", "Mira", "Zoya"]


async def test_a_dismissed_employee_leaves_the_roster_but_not_the_table(stand: Stand) -> None:
    """TZ 5.1: `is_active = false` hides the person from the screen and keeps their row."""
    boss = manager_of(stand)
    _, gone = seed(stand, full_name="Nina", member_active=False)

    assert [entry.full_name for entry in await stand.service.list_roster(boss)] == ["Mira"]
    assert gone in stand.members.rows


async def test_the_roster_never_reaches_into_another_venue(stand: Stand) -> None:
    boss = manager_of(stand)
    seed(stand, full_name="Neighbour", venue_id=OTHER_VENUE_ID)

    assert [entry.full_name for entry in await stand.service.list_roster(boss)] == ["Mira"]
    assert stand.repositories.scopes == [VENUE_ID]


async def test_the_roster_of_a_brand_new_venue_is_empty_and_not_an_error(stand: Stand) -> None:
    """TZ 8.1: the empty state is the ordinary case, so it has to be a plain empty tuple.

    The actor is built by hand rather than seeded: a venue on its first minute has no
    `venue_members` rows at all, not even the one belonging to the person looking at it,
    because the wizard writes the roster after the venue (plan, task 26).
    """
    boss = AccessContext(
        user_id=1,
        telegram_id=1,
        venue_id=OTHER_VENUE_ID,
        member_id=1,
        role=MemberRole.OWNER,
        full_name="Oleg",
    )

    assert await stand.service.list_roster(boss) == ()


async def test_a_blocked_bot_is_read_from_the_user_row(stand: Stand) -> None:
    """TZ 6: the manager sees an undelivered notification as a mark in the list."""
    boss = manager_of(stand)
    seed(stand, full_name="Pavel", bot_blocked=True)

    roster = await stand.service.list_roster(boss)
    marks = {entry.full_name: entry.is_bot_blocked for entry in roster}

    assert marks == {"Mira": False, "Pavel": True}


async def test_a_membership_without_a_user_row_still_renders(stand: Stand) -> None:
    """A row whose user is gone must not take the whole screen down with it (TZ 8.1)."""
    boss = manager_of(stand)
    stand.members.add(venue_id=VENUE_ID, user_id=9_999, role=MemberRole.STAFF)

    entries = await stand.service.list_roster(boss)

    assert [entry.full_name for entry in entries] == ["", "Mira"]
    assert entries[0].is_bot_blocked is False


async def test_listing_by_role_answers_who_may_manage_the_venue(stand: Stand) -> None:
    boss = owner_of(stand)
    manager, _ = seed(stand, full_name="Mira", role=MemberRole.MANAGER)
    seed(stand, full_name="Ex", role=MemberRole.MANAGER, member_active=False)
    seed(stand, full_name="Sam")

    found = await stand.service.list_by_role(boss, MemberRole.MANAGER)

    assert [entry.user_id for entry in found] == [manager.id]


async def test_the_roster_is_manager_only(stand: Stand) -> None:
    with pytest.raises(PermissionDeniedError):
        await stand.service.list_roster(staff_of(stand))
    with pytest.raises(PermissionDeniedError):
        await stand.service.list_by_role(staff_of(stand), MemberRole.STAFF)


async def test_who_a_shift_may_be_given_to_is_readable_by_staff(stand: Stand) -> None:
    """TZ 5.3: "who else is on this shift" is a staff screen; only writing is manager-only."""
    hand = staff_of(stand)
    seed(stand, full_name="Nina")
    seed(stand, full_name="Gone", member_active=False)

    assert [entry.full_name for entry in await stand.service.list_assignable(hand)] == [
        "Nina",
        "Sam",
    ]


async def test_one_line_is_fetched_by_id_and_a_foreign_id_is_simply_absent(stand: Stand) -> None:
    boss = manager_of(stand)
    _, mine = seed(stand, full_name="Nina")
    _, theirs = seed(stand, full_name="Neighbour", venue_id=OTHER_VENUE_ID)

    found = await stand.service.get(boss, mine.id)

    assert found is not None
    assert found.full_name == "Nina"
    assert await stand.service.get(boss, theirs.id) is None


# --------------------------------------------------------------------------------------
# Lifecycle: the row outlives the employment (TZ 5.1)
# --------------------------------------------------------------------------------------


async def test_deactivation_flips_a_flag_and_deletes_nothing(stand: Stand) -> None:
    boss = manager_of(stand)
    _, member = seed(stand, full_name="Nina", position="bartender")

    entry = await stand.service.deactivate(boss, member.id)

    assert entry.is_active is False
    assert len(stand.members.rows) == 2
    assert stand.members.rows[-1].position == "bartender"


async def test_a_returning_employee_gets_the_old_row_back(stand: Stand) -> None:
    boss = manager_of(stand)
    _, member = seed(stand, full_name="Nina", member_active=False)

    entry = await stand.service.reactivate(boss, member.id)

    assert entry.member_id == member.id
    assert entry.is_active is True
    assert len(stand.members.rows) == 2


async def test_a_membership_of_another_venue_cannot_be_deactivated(stand: Stand) -> None:
    """Acceptance 11.3: a forged `callback_data` dies on the scope, not on a later check."""
    boss = manager_of(stand)
    _, theirs = seed(stand, full_name="Neighbour", venue_id=OTHER_VENUE_ID)

    with pytest.raises(MemberNotFoundError):
        await stand.service.deactivate(boss, theirs.id)

    assert theirs.is_active is True


async def test_deactivation_is_manager_only(stand: Stand) -> None:
    hand = staff_of(stand)
    _, member = seed(stand, full_name="Nina")

    with pytest.raises(PermissionDeniedError):
        await stand.service.deactivate(hand, member.id)
    with pytest.raises(PermissionDeniedError):
        await stand.service.reactivate(hand, member.id)

    assert member.is_active is True


# --------------------------------------------------------------------------------------
# Roles and positions (TZ 2)
# --------------------------------------------------------------------------------------


async def test_only_the_owner_hands_out_the_manager_role(stand: Stand) -> None:
    boss = manager_of(stand)
    _, member = seed(stand, full_name="Nina")

    with pytest.raises(PermissionDeniedError):
        await stand.service.set_role(boss, member.id, MemberRole.MANAGER)

    assert member.role is MemberRole.STAFF


async def test_only_the_owner_takes_the_manager_role_away(stand: Stand) -> None:
    """The demotion is checked on the *current* role too, or a manager could unseat one."""
    boss = manager_of(stand)
    _, other = seed(stand, full_name="Nina", role=MemberRole.MANAGER)

    with pytest.raises(PermissionDeniedError):
        await stand.service.set_role(boss, other.id, MemberRole.STAFF)

    assert other.role is MemberRole.MANAGER


async def test_the_owner_may_promote_and_demote(stand: Stand) -> None:
    boss = owner_of(stand)
    _, member = seed(stand, full_name="Nina")

    promoted = await stand.service.set_role(boss, member.id, MemberRole.MANAGER)
    assert promoted.role is MemberRole.MANAGER

    demoted = await stand.service.set_role(boss, member.id, MemberRole.STAFF)
    assert demoted.role is MemberRole.STAFF


async def test_a_manager_may_move_people_around_inside_staff(stand: Stand) -> None:
    boss = manager_of(stand)
    _, member = seed(stand, full_name="Nina")

    entry = await stand.service.set_role(boss, member.id, MemberRole.STAFF)

    assert entry.role is MemberRole.STAFF


async def test_a_role_change_stops_at_the_venue_border(stand: Stand) -> None:
    boss = owner_of(stand)
    _, theirs = seed(stand, full_name="Neighbour", role=MemberRole.STAFF, venue_id=OTHER_VENUE_ID)

    with pytest.raises(MemberNotFoundError):
        await stand.service.set_role(boss, theirs.id, MemberRole.MANAGER)

    assert theirs.role is MemberRole.STAFF


async def test_a_position_is_free_text_and_may_be_cleared(stand: Stand) -> None:
    """TZ 5.1: the venue names its own positions, so nothing here is a closed list."""
    boss = manager_of(stand)
    _, member = seed(stand, full_name="Nina")

    assert (await stand.service.set_position(boss, member.id, "head bartender")).position == (
        "head bartender"
    )
    assert (await stand.service.set_position(boss, member.id, None)).position is None


async def test_a_position_cannot_be_set_across_venues(stand: Stand) -> None:
    boss = manager_of(stand)
    _, theirs = seed(stand, full_name="Neighbour", venue_id=OTHER_VENUE_ID)

    with pytest.raises(MemberNotFoundError):
        await stand.service.set_position(boss, theirs.id, "head bartender")


# --------------------------------------------------------------------------------------
# The name lives on `users`, which is global (decision B8)
# --------------------------------------------------------------------------------------


async def test_renaming_writes_the_global_user_row(stand: Stand) -> None:
    boss = manager_of(stand)
    user, member = seed(stand, full_name="Nina")

    entry = await stand.service.rename(boss, member.id, "  Nina Petrova  ")

    assert entry.full_name == "Nina Petrova"
    assert user.full_name == "Nina Petrova"


async def test_an_empty_name_is_refused_rather_than_written(stand: Stand) -> None:
    boss = manager_of(stand)
    user, member = seed(stand, full_name="Nina")

    with pytest.raises(MemberError):
        await stand.service.rename(boss, member.id, "   ")

    assert user.full_name == "Nina"


async def test_a_manager_cannot_rename_a_stranger(stand: Stand) -> None:
    """The membership is resolved in the scoped repository first, and that is the guard.

    Without it the global `users` table would be reachable from any venue by user id — the
    one place in this module where a scope slip would cross tenants (TZ 3.3, TZ 9).
    """
    boss = manager_of(stand)
    stranger, theirs = seed(stand, full_name="Neighbour", venue_id=OTHER_VENUE_ID)

    with pytest.raises(MemberNotFoundError):
        await stand.service.rename(boss, theirs.id, "Renamed")

    assert stranger.full_name == "Neighbour"


async def test_renaming_is_manager_only(stand: Stand) -> None:
    hand = staff_of(stand)
    user, member = seed(stand, full_name="Nina")

    with pytest.raises(PermissionDeniedError):
        await stand.service.rename(hand, member.id, "Nina Petrova")

    assert user.full_name == "Nina"


# --------------------------------------------------------------------------------------
# The seams
# --------------------------------------------------------------------------------------


def test_the_fakes_are_the_contract_the_service_asks_for(stand: Stand) -> None:
    """Checked by mypy as much as by pytest: the fakes satisfy `MemberRepositories`.

    The annotation is the assertion. If the provider protocol or the repository contract in
    `src/db/repositories/protocols.py` drifts, this fails the type check instead of quietly
    testing a shape the real code no longer has.
    """
    repositories: MemberRepositories = stand.repositories
    assert repositories is stand.repositories


async def test_every_lookup_is_scoped_to_the_actors_own_venue(stand: Stand) -> None:
    """The provider is only ever asked for the venue the actor works in (see access.py).

    That is the whole justification for taking a factory here rather than a pre-scoped
    repository: the scope is derived from the actor on every call, so it cannot disagree
    with them.
    """
    boss = manager_of(stand)
    _, member = seed(stand, full_name="Nina")

    await stand.service.list_roster(boss)
    await stand.service.get(boss, member.id)
    await stand.service.set_position(boss, member.id, "bartender")
    await stand.service.deactivate(boss, member.id)

    assert set(stand.repositories.scopes) == {VENUE_ID}


def test_an_actor_from_the_wrong_venue_is_still_refused_by_the_guard(stand: Stand) -> None:
    """`require_venue` stays the backstop even though the scope already makes it hard.

    `MemberService` calls it after resolving a membership; this is the direct check that the
    guard it relies on is the one from `access.py` and still raises (TZ 9).
    """
    from src.services.access import require_venue

    boss = manager_of(stand)
    with pytest.raises(VenueMismatchError):
        require_venue(boss, OTHER_VENUE_ID)


async def test_the_scoped_repository_carries_the_venue_in_every_where(stand: Stand) -> None:
    """The fake held to decision D9 directly, without a service in between.

    Every test above that says "another venue's row is unreachable" rests on this; if the
    predicate ever went missing, those tests would keep passing and say nothing.
    """
    _, theirs = seed(stand, full_name="Neighbour", venue_id=OTHER_VENUE_ID)
    members = stand.repositories.members(VENUE_ID)

    assert await members.get(theirs.id) is None
    assert await members.get_for_user(theirs.user_id) is None
    assert list(await members.list_active()) == []
    assert list(await members.list_by_role(MemberRole.STAFF)) == []
    assert await members.update(theirs.id, position="bartender") is None
    assert await members.set_active(theirs.id, is_active=False) is None

    # The row exists and its own venue reads it: hidden by the predicate, not by an empty table.
    assert await stand.repositories.members(OTHER_VENUE_ID).get(theirs.id) is theirs
    assert theirs.is_active is True
    assert theirs.position is None


async def test_the_fakes_refuse_a_column_the_table_does_not_have(stand: Stand) -> None:
    """Both `_apply`s of the real repositories: an unknown column is a typo, not a no-op."""
    user, member = seed(stand, full_name="Nina")

    with pytest.raises(AttributeError):
        await stand.repositories.members(VENUE_ID).update(member.id, positon="bartender")
    with pytest.raises(AttributeError):
        await stand.repositories.users.update(user.id, fullname="Nina Petrova")

    assert member.position is None
    assert user.full_name == "Nina"


def test_a_roster_entry_reads_the_membership_and_nothing_else(stand: Stand) -> None:
    user, member = seed(stand, full_name="Nina", position="bartender")

    entry = RosterEntry(member=member, user=user)

    assert (entry.member_id, entry.user_id) == (member.id, user.id)
    assert (entry.role, entry.position, entry.is_active) == (MemberRole.STAFF, "bartender", True)
    assert RosterEntry(member=member, user=None).full_name == ""
