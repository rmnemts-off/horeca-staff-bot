"""Creating a venue and editing its settings (plan, tasks 26 and 30; TZ 5.8, 3.4).

Fake repositories, like everywhere in this package. What is under test is a set of promises
the wizard makes — that a venue comes up complete, that it comes up *empty*, and that the
person who ran the wizard ends up with rights that are read from the database — not a set of
queries.

Three properties of the fakes below carry the weight and are worth stating before the tests
lean on them silently.

* **They are venue-scoped exactly as the contract is.** Every scoped fake is built for one
  venue and filters a store shared by all of them, so a row of the neighbouring bar is a row
  that *exists and is unreachable* rather than a row nobody wrote (TZ 3.3, acceptance 11.3).
  `FakeItems` is the child case of decision D9: `checklist_items` has no `venue_id`, so the
  fake joins to `checklist_templates` and reaches its own rows by that join only — including
  when it is handed a bare item id.
* **They fill in the schema defaults the way a flush does.** `venue_settings` and
  `venue_members` get most of their columns from `server_default`, and the service relies on
  that: it never passes `is_active` for the owner's membership, and the assertion that the
  owner row is a *full* one (decision D3) would be vacuous against a fake that left the
  column unset.
* **`update` mutates the stored row in place**, the way SQLAlchemy's identity map does. That
  is what makes "a settings edit records the value from *before* the write" a real test: a
  service that snapshotted after writing would read back what it had just written, and every
  diff would come out empty.

The audit sink is the one from `tests/services/test_audit.py`, kept here rather than shared
because `tests/factories.py` belongs to task 5a: it stamps the venue itself, exactly as
`AuditLogRepo.record` does, so a service cannot write into another venue's history even
holding a forged id.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import available_timezones

import pytest
from src.db.models import (
    ChecklistItem,
    ChecklistTemplate,
    ChecklistType,
    MemberRole,
    User,
    Venue,
    VenueMember,
    VenueSettings,
)
from src.db.repositories.audit import AFTER_KEY, BEFORE_KEY
from src.services.access import AccessContext, Identity, PermissionDeniedError
from src.services.audit import AuditAction, AuditEntity
from src.services.timezones import UnknownTimezoneError
from src.services.venues import (
    FIRST_TEMPLATE_VERSION,
    UnknownOwnerError,
    UnknownVenueError,
    VenueCreation,
    VenueCreationNotAllowedError,
    VenueCreationRepositories,
    VenueError,
    VenueService,
    VenueSettingsMissingError,
    WizardTimezone,
    timezone_at,
    wizard_timezones,
)

#: Far apart on purpose: with `user_id=1, telegram_id=1` the assertion that the trail records
#: the id the `audit_log.user_id` foreign key points at would pass against the wrong one.
TELEGRAM_ID = 987_654_321

MOSCOW = "Europe/Moscow"
SAMARA = "Europe/Samara"

SHIFT_START = dt.time(8, 0)
SHIFT_END = dt.time(23, 0)


# --------------------------------------------------------------------------------------
# Stores and fakes (declared here on purpose: tests/factories.py belongs to task 5a)
# --------------------------------------------------------------------------------------


def apply_fields(row: object, fields: Mapping[str, Any]) -> None:
    """`_apply` of `src/db/repositories/venues.py`, character for character.

    A bare `setattr` loop is the generous version: `update(timezon="Europe/Moscow")` would
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

    def add(self, *, full_name: str, telegram_id: int) -> User:
        user = User(
            id=self._next_id,
            telegram_id=telegram_id,
            full_name=full_name,
            username=None,
            is_active=True,
            active_venue_id=None,
            is_bot_blocked=False,
        )
        self._next_id += 1
        self.rows.append(user)
        return user


class FakeUsers:
    """`UserRepository`, global: a person exists before any venue does (TZ 2)."""

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

    async def touch_last_seen(self, user_id: int, moment: dt.datetime) -> None:
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


class VenueStore:
    def __init__(self) -> None:
        self.rows: list[Venue] = []
        self._next_id = 1

    def add(self, *, name: str, city: str, timezone: str) -> Venue:
        venue = Venue(
            id=self._next_id,
            name=name,
            city=city,
            timezone=timezone,
            is_active=True,
        )
        self._next_id += 1
        self.rows.append(venue)
        return venue


