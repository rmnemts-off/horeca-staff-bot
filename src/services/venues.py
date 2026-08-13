"""Creating a venue and editing its settings (plan, tasks 26 and 30; TZ 5.8, 3.4).

Two operations live here and they sit on opposite sides of the venue scope.

**Creating one (task 26, decisions A3, B1, D3).** :meth:`VenueService.create` is the whole
wizard in one call: ``venues``, ``venue_settings``, the owner's ``venue_members`` row and the
two checklist templates appear together or not at all. It is one call on purpose — the
transaction is the caller's (``session_scope`` commits when the handler returns), so a wizard
that wrote the venue in one method and the membership in another would leave a venue nobody
can enter the moment anything between them raised. Decision D3 is exactly that promise: after
the wizard there is a *real* ``venue_members(role='owner')`` row, and ``OWNER_TELEGRAM_IDS``
stops mattering because TZ 2 reads rights from the database and from nowhere else.

The two templates are created **empty** (decision B1): ``version=1``, ``is_active=true``, zero
items. TZ 5.4's "a default template is set up at the start" is read as "at the moment the
venue is created", not "in the delivery" — the other reading contradicts principle 1.4#6 and
acceptance 11.5 head on. Rule 8.1, "a checklist with no items is not sent at all, the manager
is told the template is empty", already assumes an empty template exists.

**Editing them (task 30).** :meth:`VenueService.update_settings` is the settings screen of
TZ 5.8. One thing about it is easy to get wrong and is therefore spelled out below: the
timezone lives on ``venues.timezone``, not on ``venue_settings``, so changing it writes a
different table and produces its own ``audit_log`` record against ``venues``.

Three rules this module keeps
-----------------------------

* **it takes factories, not a scoped repository.** Same exception as
  :mod:`src.services.access` and :mod:`src.services.members`, and for the first module's
  reason: at the moment the wizard runs there is no ``venue_id`` yet to build a scoped
  repository from — producing one is the job. Where the boundary between the two shapes of
  service runs is written down once, in the module docstring of :mod:`src.services.access`;
* **the trail is built after the venue exists.** :class:`~src.services.audit.AuditTrail` is
  the venue's own append-only log, and there is no venue-scoped sink to record into until
  ``venues.id`` is there — which is precisely the first of the two callers
  :data:`~src.services.audit.SILENT` exists for. So the rows are written first and the five
  records are appended afterwards, all carrying the freshly built owner context;
* **it never commits.** Not once, not on the happy path either. The caller owns the
  transaction.

The IANA zone that TZ 3.4 gives every venue is validated by
:func:`~src.services.timezones.venue_timezone` before a row is written, and the error it
raises — :class:`~src.services.timezones.UnknownTimezoneError` — is re-exported here rather
than declared again: a handler catching "the timezone is wrong" must not have to know whether
the wizard or the scheduler noticed it first.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from typing import Any, Final, Protocol

from src.db.models import (
    ChecklistTemplate,
    ChecklistType,
    MemberRole,
    User,
    Venue,
    VenueMember,
    VenueSettings,
)
from src.db.repositories.protocols import (
    ChecklistTemplateRepository,
    UserRepository,
    VenueMemberRepository,
    VenueRepository,
    VenueSettingsRepository,
)
from src.services.access import (
    AccessContext,
    AccessError,
    _context,
    require_manager,
    require_venue,
)
from src.services.audit import AuditEntity, AuditSink, AuditTrail, snapshot
from src.services.timezones import UnknownTimezoneError, venue_timezone

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Decision B1: the wizard sets up both checklists of stage 0 and leaves them empty.
#: `ChecklistType.STOCK` is not here — TZ 5.4 ties it to the settings rather than to a
#: shift, and stage 0 does not schedule it (decision D1).
WIZARD_TEMPLATE_TYPES: Final = (ChecklistType.OPENING, ChecklistType.CLOSING)

#: Decision B1 again: the first version is 1, and B3 forks a new one on the first edit.
FIRST_TEMPLATE_VERSION: Final = 1

#: The one field of the settings screen that is not a column of `venue_settings` (TZ 4.1).
TIMEZONE_FIELD: Final = "timezone"


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class VenueError(AccessError):
    """Base class for everything this module refuses to do."""


class UnknownOwnerError(VenueError):
    """The wizard was handed a `users.id` that no row answers to.

    Decision A3 puts a `users` row in front of the wizard — `/start` from a bootstrap id
    creates the person first — so this is a caller mistake, not a state the flow can reach.
    """

    def __init__(self, user_id: int) -> None:
        super().__init__(f"user {user_id} does not exist, so the venue would have no owner")
        self.user_id = user_id


class UnknownVenueError(VenueError):
    """The actor's venue is not in the database (a deleted venue, a stale FSM state)."""

    def __init__(self, venue_id: int) -> None:
        super().__init__(f"venue {venue_id} does not exist")
        self.venue_id = venue_id


