"""Object factories for database tests (plan, block A, task 5a).

Rules for everyone who adds a factory here:

* **Fixture data only, never delivery data.** Names below are deliberately meaningless
  ASCII ("Venue 1", "Item 1"). Nothing in this module may look like a real recipe, product,
  supplier or checklist item — the system ships empty (TZ 1.4#6) and `tests/` is the one
  place where rows are allowed to exist at all.
* **`flush()`, never `commit()`.** The `session` fixture runs inside a transaction that is
  rolled back after the test; a factory that commits would defeat it.
* Every factory takes the parents it needs explicitly, so a test never has to guess which
  venue an object ended up in (TZ 3.3: venue isolation is the thing under test).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from itertools import count

from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import (
    ChecklistItem,
    ChecklistTemplate,
    ChecklistType,
    InviteCode,
    MemberRole,
    Recipe,
    Shift,
    ShiftSource,
    ShiftStatus,
    User,
    Venue,
    VenueMember,
    VenueSettings,
)

#: Process-wide counter that keeps unique columns (telegram_id, invite code, recipe name)
#: unique without the caller having to think about it.
_sequence = count(1)


def next_sequence() -> int:
    return next(_sequence)


#: Answer A2: the test venue works in Europe/Moscow with a 08:00-23:00 shift.
DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_SHIFT_START = dt.time(8, 0)
DEFAULT_SHIFT_END = dt.time(23, 0)

#: TZ 5.1: an invite code lives 7 days.
INVITE_CODE_LIFETIME = dt.timedelta(days=7)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def create_venue(
    session: AsyncSession,
    *,
    name: str | None = None,
    city: str = "City",
    timezone: str = DEFAULT_TIMEZONE,
    is_active: bool = True,
) -> Venue:
    number = next_sequence()
    venue = Venue(
        name=name if name is not None else f"Venue {number}",
        city=city,
        timezone=timezone,
        is_active=is_active,
    )
    session.add(venue)
    await session.flush()
    return venue


async def create_venue_settings(
    session: AsyncSession,
    venue: Venue,
    *,
    default_shift_start: dt.time = DEFAULT_SHIFT_START,
    default_shift_end: dt.time = DEFAULT_SHIFT_END,
    **overrides: object,
) -> VenueSettings:
    """Settings row for a venue. Lead times and thresholds keep their schema defaults."""
    settings = VenueSettings(
        venue_id=venue.id,
        default_shift_start=default_shift_start,
        default_shift_end=default_shift_end,
        **overrides,
    )
    session.add(settings)
    await session.flush()
    return settings


async def create_user(
    session: AsyncSession,
    *,
    telegram_id: int | None = None,
    full_name: str | None = None,
    username: str | None = None,
    is_active: bool = True,
) -> User:
    number = next_sequence()
    user = User(
        telegram_id=telegram_id if telegram_id is not None else 900_000_000 + number,
        full_name=full_name if full_name is not None else f"User {number}",
        username=username,
        is_active=is_active,
    )
    session.add(user)
    await session.flush()
    return user


async def create_venue_member(
    session: AsyncSession,
    venue: Venue,
    user: User,
    *,
    role: MemberRole = MemberRole.STAFF,
    position: str | None = None,
    is_active: bool = True,
) -> VenueMember:
    member = VenueMember(
        venue_id=venue.id,
        user_id=user.id,
        role=role,
        position=position,
        is_active=is_active,
    )
    session.add(member)
    await session.flush()
    return member


async def create_invite_code(
    session: AsyncSession,
    venue: Venue,
    *,
    code: str | None = None,
    role: MemberRole = MemberRole.STAFF,
    position: str | None = None,
    full_name: str | None = None,
    created_by: User | None = None,
    expires_at: dt.datetime | None = None,
) -> InviteCode:
    number = next_sequence()
    invite = InviteCode(
        venue_id=venue.id,
        code=code if code is not None else f"inv{number:08d}",
        role=role,
        position=position,
        # Decision B8: the manager types the full name when issuing the code.
        full_name=full_name,
        created_by=created_by.id if created_by is not None else None,
        expires_at=expires_at if expires_at is not None else _utc_now() + INVITE_CODE_LIFETIME,
    )
    session.add(invite)
    await session.flush()
    return invite


def shift_hours(start: dt.time, end: dt.time) -> Decimal:
    """Length of a shift in hours; a shift ending past midnight belongs to its start date."""
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    if minutes <= 0:
        minutes += 24 * 60
    return Decimal(minutes) / Decimal(60)


async def create_shift(
    session: AsyncSession,
    venue: Venue,
    user: User,
    *,
    shift_date: dt.date | None = None,
    start_time: dt.time = DEFAULT_SHIFT_START,
    end_time: dt.time = DEFAULT_SHIFT_END,
    hours: Decimal | None = None,
    is_opener: bool = False,
    is_closer: bool = False,
    status: ShiftStatus = ShiftStatus.PLANNED,
    source: ShiftSource = ShiftSource.MANUAL,
    created_by: User | None = None,
) -> Shift:
    shift = Shift(
        venue_id=venue.id,
        user_id=user.id,
        shift_date=shift_date if shift_date is not None else dt.date(2026, 1, 1),
        start_time=start_time,
        end_time=end_time,
        hours=hours if hours is not None else shift_hours(start_time, end_time),
        is_opener=is_opener,
        is_closer=is_closer,
        status=status,
        source=source,
        created_by=created_by.id if created_by is not None else None,
    )
    session.add(shift)
    await session.flush()
    return shift


async def create_checklist_template(
    session: AsyncSession,
    venue: Venue,
    *,
    type: ChecklistType = ChecklistType.OPENING,
    name: str | None = None,
    version: int = 1,
    is_active: bool = True,
    updated_by: User | None = None,
) -> ChecklistTemplate:
    """Decision B1: a freshly created template is active and has zero items."""
    template = ChecklistTemplate(
        venue_id=venue.id,
        type=type,
        name=name if name is not None else f"Template {next_sequence()}",
        version=version,
        is_active=is_active,
        updated_by=updated_by.id if updated_by is not None else None,
    )
    session.add(template)
    await session.flush()
    return template


async def create_checklist_item(
    session: AsyncSession,
    template: ChecklistTemplate,
    *,
    text: str | None = None,
    group_name: str | None = None,
    group_index: int = 0,
    order_index: int | None = None,
    requires_photo: bool = False,
    requires_comment: bool = False,
    is_critical: bool = False,
) -> ChecklistItem:
    number = next_sequence()
    item = ChecklistItem(
        template_id=template.id,
        text=text if text is not None else f"Item {number}",
        group_name=group_name,
        group_index=group_index,
        order_index=order_index if order_index is not None else number,
        requires_photo=requires_photo,
        requires_comment=requires_comment,
        is_critical=is_critical,
    )
    session.add(item)
    await session.flush()
    return item


async def create_recipe(
    session: AsyncSession,
    venue: Venue | None,
    *,
    name: str | None = None,
    category: str = "category-a",
    aliases: list[str] | None = None,
    glassware: str | None = None,
    method: str | None = None,
    ice: str | None = None,
    garnish: str | None = None,
    instruction: str | None = None,
    is_active: bool = True,
) -> Recipe:
    """`venue=None` creates a row of the shared BarPoint library (TZ 3.3)."""
    number = next_sequence()
    recipe = Recipe(
        venue_id=venue.id if venue is not None else None,
        name=name if name is not None else f"Recipe {number}",
        category=category,
        aliases=aliases if aliases is not None else [],
        glassware=glassware,
        method=method,
        ice=ice,
        garnish=garnish,
        instruction=instruction,
        is_active=is_active,
    )
    session.add(recipe)
    await session.flush()
    return recipe
