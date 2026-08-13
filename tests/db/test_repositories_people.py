"""Integration tests for the people repositories (plan, task 11).

Covered: `venues`, `venue_settings`, `users`, `venue_members`, `invite_codes`.

Every test runs against a real PostgreSQL (marker `db`, harness in `tests/conftest.py`) and
inside a transaction that is rolled back afterwards. Two venues are built wherever isolation
is the point: criterion 11.3 is not "the query has a venue predicate" — that is what the
static guard checks — but "venue A cannot read or address a row of venue B", and only a live
database can answer it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import MemberRole, User, Venue
from src.db.repositories.invites import InviteCodeRepo
from src.db.repositories.members import VenueMemberRepo
from src.db.repositories.users import UserRepo
from src.db.repositories.venues import VenueRepo, VenueSettingsRepo

from tests.factories import (
    DEFAULT_SHIFT_END,
    DEFAULT_SHIFT_START,
    DEFAULT_TIMEZONE,
    create_invite_code,
    create_user,
    create_venue,
    create_venue_member,
    next_sequence,
)

pytestmark = pytest.mark.db

#: Any fixed instant works: nothing in the repository layer reads a clock (D12).
MOMENT = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)


def unique_code() -> str:
    """A fresh invite code. `invite_codes.code` is globally unique, sequence keeps it so."""
    return f"code{next_sequence():08d}"


async def two_venues(session: AsyncSession) -> tuple[Venue, Venue]:
    return await create_venue(session), await create_venue(session)


# --------------------------------------------------------------------------------------
# venues
# --------------------------------------------------------------------------------------


async def test_venue_is_created_and_read_back(session: AsyncSession) -> None:
    repo = VenueRepo(session)

    created = await repo.create(name="Venue A", city="City", timezone=DEFAULT_TIMEZONE)
    found = await repo.get(created.id)

    assert found is not None
    assert found.id == created.id
    assert found.timezone == DEFAULT_TIMEZONE
    assert found.is_active is True


async def test_unknown_venue_id_is_none(session: AsyncSession) -> None:
    repo = VenueRepo(session)
    venue = await create_venue(session)

    assert await repo.get(venue.id + 10_000) is None


async def test_venue_update_returns_none_for_an_unknown_id(session: AsyncSession) -> None:
    repo = VenueRepo(session)
    venue = await create_venue(session)

    updated = await repo.update(venue.id, city="Another City")
    assert updated is not None
    assert updated.city == "Another City"

    assert await repo.update(venue.id + 10_000, city="Nowhere") is None


async def test_venue_update_refuses_a_column_that_does_not_exist(session: AsyncSession) -> None:
    """A typo in a keyword used to become an instance attribute and vanish on flush."""
    repo = VenueRepo(session)
    venue = await create_venue(session)

    with pytest.raises(AttributeError):
        await repo.update(venue.id, citty="City")


async def test_list_for_user_returns_only_the_venues_the_person_belongs_to(
    session: AsyncSession,
) -> None:
    first, second = await two_venues(session)
    third = await create_venue(session)
    user = await create_user(session)
    await create_venue_member(session, first, user)
    await create_venue_member(session, second, user)

    found = await VenueRepo(session).list_for_user(user.id)

    assert {venue.id for venue in found} == {first.id, second.id}
    assert third.id not in {venue.id for venue in found}


async def test_list_for_user_is_empty_for_a_person_without_memberships(
    session: AsyncSession,
) -> None:
    """TZ 8.1: the empty state is the main scenario, not a corner case."""
    user = await create_user(session)
    await create_venue(session)

    found = await VenueRepo(session).list_for_user(user.id)

    assert list(found) == []


# --------------------------------------------------------------------------------------
# venue_settings
# --------------------------------------------------------------------------------------


async def test_settings_are_absent_until_they_are_created(session: AsyncSession) -> None:
    venue = await create_venue(session)

    assert await VenueSettingsRepo(session, venue.id).get() is None


async def test_settings_keep_their_schema_defaults(session: AsyncSession) -> None:
    venue = await create_venue(session)
    repo = VenueSettingsRepo(session, venue.id)

    created = await repo.create(
        default_shift_start=DEFAULT_SHIFT_START,
        default_shift_end=DEFAULT_SHIFT_END,
    )
    await session.refresh(created)

    assert created.default_shift_start == DEFAULT_SHIFT_START
    assert created.opening_checklist_lead_minutes == 10
    assert created.group_chat_id is None


async def test_settings_update_only_touches_the_active_venue(session: AsyncSession) -> None:
    first, second = await two_venues(session)
    for venue in (first, second):
        await VenueSettingsRepo(session, venue.id).create(
            default_shift_start=DEFAULT_SHIFT_START,
            default_shift_end=DEFAULT_SHIFT_END,
        )

    updated = await VenueSettingsRepo(session, first.id).update(group_chat_id=-100)

    assert updated is not None
    assert updated.venue_id == first.id
    other = await VenueSettingsRepo(session, second.id).get()
    assert other is not None
    assert other.group_chat_id is None


async def test_settings_of_another_venue_are_not_visible(session: AsyncSession) -> None:
    first, second = await two_venues(session)
    await VenueSettingsRepo(session, first.id).create(
        default_shift_start=DEFAULT_SHIFT_START,
        default_shift_end=DEFAULT_SHIFT_END,
    )

    assert await VenueSettingsRepo(session, second.id).get() is None
    assert await VenueSettingsRepo(session, second.id).update(group_chat_id=-1) is None


# --------------------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------------------


async def test_user_is_addressed_by_id_and_by_telegram_id(session: AsyncSession) -> None:
    repo = UserRepo(session)
    telegram_id = 800_000_000 + next_sequence()

    created = await repo.create(telegram_id=telegram_id, full_name="Person A", username="a")

    by_id = await repo.get(created.id)
    by_telegram = await repo.get_by_telegram_id(telegram_id)
    assert by_id is not None
    assert by_telegram is not None
    assert by_id.id == by_telegram.id == created.id
    assert by_id.phone is None
    assert by_id.is_bot_blocked is False


async def test_unknown_user_is_none(session: AsyncSession) -> None:
    repo = UserRepo(session)
    user = await create_user(session)

    assert await repo.get(user.id + 10_000) is None
    assert await repo.get_by_telegram_id(user.telegram_id + 10_000) is None
    assert await repo.update(user.id + 10_000, full_name="X") is None


async def test_user_flags_are_written_through_their_own_methods(session: AsyncSession) -> None:
    repo = UserRepo(session)
    user = await create_user(session)
    venue = await create_venue(session)

    await repo.touch_last_seen(user.id, MOMENT)
    await repo.set_active_venue(user.id, venue.id)
    await repo.set_bot_blocked(user.id, blocked=True)

    stored = await repo.get(user.id)
    assert stored is not None
    assert stored.last_seen_at == MOMENT
    assert stored.active_venue_id == venue.id
    assert stored.is_bot_blocked is True

    await repo.set_active_venue(user.id, None)
    cleared = await repo.get(user.id)
    assert cleared is not None
    assert cleared.active_venue_id is None


async def test_user_rename_keeps_the_row(session: AsyncSession) -> None:
    repo = UserRepo(session)
    user = await create_user(session, full_name="Before")

    updated = await repo.update(user.id, full_name="After")

    assert updated is not None
    assert updated.id == user.id
    assert updated.full_name == "After"


# --------------------------------------------------------------------------------------
# venue_members
# --------------------------------------------------------------------------------------


async def test_member_is_added_and_found_for_the_user(session: AsyncSession) -> None:
    venue = await create_venue(session)
    user = await create_user(session)
    repo = VenueMemberRepo(session, venue.id)

    member = await repo.add(user_id=user.id, role=MemberRole.MANAGER, position="p1")

    assert member.venue_id == venue.id
    found = await repo.get_for_user(user.id)
    assert found is not None
    assert found.id == member.id
    assert found.role is MemberRole.MANAGER
    assert found.is_active is True


async def test_member_of_another_venue_is_not_addressable(session: AsyncSession) -> None:
    """Acceptance 11.3: a foreign id must resolve to nothing, not to somebody's roster line."""
    first, second = await two_venues(session)
    user = await create_user(session)
    foreign = await create_venue_member(session, second, user)

    repo = VenueMemberRepo(session, first.id)

    assert await repo.get(foreign.id) is None
    assert await repo.get_for_user(user.id) is None
    assert await repo.update(foreign.id, position="p") is None
    assert await repo.set_active(foreign.id, is_active=False) is None