class FakeVenues:
    """`VenueRepository` — the one repository that is not scoped, because it *is* the scope.

    `list_for_user` reproduces the join of `VenueRepo`: every venue the person holds a
    membership row in, inactive rows included, because deciding whether such a membership
    still grants access is `src/services/access.py`'s judgement and not this table's.
    """

    def __init__(self, store: VenueStore, members: MemberStore) -> None:
        self.store = store
        self.members = members

    async def get(self, venue_id: int) -> Venue | None:
        return next((row for row in self.store.rows if row.id == venue_id), None)

    async def list_for_user(self, user_id: int) -> Sequence[Venue]:
        ids = {row.venue_id for row in self.members.rows if row.user_id == user_id}
        return [row for row in self.store.rows if row.id in ids]

    async def list_active(self) -> Sequence[Venue]:
        # `VenueRepo.list_active`: switched-on venues only, membership not consulted.
        return [row for row in self.store.rows if row.is_active]

    async def create(self, *, name: str, city: str, timezone: str) -> Venue:
        return self.store.add(name=name, city=city, timezone=timezone)

    async def update(self, venue_id: int, **fields: Any) -> Venue | None:
        row = await self.get(venue_id)
        if row is None:
            return None
        apply_fields(row, fields)
        return row


class SettingsStore:
    def __init__(self) -> None:
        self.rows: list[VenueSettings] = []

    def add(
        self,
        *,
        venue_id: int,
        default_shift_start: dt.time,
        default_shift_end: dt.time,
        **fields: Any,
    ) -> VenueSettings:
        """The columns a flush would fill from `server_default` are filled here too.

        Everything except the two shift times has a default in the schema (TZ 4.1), and the
        service deliberately passes none of them — answer A2 says a venue that never opens
        the settings screen still behaves correctly. A fake that left them `None` would make
        the "an edit to the same value records nothing" test unable to fail.
        """
        settings = VenueSettings(
            venue_id=venue_id,
            default_shift_start=default_shift_start,
            default_shift_end=default_shift_end,
            opening_checklist_lead_minutes=10,
            closing_checklist_lead_minutes=30,
            shift_reminder_hours=12,
            checklist_overdue_minutes=60,
            escalate_to_manager=True,
            group_chat_id=None,
            order_reminder_time=None,
        )
        apply_fields(settings, fields)
        self.rows.append(settings)
        return settings


class FakeSettings:
    """`VenueSettingsRepository`: one row per venue, so the scope alone identifies it."""

    def __init__(self, store: SettingsStore, venue_id: int) -> None:
        self.store = store
        self.venue_id = venue_id

    async def get(self) -> VenueSettings | None:
        return next((row for row in self.store.rows if row.venue_id == self.venue_id), None)

    async def create(
        self,
        *,
        default_shift_start: dt.time,
        default_shift_end: dt.time,
        **fields: Any,
    ) -> VenueSettings:
        return self.store.add(
            venue_id=self.venue_id,
            default_shift_start=default_shift_start,
            default_shift_end=default_shift_end,
            **fields,
        )

    async def update(self, **fields: Any) -> VenueSettings | None:
        row = await self.get()
        if row is None:
            return None
        apply_fields(row, fields)
        return row


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
        # `is_active` and `joined_at` come from `server_default`; the store fills them the
        # way a flush would, which is what decision D3 is asserted against.
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


class TemplateStore:
    def __init__(self) -> None:
        self.rows: list[ChecklistTemplate] = []
        self._next_id = 1

    def add(
        self,
        *,
        venue_id: int,
        checklist_type: ChecklistType,
        name: str,
        version: int,
        updated_by: int | None,
    ) -> ChecklistTemplate:
        template = ChecklistTemplate(
            id=self._next_id,
            venue_id=venue_id,
            type=checklist_type,
            name=name,
            version=version,
            is_active=True,
            updated_by=updated_by,
        )
        self._next_id += 1
        self.rows.append(template)
        return template