class VenueSettingsMissingError(VenueError):
    """No `venue_settings` row: the venue did not come through the wizard (task 26).

    Declared here and not imported from :mod:`src.services.notifications`, which spells the
    same refusal for its own reason. Sharing it would make the two services import each
    other, and a settings screen has no business depending on the notification queue.
    """

    def __init__(self, venue_id: int) -> None:
        super().__init__(f"venue {venue_id} has no settings row to edit")
        self.venue_id = venue_id


# --------------------------------------------------------------------------------------
# The zones the wizard offers as buttons (TZ 3.4, plan task 26)
# --------------------------------------------------------------------------------------


class WizardTimezone(enum.StrEnum):
    """IANA identifiers of the zones the create-venue wizard shows as buttons.

    **Why this is configuration and not content.** Principle 1.4#6 and acceptance 11.5 say
    the system ships empty: not a recipe, not a product, not a checklist item until a venue
    enters its own. None of those describe this list. An IANA identifier is a key of the
    time zone database the runtime already carries — the same kind of system vocabulary as
    a role name or a table name, and the same kind of thing `OWNER_TELEGRAM_IDS` is under
    decision A3. It is also not the venue's data in any sense the schema recognises: what
    the venue owns is the single string in `venues.timezone`, and this list is only the
    keyboard it was picked from. Nothing here reaches a `venues`, `recipes` or
    `checklist_items` row on its own.

    Written as an enum for the same reason :class:`~src.services.audit.AuditEntity` is: a
    closed list of identifiers the code must name is a type, not a table of rows. TZ 3.4
    demands an IANA name and principle 1.4#5 demands buttons wherever a choice can be one,
    so the two together are exactly this: the offsets Russia and the CIS actually use, one
    representative zone each. A venue outside them is a wizard change, not a code change to
    every screen — the column keeps any IANA name, and only the *keyboard* is this list.

    **The order is the contract.** `TimezoneChoice` carries an index rather than a name (see
    `src/bot/callbacks.py`: the widest zone name is over half the callback budget), and a
    message from before a redeploy still holds the old index. So entries are appended, never
    reordered or removed — the same discipline as an enum written into a column.

    Every member is checked against `zoneinfo.available_timezones()` by
    `tests/services/test_venues.py`; a typo here would otherwise only show up when a real
    venue tried to open its first shift.
    """

    KALININGRAD = "Europe/Kaliningrad"
    MOSCOW = "Europe/Moscow"
    SAMARA = "Europe/Samara"
    YEKATERINBURG = "Asia/Yekaterinburg"
    OMSK = "Asia/Omsk"
    KRASNOYARSK = "Asia/Krasnoyarsk"
    IRKUTSK = "Asia/Irkutsk"
    YAKUTSK = "Asia/Yakutsk"
    VLADIVOSTOK = "Asia/Vladivostok"
    MAGADAN = "Asia/Magadan"
    KAMCHATKA = "Asia/Kamchatka"
    MINSK = "Europe/Minsk"
    KYIV = "Europe/Kyiv"
    CHISINAU = "Europe/Chisinau"
    TBILISI = "Asia/Tbilisi"
    YEREVAN = "Asia/Yerevan"
    BAKU = "Asia/Baku"
    ALMATY = "Asia/Almaty"
    TASHKENT = "Asia/Tashkent"
    ASHGABAT = "Asia/Ashgabat"
    DUSHANBE = "Asia/Dushanbe"
    BISHKEK = "Asia/Bishkek"