async def test_listing_members_stays_inside_the_venue(session: AsyncSession) -> None:
    first, second = await two_venues(session)
    here = await create_venue_member(session, first, await create_user(session))
    gone = await create_venue_member(session, first, await create_user(session), is_active=False)
    elsewhere = await create_venue_member(session, second, await create_user(session))

    active = await VenueMemberRepo(session, first.id).list_active()

    ids = {member.id for member in active}
    assert here.id in ids
    assert gone.id not in ids
    assert elsewhere.id not in ids


async def test_listing_by_role_ignores_other_roles_and_other_venues(
    session: AsyncSession,
) -> None:
    first, second = await two_venues(session)
    manager = await create_venue_member(
        session, first, await create_user(session), role=MemberRole.MANAGER
    )
    await create_venue_member(session, first, await create_user(session), role=MemberRole.STAFF)
    await create_venue_member(session, second, await create_user(session), role=MemberRole.MANAGER)

    found = await VenueMemberRepo(session, first.id).list_by_role(MemberRole.MANAGER)

    assert [member.id for member in found] == [manager.id]


async def test_deactivating_a_member_keeps_the_row(session: AsyncSession) -> None:
    """TZ 5.1: deactivation is not deletion — the history hangs on this row."""
    venue = await create_venue(session)
    member = await create_venue_member(session, venue, await create_user(session))
    repo = VenueMemberRepo(session, venue.id)

    updated = await repo.set_active(member.id, is_active=False)

    assert updated is not None
    assert updated.is_active is False
    assert await repo.get(member.id) is not None