class FakeTemplates:
    """`ChecklistTemplateRepository`, scoped.

    The partial unique index of decision D7 ("one active template per venue and type") is the
    database's job and is not reproduced here: nothing in this module creates a second
    version, and a fake that enforced more than it is asked would make the wizard's two
    `create` calls pass for a reason the schema does not supply.
    """

    def __init__(self, store: TemplateStore, venue_id: int) -> None:
        self.store = store
        self.venue_id = venue_id

    def _scoped(self) -> list[ChecklistTemplate]:
        return [row for row in self.store.rows if row.venue_id == self.venue_id]

    async def get(self, template_id: int) -> ChecklistTemplate | None:
        return next((row for row in self._scoped() if row.id == template_id), None)

    async def get_active(self, checklist_type: ChecklistType) -> ChecklistTemplate | None:
        return next(
            (row for row in self._scoped() if row.type is checklist_type and row.is_active),
            None,
        )

    async def list_versions(self, checklist_type: ChecklistType) -> Sequence[ChecklistTemplate]:
        return [row for row in self._scoped() if row.type is checklist_type]

    async def create(
        self,
        *,
        checklist_type: ChecklistType,
        name: str,
        version: int,
        updated_by: int | None = None,
    ) -> ChecklistTemplate:
        return self.store.add(
            venue_id=self.venue_id,
            checklist_type=checklist_type,
            name=name,
            version=version,
            updated_by=updated_by,
        )

    async def update(self, template_id: int, **fields: Any) -> ChecklistTemplate | None:
        row = await self.get(template_id)
        if row is None:
            return None
        apply_fields(row, fields)
        return row

    async def deactivate(self, template_id: int) -> ChecklistTemplate | None:
        return await self.update(template_id, is_active=False)

    async def is_referenced_by_runs(self, template_id: int) -> bool:
        return False


class ItemStore:
    def __init__(self) -> None:
        self.rows: list[ChecklistItem] = []
        self._next_id = 1

    def add(self, *, template_id: int, text: str) -> ChecklistItem:
        item = ChecklistItem(
            id=self._next_id,
            template_id=template_id,
            text=text,
            group_name=None,
            group_index=0,
            order_index=len(self.rows),
            requires_photo=False,
            requires_comment=False,
            is_critical=False,
        )
        self._next_id += 1
        self.rows.append(item)
        return item


class FakeItems:
    """`checklist_items` as decision D9 builds it: a child reachable only through its parent.

    Only the reading half is here — this service writes no items at all, and that is exactly
    what the tests below need to check (decision B1, acceptance 11.5). The join is the point:
    a bare item id of another venue resolves to nothing, not to a row.
    """

    def __init__(self, items: ItemStore, templates: TemplateStore, venue_id: int) -> None:
        self.items = items
        self.templates = templates
        self.venue_id = venue_id

    def _parent(self, template_id: int) -> ChecklistTemplate | None:
        return next(
            (
                row
                for row in self.templates.rows
                if row.id == template_id and row.venue_id == self.venue_id
            ),
            None,
        )

    async def get(self, item_id: int) -> ChecklistItem | None:
        row = next((item for item in self.items.rows if item.id == item_id), None)
        if row is None or self._parent(row.template_id) is None:
            return None
        return row

    async def list_for_template(self, template_id: int) -> Sequence[ChecklistItem]:
        if self._parent(template_id) is None:
            return []
        return [row for row in self.items.rows if row.template_id == template_id]

    async def count_for_template(self, template_id: int) -> int:
        return len(await self.list_for_template(template_id))


@dataclass(frozen=True, slots=True)
class Entry:
    """One `audit_log` row, with the columns `AuditLogRepo.record` writes."""

    id: int
    venue_id: int
    user_id: int | None
    entity: str
    entity_id: int | None
    action: str
    diff: dict[str, Any] | None


class FakeAuditLog:
    """`AuditLogRepo` as `AuditSink` sees it: append-only and venue-scoped.

    The store is shared between venues because the table is — a record of another bar is a
    row that exists and is unreachable, not a row nobody wrote.
    """

    def __init__(self, store: list[Entry], venue_id: int) -> None:
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
    ) -> Entry:
        entry = Entry(
            id=len(self.store) + 1,
            venue_id=self.venue_id,
            user_id=user_id,
            entity=entity,
            entity_id=entity_id,
            action=action,
            diff=diff,
        )
        self.store.append(entry)
        return entry

    @property
    def entries(self) -> list[Entry]:
        """Everything this venue wrote, oldest first."""
        return [row for row in self.store if row.venue_id == self.venue_id]