_WIZARD_TIMEZONES: Final = tuple(str(zone) for zone in WizardTimezone)


def wizard_timezones() -> tuple[str, ...]:
    """The zones the wizard offers, in the order their buttons are drawn (TZ 3.4)."""
    return _WIZARD_TIMEZONES


def timezone_at(index: int) -> str | None:
    """The zone a `TimezoneChoice` names, or `None` when the index names nothing.

    `None` rather than an exception, for the reason every repository answers `None` to a
    foreign id (TZ 9): the number arrives inside a `callback_data` that anybody can retype,
    and a wizard that re-draws the keyboard is a better answer than a traceback.
    """
    if 0 <= index < len(_WIZARD_TIMEZONES):
        return _WIZARD_TIMEZONES[index]
    return None


# --------------------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VenueCreation:
    """Everything the wizard produced, including the rights its owner now holds.

    `owner` is handed back because the person who ran the wizard had no venue a moment ago
    and therefore no :class:`~src.services.access.AccessContext`; the caller needs one to
    render the very next screen, and re-resolving the identity to obtain it would be a
    second read of the rows this call just wrote.
    """

    venue: Venue
    settings: VenueSettings
    member: VenueMember
    templates: tuple[ChecklistTemplate, ...]
    owner: AccessContext

    @property
    def venue_id(self) -> int:
        return self.venue.id


@dataclass(frozen=True, slots=True)
class VenueConfiguration:
    """The pair the settings screen renders: the venue and its settings row (TZ 5.8).

    Both, because the screen edits both — the timezone is a column of `venues` and every
    other field is a column of `venue_settings`.
    """

    venue: Venue
    settings: VenueSettings


# --------------------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------------------


class VenueCreationRepositories(Protocol):
    """The data this service reaches for, as factories rather than as objects.

    `users` and `venues` are global tables and arrive as they are. The other four are
    venue-scoped, and a venue-scoped repository carries its venue in the object (TZ 3.3) —
    which cannot be given at construction here, because the venue does not exist until
    :meth:`VenueService.create` has written it. Same shape and same reason as
    :class:`~src.services.access.AccessRepositories`.

    `audit` returns an :class:`~src.services.audit.AuditSink` and not the full repository
    protocol: the trail appends and never reads, so a service holding one cannot use it to
    look into another venue's history.
    """

    @property
    def users(self) -> UserRepository: ...

    @property
    def venues(self) -> VenueRepository: ...

    def settings(self, venue_id: int) -> VenueSettingsRepository: ...

    def members(self, venue_id: int) -> VenueMemberRepository: ...

    def templates(self, venue_id: int) -> ChecklistTemplateRepository: ...

    def audit(self, venue_id: int) -> AuditSink: ...


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------