# --------------------------------------------------------------------------------------
# invite_codes
# --------------------------------------------------------------------------------------


async def test_invite_code_is_created_and_read_by_its_code(session: AsyncSession) -> None:
    venue = await create_venue(session)
    author: User = await create_user(session)
    repo = InviteCodeRepo(session, venue.id)
    code = unique_code()

    created = await repo.create(
        code=code,
        role=MemberRole.STAFF,
        expires_at=MOMENT + dt.timedelta(days=7),
        position="p1",
        full_name="Person A",
        created_by=author.id,
    )

    found = await repo.get_by_code(code)
    assert found is not None
    assert found.id == created.id
    assert found.venue_id == venue.id
    assert found.used_at is None
    assert found.revoked_at is None


async def test_invite_code_of_another_venue_is_not_found(session: AsyncSession) -> None:
    first, second = await two_venues(session)
    foreign = await create_invite_code(session, second)

    repo = InviteCodeRepo(session, first.id)

    assert await repo.get_by_code(foreign.code) is None
    assert await repo.revoke(foreign.id, MOMENT) is None
    assert await repo.mark_used(foreign.id, used_by=1, used_at=MOMENT) is None


async def test_pending_codes_exclude_the_spent_and_the_revoked(session: AsyncSession) -> None:
    venue = await create_venue(session)
    user = await create_user(session)
    repo = InviteCodeRepo(session, venue.id)
    waiting = await create_invite_code(session, venue)
    spent = await create_invite_code(session, venue)
    withdrawn = await create_invite_code(session, venue)

    await repo.mark_used(spent.id, used_by=user.id, used_at=MOMENT)
    await repo.revoke(withdrawn.id, MOMENT)

    pending = {invite.id for invite in await repo.list_pending()}
    assert waiting.id in pending
    assert spent.id not in pending
    assert withdrawn.id not in pending


async def test_an_expired_code_is_still_returned(session: AsyncSession) -> None:
    """Which of the three refusals applies is the service's answer, not the repository's."""
    venue = await create_venue(session)
    invite = await create_invite_code(session, venue, expires_at=MOMENT - dt.timedelta(days=1))

    found = await InviteCodeRepo(session, venue.id).get_by_code(invite.code)

    assert found is not None
    assert found.expires_at < MOMENT


async def test_revocation_keeps_the_expiry(session: AsyncSession) -> None:
    venue = await create_venue(session)
    expires_at = MOMENT + dt.timedelta(days=7)
    invite = await create_invite_code(session, venue, expires_at=expires_at)

    revoked = await InviteCodeRepo(session, venue.id).revoke(invite.id, MOMENT)

    assert revoked is not None
    assert revoked.revoked_at == MOMENT
    assert revoked.expires_at == expires_at