class FakeRepositories:
    """The provider `VenueService` asks for: two global tables and four scoped factories."""

    def __init__(self) -> None:
        self.user_store = UserStore()
        self.venue_store = VenueStore()
        self.settings_store = SettingsStore()
        self.member_store = MemberStore()
        self.template_store = TemplateStore()
        self.item_store = ItemStore()
        self.audit_store: list[Entry] = []
        self._users = FakeUsers(self.user_store)
        self._venues = FakeVenues(self.venue_store, self.member_store)
        #: Every venue whose repository was ever built, in order — the scoping assertion.
        self.scopes: list[int] = []

    @property
    def users(self) -> FakeUsers:
        return self._users

    @property
    def venues(self) -> FakeVenues:
        return self._venues

    def settings(self, venue_id: int) -> FakeSettings:
        self.scopes.append(venue_id)
        return FakeSettings(self.settings_store, venue_id)

    def members(self, venue_id: int) -> FakeMembers:
        self.scopes.append(venue_id)
        return FakeMembers(self.member_store, venue_id)

    def templates(self, venue_id: int) -> FakeTemplates:
        self.scopes.append(venue_id)
        return FakeTemplates(self.template_store, venue_id)

    def items(self, venue_id: int) -> FakeItems:
        self.scopes.append(venue_id)
        return FakeItems(self.item_store, self.template_store, venue_id)

    def audit(self, venue_id: int) -> FakeAuditLog:
        self.scopes.append(venue_id)
        return FakeAuditLog(self.audit_store, venue_id)


# --------------------------------------------------------------------------------------
# Test stand
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Stand:
    repositories: FakeRepositories
    service: VenueService
    telegram_ids: Any = field(default_factory=lambda: iter(range(TELEGRAM_ID, TELEGRAM_ID + 100)))

    def person(self, full_name: str) -> User:
        return self.repositories.user_store.add(
            full_name=full_name,
            telegram_id=next(self.telegram_ids),
        )

    def permit(self, full_name: str = "Oleg") -> Identity:
        """A bootstrap owner of decision A3: a person, belonging nowhere, allowed in."""
        return permit_of(self.person(full_name))

    def audit_of(self, venue_id: int) -> list[Entry]:
        return self.repositories.audit(venue_id).entries


@pytest.fixture
def stand() -> Stand:
    repositories = FakeRepositories()
    return Stand(repositories=repositories, service=VenueService(repositories))


def permit_of(user: User, *, allowed: bool = True) -> Identity:
    """The identity the wizard is handed: who, and whether they may (decision A3)."""
    return Identity(user=user, contexts=(), active=None, may_create_venue=allowed)


async def open_venue(
    stand: Stand,
    *,
    name: str,
    owner_name: str = "Oleg",
    zone: str = MOSCOW,
) -> VenueCreation:
    """The wizard, run the way task 26 runs it."""
    return await stand.service.create(
        permit_of(stand.person(owner_name)),
        name=name,
        city="Moscow",
        timezone=zone,
        default_shift_start=SHIFT_START,
        default_shift_end=SHIFT_END,
    )


def staff_in(venue_id: int) -> AccessContext:
    """A `staff` membership of the same venue, built by hand: the wizard makes owners only."""
    return AccessContext(
        user_id=99,
        telegram_id=TELEGRAM_ID + 500,
        venue_id=venue_id,
        member_id=99,
        role=MemberRole.STAFF,
        full_name="Sam",
    )


# --------------------------------------------------------------------------------------
# The wizard (task 26, decisions A3, B1, D3)
# --------------------------------------------------------------------------------------


async def test_the_wizard_writes_the_venue_its_settings_and_its_owner_at_once(
    stand: Stand,
) -> None:
    creation = await open_venue(stand, name="Invasion")

    assert (creation.venue.name, creation.venue.city) == ("Invasion", "Moscow")
    assert creation.venue.timezone == MOSCOW
    assert creation.settings.venue_id == creation.venue.id
    assert (creation.settings.default_shift_start, creation.settings.default_shift_end) == (
        SHIFT_START,
        SHIFT_END,
    )
    assert creation.member.venue_id == creation.venue.id
    # Answer A2: the timings nobody chose are the schema's, not this service's invention.
    assert creation.settings.opening_checklist_lead_minutes == 10


async def test_both_checklists_are_created_and_hold_not_a_single_item(stand: Stand) -> None:
    """Decision B1 and acceptance 11.5: the templates exist, the venue starts empty."""
    creation = await open_venue(stand, name="Invasion")

    types = [template.type for template in creation.templates]
    assert types == [ChecklistType.OPENING, ChecklistType.CLOSING]
    assert all(template.version == FIRST_TEMPLATE_VERSION for template in creation.templates)
    assert all(template.is_active for template in creation.templates)

    items = stand.repositories.items(creation.venue_id)
    counts = [await items.count_for_template(template.id) for template in creation.templates]
    assert counts == [0, 0]
    # Not a single `checklist_items` row anywhere, which is the criterion as it is worded.
    assert stand.repositories.item_store.rows == []