class VenueService:
    """The create-venue wizard and the settings screen (plan, tasks 26 and 30)."""

    def __init__(self, repositories: VenueCreationRepositories) -> None:
        self.repositories = repositories

    # -- creating (task 26) --------------------------------------------------------------

    async def create(
        self,
        owner: User | int,
        *,
        name: str,
        city: str,
        timezone: str,
        default_shift_start: dt.time,
        default_shift_end: dt.time,
    ) -> VenueCreation:
        """Raise a venue from nothing, in one transaction the caller commits (task 26).

        Takes the owner as a row or as a `users.id` because both callers exist: the wizard
        holds the `User` it created on `/start` (decision A3), and a future web admin will
        hold only an id.

        There is no permission check and that is deliberate. Rights are read from
        `venue_members` (TZ 2) and this call is what creates the first such row, so there is
        nothing yet to check against; who is allowed to reach the wizard at all is decided
        one layer up by `Identity.may_create_venue`, which spends the `OWNER_TELEGRAM_IDS`
        trigger the moment the person belongs anywhere (decision A3).

        The order of the writes is the order of the foreign keys: the venue first, then
        everything that points at it. The audit records come last, once there is a venue to
        record into and an owner context to sign them with.
        """
        user = await self._require_user(owner)
        # Validated before the first write: an unknown zone must not leave a half-built
        # venue behind for the caller's rollback to clean up (TZ 3.4).
        venue_timezone(timezone)

        venue = await self.repositories.venues.create(
            name=_required_text(name, "name"),
            city=_required_text(city, "city"),
            timezone=timezone,
        )
        settings = await self.repositories.settings(venue.id).create(
            default_shift_start=default_shift_start,
            default_shift_end=default_shift_end,
        )
        # `is_active` and `joined_at` come from the schema defaults, which is what makes this
        # the full membership row decision D3 asks for rather than a placeholder.
        member = await self.repositories.members(venue.id).add(
            user_id=user.id,
            role=MemberRole.OWNER,
        )
        templates = await self._create_empty_templates(venue.id, updated_by=user.id)

        owner_context = _context(user, member)
        await self._record_creation(owner_context, venue, settings, member, templates)
        return VenueCreation(
            venue=venue,
            settings=settings,
            member=member,
            templates=templates,
            owner=owner_context,
        )

    # -- editing (task 30) ---------------------------------------------------------------

    async def update_settings(self, actor: AccessContext, **fields: Any) -> VenueConfiguration:
        """Change the venue's settings, recording only what actually moved (TZ 5.8).

        Manager and above (TZ 2). Only the fields named by the caller are touched, so the
        screen can save one row of itself without carrying the rest along; an unknown column
        is a typo and the repository says so rather than writing nothing quietly.

        `timezone` is accepted here together with the rest because it is one screen to the
        manager, but it is a column of `venues` — so it is routed to a different repository
        and gets its own `audit_log` record against `AuditEntity.VENUE`. Everything else is
        one record against `AuditEntity.VENUE_SETTINGS`, whose `entity_id` is the venue id:
        `venue_settings` has no id of its own, `venue_id` is its primary key (TZ 4.1).

        A change that sets a field to the value it already had writes no record at all — the
        trail decides that from the diff, and a log full of empty diffs is a log nobody
        reads (see `src/services/audit.py`).
        """
        require_manager(actor)
        venue = await self.repositories.venues.get(actor.venue_id)
        if venue is None:
            raise UnknownVenueError(actor.venue_id)
        # `VenueRepo` is the one repository of the project that is not venue-scoped — the
        # table *is* the scope — so the guard of TZ 9 is spelled out here instead of being
        # implied by a `WHERE` the way it is everywhere else.
        require_venue(actor, venue.id)

        settings_repo = self.repositories.settings(actor.venue_id)
        settings = await settings_repo.get()
        if settings is None:
            raise VenueSettingsMissingError(actor.venue_id)

        trail = AuditTrail(self.repositories.audit(actor.venue_id))
        rest = dict(fields)
        if TIMEZONE_FIELD in rest:
            venue = await self._update_timezone(actor, venue, trail, str(rest.pop(TIMEZONE_FIELD)))
        if rest:
            settings = await self._update_settings_row(actor, settings_repo, trail, rest)
        return VenueConfiguration(venue=venue, settings=settings)

    # -- internals -----------------------------------------------------------------------

    async def _require_user(self, owner: User | int) -> User:
        if isinstance(owner, User):
            return owner
        user = await self.repositories.users.get(owner)
        if user is None:
            raise UnknownOwnerError(owner)
        return user

    async def _create_empty_templates(
        self,
        venue_id: int,
        *,
        updated_by: int,
    ) -> tuple[ChecklistTemplate, ...]:
        """Decision B1: both templates exist from minute one and both hold zero items.

        The `name` column is filled with the type — `opening`, `closing` — and not with a
        heading. The heading a manager reads is interface wording and lives in
        `src/bot/texts/` (TZ 8.2); writing one into the table would ship a phrase with the
        product and make the venue's first row content it never asked for (TZ 1.4#6).
        """
        templates = self.repositories.templates(venue_id)
        created: list[ChecklistTemplate] = []
        for checklist_type in WIZARD_TEMPLATE_TYPES:
            created.append(
                await templates.create(
                    checklist_type=checklist_type,
                    name=str(checklist_type),
                    version=FIRST_TEMPLATE_VERSION,
                    updated_by=updated_by,
                )
            )
        return tuple(created)

    async def _record_creation(
        self,
        actor: AccessContext,
        venue: Venue,
        settings: VenueSettings,
        member: VenueMember,
        templates: tuple[ChecklistTemplate, ...],
    ) -> None:
        """The five records of a creation, all signed by the owner the wizard just made."""
        trail = AuditTrail(self.repositories.audit(venue.id))
        await trail.created(
            actor,
            AuditEntity.VENUE,
            venue.id,
            after=snapshot(venue, "name", "city", TIMEZONE_FIELD),
        )
        await trail.created(
            actor,
            AuditEntity.VENUE_SETTINGS,
            venue.id,
            after=snapshot(settings, "default_shift_start", "default_shift_end"),
        )
        await trail.created(
            actor,
            AuditEntity.MEMBER,
            member.id,
            after=snapshot(member, "user_id", "role", "is_active"),
        )
        for template in templates:
            await trail.created(
                actor,
                AuditEntity.CHECKLIST_TEMPLATE,
                template.id,
                after=snapshot(template, "type", "version", "is_active"),
            )

    async def _update_timezone(
        self,
        actor: AccessContext,
        venue: Venue,
        trail: AuditTrail,
        name: str,
    ) -> Venue:
        """TZ 3.4 lives on `venues.timezone`, so it is a separate write and a separate record."""
        venue_timezone(name)
        # Read before writing: `update` mutates the very row `venue` points at (the identity
        # map hands back one object per id), so a "before" taken afterwards would be the
        # value this call just wrote — and every diff would come out empty.
        before = snapshot(venue, TIMEZONE_FIELD)
        updated = await self.repositories.venues.update(venue.id, timezone=name)
        if updated is None:
            raise UnknownVenueError(venue.id)
        await trail.updated(
            actor,
            AuditEntity.VENUE,
            updated.id,
            before=before,
            after=snapshot(updated, TIMEZONE_FIELD),
        )
        return updated

    async def _update_settings_row(
        self,
        actor: AccessContext,
        repository: VenueSettingsRepository,
        trail: AuditTrail,
        fields: dict[str, Any],
    ) -> VenueSettings:
        current = await repository.get()
        if current is None:
            raise VenueSettingsMissingError(actor.venue_id)
        before = snapshot(current, *fields)
        updated = await repository.update(**fields)
        if updated is None:
            raise VenueSettingsMissingError(actor.venue_id)
        await trail.updated(
            actor,
            AuditEntity.VENUE_SETTINGS,
            actor.venue_id,
            before=before,
            after=snapshot(updated, *fields),
        )
        return updated


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _required_text(value: str, field: str) -> str:
    """A name typed into the wizard, trimmed; blank is a refusal rather than a blank row."""
    cleaned = value.strip()
    if not cleaned:
        raise VenueError(f"{field} cannot be empty")
    return cleaned


__all__ = [
    "FIRST_TEMPLATE_VERSION",
    "TIMEZONE_FIELD",
    "WIZARD_TEMPLATE_TYPES",
    "UnknownOwnerError",
    "UnknownTimezoneError",
    "UnknownVenueError",
    "VenueConfiguration",
    "VenueCreation",
    "VenueCreationRepositories",
    "VenueError",
    "VenueService",
    "VenueSettingsMissingError",
    "WizardTimezone",
    "timezone_at",
    "wizard_timezones",
]