async def test_the_owner_gets_a_real_membership_row_and_the_rights_that_follow(
    stand: Stand,
) -> None:
    """Decisions A3 and D3: after the wizard, TZ 2 can read the rights out of the database."""
    creation = await open_venue(stand, name="Invasion")

    stored = stand.repositories.member_store.rows
    assert len(stored) == 1
    assert stored[0].role is MemberRole.OWNER
    assert stored[0].is_active is True
    assert stored[0].venue_id == creation.venue.id

    assert creation.owner.role is MemberRole.OWNER
    assert creation.owner.is_owner and creation.owner.is_manager
    assert creation.owner.venue_id == creation.venue.id
    assert creation.owner.member_id == stored[0].id
    assert creation.owner.user_id == stored[0].user_id


async def test_an_unknown_timezone_is_refused_before_anything_is_written(stand: Stand) -> None:
    with pytest.raises(UnknownTimezoneError):
        await open_venue(stand, name="Invasion", zone="Europe/Moskva")

    assert stand.repositories.venue_store.rows == []
    assert stand.repositories.settings_store.rows == []
    assert stand.repositories.member_store.rows == []
    assert stand.repositories.template_store.rows == []
    assert stand.repositories.audit_store == []


async def test_an_identity_that_may_not_create_a_venue_is_refused_by_the_service(
    stand: Stand,
) -> None:
    """The check is here and not only in the screen that draws the wizard.

    `VenueService` sits in the handler context of every update, so a handler written at
    stage 1 that calls it without asking would let ordinary `staff` raise venues — and
    nothing in the type system would notice, because a bare `User` argument carries no
    permission at all. That is why the parameter is the whole `Identity` (decision A3).
    """
    user = stand.person("Sam")

    with pytest.raises(VenueCreationNotAllowedError):
        await stand.service.create(
            permit_of(user, allowed=False),
            name="Ghost",
            city="Moscow",
            timezone=MOSCOW,
            default_shift_start=SHIFT_START,
            default_shift_end=SHIFT_END,
        )

    assert stand.repositories.venue_store.rows == [], "nothing may be written before the check"


async def test_a_permit_without_a_person_behind_it_is_refused(stand: Stand) -> None:
    """Decision A3 puts a `users` row in front of the wizard: `/start` creates it first."""
    with pytest.raises(UnknownOwnerError):
        await stand.service.create(
            Identity(user=None, contexts=(), active=None, may_create_venue=True),
            name="Ghost",
            city="Moscow",
            timezone=MOSCOW,
            default_shift_start=SHIFT_START,
            default_shift_end=SHIFT_END,
        )
    assert stand.repositories.venue_store.rows == []


async def test_a_blank_name_or_city_is_refused_rather_than_written(stand: Stand) -> None:
    with pytest.raises(VenueError):
        await open_venue(stand, name="   ")
    assert stand.repositories.venue_store.rows == []


async def test_the_name_and_the_city_are_trimmed(stand: Stand) -> None:
    creation = await stand.service.create(
        stand.permit(),
        name="  Invasion  ",
        city="  Moscow  ",
        timezone=MOSCOW,
        default_shift_start=SHIFT_START,
        default_shift_end=SHIFT_END,
    )

    assert (creation.venue.name, creation.venue.city) == ("Invasion", "Moscow")


async def test_the_creation_is_recorded_five_times_under_the_owners_user_id(stand: Stand) -> None:
    """TZ 2: who created what. The trail can only be built once `venues.id` exists."""
    creation = await open_venue(stand, name="Invasion")
    entries = stand.audit_of(creation.venue_id)

    assert [(entry.entity, entry.action) for entry in entries] == [
        (AuditEntity.VENUE, AuditAction.CREATE),
        (AuditEntity.VENUE_SETTINGS, AuditAction.CREATE),
        (AuditEntity.MEMBER, AuditAction.CREATE),
        (AuditEntity.CHECKLIST_TEMPLATE, AuditAction.CREATE),
        (AuditEntity.CHECKLIST_TEMPLATE, AuditAction.CREATE),
    ]
    # `users.id`, which is what the `audit_log.user_id` foreign key points at — never the
    # Telegram id, which is a different number here for exactly this assertion.
    assert {entry.user_id for entry in entries} == {creation.owner.user_id}
    assert creation.owner.telegram_id != creation.owner.user_id

    venue_entry, settings_entry, member_entry = entries[0], entries[1], entries[2]
    assert venue_entry.entity_id == creation.venue.id
    # `venue_settings` has no id of its own: `venue_id` is its primary key (TZ 4.1).
    assert settings_entry.entity_id == creation.venue.id
    assert member_entry.entity_id == creation.member.id
    assert venue_entry.diff is not None
    assert venue_entry.diff["timezone"] == {BEFORE_KEY: None, AFTER_KEY: MOSCOW}
    assert member_entry.diff is not None
    assert member_entry.diff["role"][AFTER_KEY] == MemberRole.OWNER.value


# --------------------------------------------------------------------------------------
# The settings screen (task 30, TZ 5.8)
# --------------------------------------------------------------------------------------


async def test_the_settings_screen_is_manager_only(stand: Stand) -> None:
    creation = await open_venue(stand, name="Invasion")

    with pytest.raises(PermissionDeniedError):
        await stand.service.update_settings(
            staff_in(creation.venue_id),
            opening_checklist_lead_minutes=25,
        )

    assert creation.settings.opening_checklist_lead_minutes == 10
    assert len(stand.audit_of(creation.venue_id)) == 5


async def test_a_manager_changes_a_timing_and_the_diff_carries_the_old_value(
    stand: Stand,
) -> None:
    creation = await open_venue(stand, name="Invasion")

    updated = await stand.service.update_settings(
        creation.owner,
        opening_checklist_lead_minutes=25,
        checklist_overdue_minutes=45,
    )

    assert updated.settings.opening_checklist_lead_minutes == 25
    assert updated.settings.checklist_overdue_minutes == 45
    entry = stand.audit_of(creation.venue_id)[-1]
    assert (entry.entity, entry.action) == (AuditEntity.VENUE_SETTINGS, AuditAction.UPDATE)
    assert entry.entity_id == creation.venue_id
    assert entry.diff == {
        "opening_checklist_lead_minutes": {BEFORE_KEY: 10, AFTER_KEY: 25},
        "checklist_overdue_minutes": {BEFORE_KEY: 60, AFTER_KEY: 45},
    }


async def test_only_the_fields_that_moved_reach_the_diff(stand: Stand) -> None:
    creation = await open_venue(stand, name="Invasion")

    await stand.service.update_settings(
        creation.owner,
        opening_checklist_lead_minutes=25,
        shift_reminder_hours=12,
    )

    assert stand.audit_of(creation.venue_id)[-1].diff == {
        "opening_checklist_lead_minutes": {BEFORE_KEY: 10, AFTER_KEY: 25},
    }


async def test_writing_a_value_that_is_already_there_records_nothing(stand: Stand) -> None:
    """An "update" that changed nothing is not a change, and an empty diff is noise (TZ 2)."""
    creation = await open_venue(stand, name="Invasion")
    before = len(stand.audit_of(creation.venue_id))

    result = await stand.service.update_settings(
        creation.owner,
        opening_checklist_lead_minutes=10,
        timezone=MOSCOW,
    )

    assert result.settings.opening_checklist_lead_minutes == 10
    assert result.venue.timezone == MOSCOW
    assert len(stand.audit_of(creation.venue_id)) == before


async def test_calling_with_nothing_to_change_is_a_read(stand: Stand) -> None:
    creation = await open_venue(stand, name="Invasion")
    before = len(stand.audit_of(creation.venue_id))

    result = await stand.service.update_settings(creation.owner)

    assert result.venue is creation.venue
    assert result.settings is creation.settings
    assert len(stand.audit_of(creation.venue_id)) == before


async def test_the_timezone_is_written_to_the_venue_and_recorded_against_it(
    stand: Stand,
) -> None:
    """TZ 3.4 keeps the zone on `venues`, so it is a different table and a different record."""
    creation = await open_venue(stand, name="Invasion")

    updated = await stand.service.update_settings(creation.owner, timezone=SAMARA)

    assert updated.venue.timezone == SAMARA
    assert stand.repositories.venue_store.rows[0].timezone == SAMARA
    entry = stand.audit_of(creation.venue_id)[-1]
    assert (entry.entity, entry.action) == (AuditEntity.VENUE, AuditAction.UPDATE)
    assert entry.entity_id == creation.venue.id
    assert entry.diff == {"timezone": {BEFORE_KEY: MOSCOW, AFTER_KEY: SAMARA}}


async def test_the_timezone_and_the_rest_are_two_records_not_one(stand: Stand) -> None:
    creation = await open_venue(stand, name="Invasion")
    before = len(stand.audit_of(creation.venue_id))

    await stand.service.update_settings(
        creation.owner,
        timezone=SAMARA,
        opening_checklist_lead_minutes=25,
    )

    written = stand.audit_of(creation.venue_id)[before:]
    assert [entry.entity for entry in written] == [
        AuditEntity.VENUE,
        AuditEntity.VENUE_SETTINGS,
    ]


async def test_an_unknown_timezone_never_reaches_the_row(stand: Stand) -> None:
    creation = await open_venue(stand, name="Invasion")
    before = len(stand.audit_of(creation.venue_id))

    with pytest.raises(UnknownTimezoneError):
        await stand.service.update_settings(creation.owner, timezone="Mars/Olympus")

    assert creation.venue.timezone == MOSCOW
    assert len(stand.audit_of(creation.venue_id)) == before


async def test_a_venue_without_a_settings_row_is_a_refusal_not_a_crash(stand: Stand) -> None:
    """TZ 8.1: a venue that predates the wizard has no settings row, and the screen says so."""
    creation = await open_venue(stand, name="Invasion")
    stand.repositories.settings_store.rows.clear()

    with pytest.raises(VenueSettingsMissingError):
        await stand.service.update_settings(creation.owner, opening_checklist_lead_minutes=25)


async def test_an_actor_whose_venue_is_gone_is_a_refusal(stand: Stand) -> None:
    creation = await open_venue(stand, name="Invasion")
    stand.repositories.venue_store.rows.clear()

    with pytest.raises(UnknownVenueError):
        await stand.service.update_settings(creation.owner, opening_checklist_lead_minutes=25)


async def test_an_unknown_column_is_a_typo_and_says_so(stand: Stand) -> None:
    creation = await open_venue(stand, name="Invasion")

    with pytest.raises(AttributeError):
        await stand.service.update_settings(creation.owner, opening_checklist_lead_minute=25)

    assert creation.settings.opening_checklist_lead_minutes == 10


# --------------------------------------------------------------------------------------
# The zones the wizard offers (TZ 3.4, plan task 26)
# --------------------------------------------------------------------------------------


def test_every_offered_zone_exists_in_the_time_zone_database() -> None:
    """A typo here would only surface when a real venue opened its first shift."""
    unknown = [zone for zone in wizard_timezones() if zone not in available_timezones()]

    assert unknown == []


def test_the_offered_zones_are_the_enum_in_its_declared_order() -> None:
    """The order is the contract: `TimezoneChoice` carries an index, not a name."""
    assert wizard_timezones() == tuple(str(zone) for zone in WizardTimezone)
    assert len(set(wizard_timezones())) == len(wizard_timezones())
    assert wizard_timezones()[1] == MOSCOW


@pytest.mark.parametrize("index", [-1, len(wizard_timezones()), 10_000])
def test_an_index_that_names_nothing_answers_none(index: int) -> None:
    """A number out of a `callback_data` anybody can retype dies here, not in a traceback."""
    assert timezone_at(index) is None


def test_a_zone_is_found_by_the_index_the_callback_carries() -> None:
    zones = wizard_timezones()

    assert [timezone_at(index) for index in range(len(zones))] == list(zones)


async def test_an_offered_zone_is_accepted_by_the_wizard(stand: Stand) -> None:
    """The list and the validation have to agree, or the buttons would offer refusals."""
    for index, zone in enumerate(wizard_timezones()):
        creation = await open_venue(stand, name=f"Bar {index}", zone=zone)
        assert creation.venue.timezone == zone


# --------------------------------------------------------------------------------------
# The seams
# --------------------------------------------------------------------------------------


def test_the_fakes_are_the_contract_the_service_asks_for(stand: Stand) -> None:
    """Checked by mypy as much as by pytest: the fakes satisfy `VenueCreationRepositories`.

    The annotation is the assertion. If the provider protocol or the repository contract in
    `src/db/repositories/protocols.py` drifts, this fails the type check instead of quietly
    testing a shape the real code no longer has.
    """
    repositories: VenueCreationRepositories = stand.repositories
    assert repositories is stand.repositories


async def test_every_scoped_lookup_is_made_for_the_venue_being_worked_on(stand: Stand) -> None:
    creation = await open_venue(stand, name="Invasion")
    stand.repositories.scopes.clear()

    await stand.service.update_settings(creation.owner, opening_checklist_lead_minutes=25)

    assert set(stand.repositories.scopes) == {creation.venue_id}


async def test_another_venue_is_not_visible_through_any_scoped_fake(stand: Stand) -> None:
    """Acceptance 11.3: the neighbour's rows are written, shared, and unreachable.

    Every "another venue cannot be touched" claim in this file rests on the predicates below.
    Without this test they would be dead code: remove any of them and the whole file stays
    green, because nothing else ever addresses a foreign id directly.
    """
    mine = await open_venue(stand, name="Invasion", owner_name="Oleg")
    theirs = await open_venue(stand, name="Neighbour", owner_name="Nina", zone=SAMARA)

    settings = stand.repositories.settings(mine.venue_id)
    members = stand.repositories.members(mine.venue_id)
    templates = stand.repositories.templates(mine.venue_id)
    audit = stand.repositories.audit(mine.venue_id)

    assert await members.get(theirs.member.id) is None
    assert await members.get_for_user(theirs.member.user_id) is None
    assert [row.id for row in await members.list_active()] == [mine.member.id]
    assert await templates.get(theirs.templates[0].id) is None
    assert await templates.get_active(ChecklistType.OPENING) is mine.templates[0]
    assert [row.id for row in await templates.list_versions(ChecklistType.CLOSING)] == [
        mine.templates[1].id
    ]
    assert await settings.get() is mine.settings
    assert theirs.venue_id not in {entry.venue_id for entry in audit.entries}

    # The rows exist and their own venue reads them: hidden by a predicate, not by an empty
    # table. The same store answers both venues, which is what makes the check meaningful.
    neighbour = stand.repositories.members(theirs.venue_id)
    assert await neighbour.get(theirs.member.id) is theirs.member
    assert await stand.repositories.settings(theirs.venue_id).get() is theirs.settings
    assert theirs.venue.timezone == SAMARA


async def test_a_foreign_checklist_item_is_not_addressable_even_by_its_own_id(
    stand: Stand,
) -> None:
    """Decision D9: `checklist_items` has no `venue_id`, so the fake joins to its template."""
    mine = await open_venue(stand, name="Invasion", owner_name="Oleg")
    theirs = await open_venue(stand, name="Neighbour", owner_name="Nina")
    foreign = stand.repositories.item_store.add(
        template_id=theirs.templates[0].id,
        text="wipe the bar",
    )

    items = stand.repositories.items(mine.venue_id)

    assert await items.get(foreign.id) is None
    assert await items.count_for_template(theirs.templates[0].id) == 0
    assert list(await items.list_for_template(theirs.templates[0].id)) == []

    # Its own venue sees it, so the row is real and the predicate is what hides it.
    neighbour = stand.repositories.items(theirs.venue_id)
    assert await neighbour.get(foreign.id) is foreign
    assert await neighbour.count_for_template(theirs.templates[0].id) == 1


async def test_the_venue_repository_is_the_one_that_is_not_scoped(stand: Stand) -> None:
    """`venues` *is* the scope, and `list_for_user` is the join `VenueRepo` makes.

    Spelled out because the service leans on it: `update_settings` reads the venue through
    this unscoped repository and therefore calls `require_venue` by hand.
    """
    mine = await open_venue(stand, name="Invasion", owner_name="Oleg")
    theirs = await open_venue(stand, name="Neighbour", owner_name="Nina")

    assert await stand.repositories.venues.get(theirs.venue_id) is theirs.venue
    assert [
        row.id for row in await stand.repositories.venues.list_for_user(mine.owner.user_id)
    ] == [mine.venue_id]


async def test_the_fakes_refuse_a_column_the_table_does_not_have(stand: Stand) -> None:
    """`_apply` of the real repositories: an unknown column is a typo, not a silent no-op."""
    creation = await open_venue(stand, name="Invasion")

    with pytest.raises(AttributeError):
        await stand.repositories.venues.update(creation.venue_id, timezon=SAMARA)
    with pytest.raises(AttributeError):
        await stand.repositories.settings(creation.venue_id).update(shift_reminder_hour=6)

    assert creation.venue.timezone == MOSCOW
    assert creation.settings.shift_reminder_hours == 12
