"""Notification service (plan, task 19; TZ 6, 5.4, 5.5; acceptance 11.2 and 11.4).

The fakes below reproduce the two properties the queue is built on, because everything
this service promises rests on them:

* :meth:`FakeNotifications.upsert_scheduled` behaves like the partial unique index on
  ``dedup_key`` (schema notes): a *live* row — ``scheduled`` or ``sending`` — with the same
  key is moved, anything else is history and a new row is inserted. That is what makes
  replanning a shift move one message instead of queueing a second one;
* :meth:`FakeNotifications.cancel_for_entity` touches live rows only, so a delivered
  notification is not retroactively "cancelled" when the shift it belonged to is deleted.

And it is scoped to one venue over a table every venue writes into, because that is the
shape of ``NotificationRepo``: every statement there carries ``AND venue_id = :venue_id``,
including the ``ON CONFLICT DO UPDATE`` of ``upsert_scheduled`` — a live key held by another
venue is not re-armed, the ``RETURNING`` comes back empty and the repository raises
``NoResultFound`` instead of silently moving somebody else's message. The tests under "one
installation, many venues" are what keeps that predicate alive here: without them it is code
no assertion depends on, and a fake that quietly drops it makes every test above green about
an isolation production does not have (TZ 3.3, TZ 9, acceptance 11.3).

Every test pins the clock with ``use_clock(FixedClock(...))``: the service reads time
through ``src/services/timezones.py`` and nowhere else (decision D12), which is what makes
``scheduled_at`` an assertable value rather than an approximation.

The DST cases run on a second venue in Europe/Berlin. Europe/Moscow — the timezone of the
test venue (answer A2) — has not shifted since 2014, so the same test written on it would
be evergreen; ``tests/services/test_timezones.py`` states that in code.
"""

from __future__ import annotations

import ast
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.exc import NoResultFound
from src.db.models import (
    ChecklistType,
    Notification,
    NotificationStatus,
    Shift,
    ShiftSource,
    ShiftStatus,
    User,
    Venue,
    VenueSettings,
)
from src.db.models.service import ACTIVE_NOTIFICATION_STATUSES
from src.services.checklists import (
    ChecklistNotifier,
    EmptyTemplateAlert,
    PendingItem,
    PendingItemsAlert,
)
from src.services.notifications import (
    CHECKLIST_RUN_ENTITY,
    SHIFT_ENTITY,
    DuplicateNotificationTypeError,
    ForeignShiftError,
    NotificationRegistry,
    NotificationService,
    NotificationSpec,
    NotificationType,
    OutgoingMessage,
    RendererNotRegisteredError,
    ScheduleOutcome,
    UnknownNotificationTypeError,
    dedup_key,
    opening_checklist_moment,
    payload_items,
)
from src.services.recipes import MissingRecipeAlert, RecipeNotifier
from src.services.shifts import ShiftNotifier
from src.services.timezones import UTC, FixedClock, use_clock

from tests.repo_scan import SRC_DIR, read, relative

VENUE_ID = 1
#: The bar next door: its rows live in the same table and must be unreachable from here.
OTHER_VENUE_ID = 2
OPENER_ID = 7
OPENER_CHAT_ID = 917323199
STAND_IN_ID = 8
STAND_IN_CHAT_ID = 917323200
MANAGER_ID = 9
MANAGER_CHAT_ID = 1672818749
SECOND_MANAGER_ID = 10
SECOND_MANAGER_CHAT_ID = 1672818750
SHIFT_ID = 42
RUN_ID = 55

MOSCOW = "Europe/Moscow"
BERLIN = "Europe/Berlin"

#: A quiet moment three days before every scheduled shift in this module, so that "the
#: computed moment is in the future" is the default and the past is asked for explicitly.
NOW = dt.datetime(2026, 8, 10, 6, 0, tzinfo=UTC)
SHIFT_DATE = dt.date(2026, 8, 13)

SERVICE_MODULE = SRC_DIR / "services" / "notifications.py"


# --------------------------------------------------------------------------------------
# Model factories (declared here on purpose: tests/factories.py belongs to task 5a)
# --------------------------------------------------------------------------------------


def make_venue(timezone: str = MOSCOW, *, venue_id: int = VENUE_ID) -> Venue:
    return Venue(id=venue_id, name="venue", city="city", timezone=timezone, is_active=True)


def make_settings(lead_minutes: int = 10) -> VenueSettings:
    return VenueSettings(
        venue_id=VENUE_ID,
        opening_checklist_lead_minutes=lead_minutes,
        closing_checklist_lead_minutes=30,
        shift_reminder_hours=12,
        checklist_overdue_minutes=60,
        escalate_to_manager=True,
        default_shift_start=dt.time(8, 0),
        default_shift_end=dt.time(23, 0),
    )


def make_user(user_id: int, telegram_id: int) -> User:
    return User(id=user_id, telegram_id=telegram_id, full_name=f"user-{user_id}", is_active=True)


def make_shift(
    *,
    shift_id: int = SHIFT_ID,
    user_id: int = OPENER_ID,
    shift_date: dt.date = SHIFT_DATE,
    start_time: dt.time = dt.time(9, 0),
    end_time: dt.time = dt.time(21, 0),
    is_opener: bool = True,
    status: ShiftStatus = ShiftStatus.PLANNED,
) -> Shift:
    return Shift(
        id=shift_id,
        venue_id=VENUE_ID,
        user_id=user_id,
        shift_date=shift_date,
        start_time=start_time,
        end_time=end_time,
        hours=Decimal("12"),
        is_opener=is_opener,
        is_closer=False,
        status=status,
        source=ShiftSource.MANUAL,
    )


def make_pending_alert() -> PendingItemsAlert:
    """Critical first, exactly as the checklist service hands it over (TZ 6)."""
    return PendingItemsAlert(
        run_id=RUN_ID,
        checklist_type=ChecklistType.OPENING,
        user_id=OPENER_ID,
        completed_by=OPENER_ID,
        skip_comment="no delivery",
        items=(
            PendingItem(item_id=3, text="critical one", group_name="station", is_critical=True),
            PendingItem(item_id=1, text="ordinary one", group_name="visual", is_critical=False),
        ),
    )


# --------------------------------------------------------------------------------------
# Fake repositories
# --------------------------------------------------------------------------------------


class FakeVenues:
    """`VenueRepository` — the service reads the timezone and nothing else.

    Holds a table, not a single row, like the real one. With one row the lookup cannot be
    wrong: whatever it returns is the only venue there is, so a service that ignored the id
    and took the nearest venue would still look correct. A neighbour makes it answerable.
    """

    def __init__(self, venues: Sequence[Venue], table: dict[int, Venue] | None = None) -> None:
        self.table = {} if table is None else table
        for venue in venues:
            self.table[venue.id] = venue

    async def get(self, venue_id: int) -> Venue | None:
        return self.table.get(venue_id)

    async def list_for_user(self, user_id: int) -> Sequence[Venue]:
        raise NotImplementedError

    async def create(self, *, name: str, city: str, timezone: str) -> Venue:
        raise NotImplementedError

    async def update(self, venue_id: int, **fields: Any) -> Venue | None:
        raise NotImplementedError


class FakeSettings:
    """`VenueSettingsRepository`; the lead time is read fresh on every scheduling call.

    `VenueSettingsRepo.get()` is `for_venue()` — the venue lives in the `WHERE`, not in the
    call. So the fake keeps one table for every venue and answers only for its own scope;
    holding a single row would make the predicate unwritable and the leak unnoticeable.
    """

    def __init__(
        self,
        settings: VenueSettings | None,
        *,
        venue_id: int,
        table: dict[int, VenueSettings] | None = None,
    ) -> None:
        self.venue_id = venue_id
        self.table = {} if table is None else table
        if settings is not None:
            self.table[venue_id] = settings

    async def get(self) -> VenueSettings | None:
        return self.table.get(self.venue_id)

    async def create(
        self,
        *,
        default_shift_start: dt.time,
        default_shift_end: dt.time,
        **fields: Any,
    ) -> VenueSettings:
        raise NotImplementedError

    async def update(self, **fields: Any) -> VenueSettings | None:
        raise NotImplementedError


class FakeUsers:
    """`UserRepository` — used only to turn a user id into a personal chat id."""

    def __init__(self, users: Sequence[User]) -> None:
        self.users = {user.id: user for user in users}

    async def get(self, user_id: int) -> User | None:
        return self.users.get(user_id)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        raise NotImplementedError

    async def create(
        self,
        *,
        telegram_id: int,
        full_name: str,
        username: str | None = None,
        phone: str | None = None,
    ) -> User:
        raise NotImplementedError

    async def update(self, user_id: int, **fields: Any) -> User | None:
        raise NotImplementedError

    async def touch_last_seen(self, user_id: int, moment: dt.datetime) -> None:
        raise NotImplementedError

    async def set_active_venue(self, user_id: int, venue_id: int | None) -> None:
        raise NotImplementedError

    async def set_bot_blocked(self, user_id: int, *, blocked: bool) -> None:
        raise NotImplementedError


class FakeNotifications:
    """`NotificationRepository`, with the partial unique index reproduced in Python.

    The index is reproduced *as it is declared*: `uq_notifications_dedup_key` is one column
    over the whole table, not one per venue. Two repositories built on the same `store`
    therefore compete for keys exactly as two venues do in PostgreSQL, which is what
    `test_the_same_event_in_two_venues_is_two_rows` needs in order to be able to fail.
    """

    def __init__(self, venue_id: int = VENUE_ID, store: list[Notification] | None = None) -> None:
        self.venue_id = venue_id
        self.rows: list[Notification] = store if store is not None else []

    # -- the two methods the service uses ----------------------------------------------

    async def upsert_scheduled(
        self,
        *,
        dedup_key: str,
        notification_type: str,
        scheduled_at: dt.datetime,
        user_id: int | None,
        chat_id: int | None,
        payload: dict[str, Any],
        entity: str | None = None,
        entity_id: int | None = None,
    ) -> Notification:
        live = next(
            (
                row
                for row in self.rows
                if row.dedup_key == dedup_key and row.status in ACTIVE_NOTIFICATION_STATUSES
            ),
            None,
        )
        if live is not None and live.venue_id != self.venue_id:
            # `NotificationRepo.upsert_scheduled` carries the venue inside the conflict
            # clause: `ON CONFLICT (dedup_key) ... WHERE venue_id = :venue_id`. The insert
            # loses to the index, the update matches nothing, `RETURNING` comes back empty
            # — and the repository raises rather than pretending the row was queued.
            # Re-arming here instead would move the neighbour's message and drop this
            # venue's, which is the one outcome TZ 3.3 exists to prevent.
            raise NoResultFound(
                f"dedup_key {dedup_key!r} is held by a live notification of another venue"
            )
        if live is not None:
            # The `set_` of the real statement, column for column: the row goes out again,
            # so the claim and the last error are dropped and `attempts` — the history of
            # what has already been tried — is kept.
            live.type = notification_type
            live.scheduled_at = scheduled_at
            live.user_id = user_id
            live.chat_id = chat_id
            live.payload = payload
            live.entity = entity
            live.entity_id = entity_id
            live.status = NotificationStatus.SCHEDULED
            live.claimed_at = None
            live.error = None
            return live

        row = Notification(
            id=max((existing.id for existing in self.rows), default=0) + 1,
            venue_id=self.venue_id,
            user_id=user_id,
            chat_id=chat_id,
            type=notification_type,
            payload=payload,
            scheduled_at=scheduled_at,
            status=NotificationStatus.SCHEDULED,
            dedup_key=dedup_key,
            attempts=0,
            entity=entity,
            entity_id=entity_id,
        )
        self.rows.append(row)
        return row

    async def cancel_for_entity(self, *, entity: str, entity_id: int) -> int:
        cancelled = 0
        for row in self.rows:
            if row.venue_id != self.venue_id:
                continue
            if row.entity != entity or row.entity_id != entity_id:
                continue
            if row.status not in ACTIVE_NOTIFICATION_STATUSES:
                continue
            row.status = NotificationStatus.CANCELLED
            cancelled += 1
        return cancelled

    # -- the worker's half of the contract, unused here ---------------------------------
    #
    # Unused, but not free-form: this class is handed to `NotificationService`, whose
    # parameter is typed `NotificationRepository`, so mypy checks every signature below
    # against the protocol. `claim_batch` stamps the claim token into `claimed_at` and
    # `mark_sent` / `mark_failed` only conclude the claim that carries it (criterion 11.4),
    # which is why those two take it as a keyword-only argument here as well. The bodies
    # stay `NotImplementedError` on purpose: the service under test never claims or
    # delivers — that is the worker's half — and a fake that pretended to would be one more
    # thing able to disagree with `NotificationRepo` in silence.

    async def get(self, notification_id: int) -> Notification | None:
        return next(
            (
                row
                for row in self.rows
                if row.id == notification_id and row.venue_id == self.venue_id
            ),
            None,
        )

    async def claim_batch(self, *, now: dt.datetime, limit: int) -> Sequence[Notification]:
        raise NotImplementedError

    async def reclaim_stale(self, *, older_than: dt.datetime) -> int:
        raise NotImplementedError

    async def mark_sent(
        self,
        notification_id: int,
        moment: dt.datetime,
        *,
        claimed_at: dt.datetime | None,
    ) -> None:
        raise NotImplementedError

    async def mark_failed(
        self,
        notification_id: int,
        error: str,
        *,
        claimed_at: dt.datetime | None,
    ) -> None:
        raise NotImplementedError

    async def reschedule(self, notification_id: int, scheduled_at: dt.datetime) -> None:
        raise NotImplementedError

    async def list_for_entity(self, *, entity: str, entity_id: int) -> Sequence[Notification]:
        return [
            row
            for row in self.rows
            if row.venue_id == self.venue_id and row.entity == entity and row.entity_id == entity_id
        ]

    # -- helpers for the assertions -----------------------------------------------------

    @property
    def live(self) -> list[Notification]:
        return [row for row in self.rows if row.status in ACTIVE_NOTIFICATION_STATUSES]

    def of_type(self, notification_type: NotificationType) -> list[Notification]:
        return [row for row in self.rows if row.type == str(notification_type)]


class FakeRecipients:
    """Answer A4 as the service sees it: a list of user ids, resolved elsewhere."""

    def __init__(self, user_ids: Sequence[int]) -> None:
        self.user_ids = tuple(user_ids)
        self.calls: list[int] = []

    async def manager_recipient_ids(self, venue_id: int) -> tuple[int, ...]:
        self.calls.append(venue_id)
        return self.user_ids


# --------------------------------------------------------------------------------------
# Test stand
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Stand:
    service: NotificationService
    notifications: FakeNotifications
    venues: FakeVenues
    settings: FakeSettings
    users: FakeUsers
    recipients: FakeRecipients

    def set_lead(self, minutes: int) -> None:
        own = self.settings.table[self.settings.venue_id]
        own.opening_checklist_lead_minutes = minutes


def build(
    *,
    venue_id: int = VENUE_ID,
    timezone: str = MOSCOW,
    lead_minutes: int = 10,
    recipients: Sequence[int] = (MANAGER_ID,),
    registry: NotificationRegistry | None = None,
    store: list[Notification] | None = None,
    venue_table: dict[int, Venue] | None = None,
    settings_table: dict[int, VenueSettings] | None = None,
) -> Stand:
    """One venue's service.

    `store` shares the queue between two of them (see below); `venue_table` and
    `settings_table` do the same for the venue directory and its settings, so a test can
    stand two venues side by side and check that neither reads the other's row.
    """
    venues = FakeVenues([make_venue(timezone, venue_id=venue_id)], table=venue_table)
    settings = FakeSettings(make_settings(lead_minutes), venue_id=venue_id, table=settings_table)
    users = FakeUsers(
        [
            make_user(OPENER_ID, OPENER_CHAT_ID),
            make_user(STAND_IN_ID, STAND_IN_CHAT_ID),
            make_user(MANAGER_ID, MANAGER_CHAT_ID),
            make_user(SECOND_MANAGER_ID, SECOND_MANAGER_CHAT_ID),
        ]
    )
    notifications = FakeNotifications(venue_id=venue_id, store=store)
    people = FakeRecipients(recipients)
    service = NotificationService(
        venue_id=venue_id,
        venues=venues,
        settings=settings,
        users=users,
        notifications=notifications,
        recipients=people,
        registry=registry,
    )
    return Stand(
        service=service,
        notifications=notifications,
        venues=venues,
        settings=settings,
        users=users,
        recipients=people,
    )


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# The scheduled type: when the opening checklist goes out (acceptance 11.2, test 37)
# --------------------------------------------------------------------------------------


async def test_ordinary_shift_is_queued_lead_minutes_before_the_start() -> None:
    """09:00 in Moscow with a lead of ten minutes: 08:50 local, 05:50 UTC."""
    stand = build()
    with use_clock(FixedClock(NOW)):
        result = await stand.service.shift_saved(make_shift())

    assert result.outcome is ScheduleOutcome.SCHEDULED
    assert result.scheduled_at == utc(2026, 8, 13, 5, 50)
    row = stand.notifications.rows[0]
    assert row.type == str(NotificationType.OPENING_CHECKLIST)
    assert row.scheduled_at == utc(2026, 8, 13, 5, 50)
    assert row.status is NotificationStatus.SCHEDULED


async def test_the_row_carries_the_recipient_and_the_object_it_is_about() -> None:
    stand = build()
    with use_clock(FixedClock(NOW)):
        await stand.service.shift_saved(make_shift())

    row = stand.notifications.rows[0]
    assert row.user_id == OPENER_ID
    # The chat is resolved here so that the worker sends without a lookup of its own.
    assert row.chat_id == OPENER_CHAT_ID
    assert (row.entity, row.entity_id) == (SHIFT_ENTITY, SHIFT_ID)
    assert row.dedup_key == dedup_key(
        VENUE_ID, NotificationType.OPENING_CHECKLIST, SHIFT_ENTITY, SHIFT_ID
    )
    assert row.dedup_key.startswith(f"{VENUE_ID}:"), "the venue leads every key"
    assert row.payload["shift_id"] == SHIFT_ID
    assert row.payload["checklist_type"] == ChecklistType.OPENING.value


async def test_shift_that_ends_exactly_at_midnight() -> None:
    """14:00-00:00 — the end is tomorrow's 00:00, and the push is still 13:50 today."""
    stand = build()
    shift = make_shift(start_time=dt.time(14, 0), end_time=dt.time(0, 0))
    with use_clock(FixedClock(NOW)):
        result = await stand.service.shift_saved(shift)

    assert result.scheduled_at == utc(2026, 8, 13, 10, 50)


async def test_shift_that_ends_at_midnight_is_still_live_at_half_past_eleven() -> None:
    """The window is what decides "over", so 23:30 local is inside a 14:00-00:00 shift."""
    stand = build()
    shift = make_shift(start_time=dt.time(14, 0), end_time=dt.time(0, 0))
    with use_clock(FixedClock(utc(2026, 8, 13, 20, 30))):
        result = await stand.service.shift_saved(shift)

    assert result.outcome is ScheduleOutcome.IMMEDIATE
    assert result.scheduled_at == utc(2026, 8, 13, 20, 30)


async def test_night_shift_belongs_to_the_date_it_starts_on() -> None:
    """18:00-02:00: the push is 17:50 on the shift date, 14:50 UTC."""
    stand = build()
    shift = make_shift(start_time=dt.time(18, 0), end_time=dt.time(2, 0))
    with use_clock(FixedClock(NOW)):
        result = await stand.service.shift_saved(shift)

    assert result.scheduled_at == utc(2026, 8, 13, 14, 50)


async def test_shift_starting_after_midnight_is_pushed_on_the_previous_utc_day() -> None:
    """00:10 in Moscow is 21:10 UTC *the day before*, and the push is earlier still.

    With a lead of ten minutes the local reading is still 00:00 of the shift date; with
    twenty it moves to 23:50 of the day before. Both are the same instant arithmetic, and
    both land on the previous UTC day — which is the whole reason the queue stores UTC.
    """
    stand = build()
    shift = make_shift(start_time=dt.time(0, 10), end_time=dt.time(8, 0))
    with use_clock(FixedClock(NOW)):
        ten = await stand.service.shift_saved(shift)
        stand.set_lead(20)
        twenty = await stand.service.shift_saved(shift)

    assert ten.scheduled_at == utc(2026, 8, 12, 21, 0)
    assert twenty.scheduled_at == utc(2026, 8, 12, 20, 50)


async def test_changing_the_lead_moves_the_moment_of_one_row() -> None:
    """Task 30: editing `opening_checklist_lead_minutes` moves the next push, in place."""
    stand = build(lead_minutes=10)
    shift = make_shift()
    with use_clock(FixedClock(NOW)):
        first = await stand.service.shift_saved(shift)
        stand.set_lead(45)
        second = await stand.service.shift_saved(shift)

    assert first.scheduled_at == utc(2026, 8, 13, 5, 50)
    assert second.scheduled_at == utc(2026, 8, 13, 5, 15)
    assert len(stand.notifications.rows) == 1
    assert second.notification is first.notification


async def test_a_moment_that_has_passed_becomes_now() -> None:
    """The acceptance run: a shift created five minutes before it starts, lead of ten."""
    stand = build()
    now = utc(2026, 8, 13, 5, 55)
    with use_clock(FixedClock(now)):
        result = await stand.service.shift_saved(make_shift())

    assert result.outcome is ScheduleOutcome.IMMEDIATE
    assert result.scheduled_at == now
    assert stand.notifications.rows[0].scheduled_at == now


async def test_a_shift_already_running_still_gets_its_checklist() -> None:
    """The extension of the rule above: the opener can open it any time until the shift ends."""
    stand = build()
    now = utc(2026, 8, 13, 12, 0)
    with use_clock(FixedClock(now)):
        result = await stand.service.shift_saved(make_shift())

    assert result.outcome is ScheduleOutcome.IMMEDIATE
    assert result.scheduled_at == now


async def test_a_shift_that_is_over_is_not_queued_at_all() -> None:
    stand = build()
    with use_clock(FixedClock(utc(2026, 8, 14, 6, 0))):
        result = await stand.service.shift_saved(make_shift())

    assert result.outcome is ScheduleOutcome.IN_THE_PAST
    assert stand.notifications.rows == []


async def test_a_shift_of_another_venue_is_refused() -> None:
    """Acceptance 11.3: a mis-wired service would time the checklist in the wrong zone."""
    stand = build()
    foreign = make_shift()
    foreign.venue_id = VENUE_ID + 1
    with use_clock(FixedClock(NOW)), pytest.raises(ForeignShiftError):
        await stand.service.shift_saved(foreign)

    assert stand.notifications.rows == []


async def test_a_shift_corrected_into_the_past_takes_its_push_with_it() -> None:
    """The row was planned while the shift was still ahead; the correction must undo it."""
    stand = build()
    with use_clock(FixedClock(NOW)):
        await stand.service.shift_saved(make_shift())
        result = await stand.service.shift_saved(make_shift(shift_date=dt.date(2026, 8, 9)))

    assert result.outcome is ScheduleOutcome.IN_THE_PAST
    assert result.cancelled == 1
    assert stand.notifications.live == []


# --------------------------------------------------------------------------------------
# Replanning and cancelling (TZ 6, plan task 33)
# --------------------------------------------------------------------------------------


async def test_moving_a_shift_moves_the_same_row() -> None:
    stand = build()
    with use_clock(FixedClock(NOW)):
        await stand.service.shift_saved(make_shift())
        moved = await stand.service.shift_saved(make_shift(start_time=dt.time(11, 0)))

    assert len(stand.notifications.rows) == 1
    assert moved.scheduled_at == utc(2026, 8, 13, 7, 50)
    assert stand.notifications.rows[0].scheduled_at == utc(2026, 8, 13, 7, 50)


async def test_reassigning_a_shift_moves_the_row_to_the_new_opener() -> None:
    stand = build()
    with use_clock(FixedClock(NOW)):
        await stand.service.shift_saved(make_shift())
        await stand.service.shift_saved(make_shift(user_id=STAND_IN_ID))

    assert len(stand.notifications.rows) == 1
    row = stand.notifications.rows[0]
    assert (row.user_id, row.chat_id) == (STAND_IN_ID, STAND_IN_CHAT_ID)


async def test_losing_the_opener_flag_cancels_the_row() -> None:
    stand = build()
    with use_clock(FixedClock(NOW)):
        await stand.service.shift_saved(make_shift())
        result = await stand.service.shift_saved(make_shift(is_opener=False))

    assert result.outcome is ScheduleOutcome.CANCELLED
    assert result.cancelled == 1
    assert stand.notifications.rows[0].status is NotificationStatus.CANCELLED
    assert stand.notifications.live == []


async def test_a_cancelled_shift_notifies_nobody() -> None:
    stand = build()
    with use_clock(FixedClock(NOW)):
        await stand.service.shift_saved(make_shift())
        await stand.service.shift_saved(make_shift(status=ShiftStatus.CANCELLED))

    assert stand.notifications.live == []


async def test_a_shift_that_never_had_a_checklist_cancels_nothing() -> None:
    stand = build()
    with use_clock(FixedClock(NOW)):
        result = await stand.service.shift_saved(make_shift(is_opener=False))

    assert result.cancelled == 0
    assert stand.notifications.rows == []


async def test_deleting_a_shift_cancels_its_live_rows_only() -> None:
    stand = build()
    with use_clock(FixedClock(NOW)):
        await stand.service.shift_saved(make_shift())
        await stand.service.checklist_template_empty(
            EmptyTemplateAlert(
                checklist_type=ChecklistType.OPENING,
                template_id=3,
                shift_id=SHIFT_ID,
                user_id=OPENER_ID,
            )
        )
        # A row already delivered is history; deleting the shift must not rewrite it.
        delivered = stand.notifications.of_type(NotificationType.CHECKLIST_TEMPLATE_EMPTY)[0]
        delivered.status = NotificationStatus.SENT
        cancelled = await stand.service.shift_removed(SHIFT_ID)

    assert cancelled == 1
    statuses = {row.type: row.status for row in stand.notifications.rows}
    assert statuses[str(NotificationType.OPENING_CHECKLIST)] is NotificationStatus.CANCELLED
    assert statuses[str(NotificationType.CHECKLIST_TEMPLATE_EMPTY)] is NotificationStatus.SENT


async def test_an_opener_put_back_gets_a_fresh_row() -> None:
    """The scenario the partial index exists for (schema notes): cancel, then plan again."""
    stand = build()
    with use_clock(FixedClock(NOW)):
        await stand.service.shift_saved(make_shift())
        await stand.service.shift_saved(make_shift(is_opener=False))
        again = await stand.service.shift_saved(make_shift())

    assert again.outcome is ScheduleOutcome.SCHEDULED
    assert len(stand.notifications.rows) == 2
    assert len(stand.notifications.live) == 1


# --------------------------------------------------------------------------------------
# Daylight saving, both directions, on Europe/Berlin (acceptance 11.2)
# --------------------------------------------------------------------------------------


async def test_spring_the_lead_is_subtracted_from_the_instant_not_from_the_clock() -> None:
    """A shift starting at 03:00 on the night the clock jumps 02:00 -> 03:00.

    Ten minutes before it is 01:50 local (still CET) — 00:50 UTC. Subtracting the lead on
    the wall clock instead would have produced 02:50, a reading that never existed.
    """
    stand = build(timezone=BERLIN)
    shift = make_shift(
        shift_date=dt.date(2026, 3, 29),
        start_time=dt.time(3, 0),
        end_time=dt.time(11, 0),
    )
    with use_clock(FixedClock(utc(2026, 3, 27, 6, 0))):
        result = await stand.service.shift_saved(shift)

    assert result.scheduled_at == utc(2026, 3, 29, 0, 50)


async def test_spring_a_start_that_never_happened_is_moved_forward() -> None:
    """02:30 does not exist on that date: the shift starts at 03:30 (D12), push at 03:20."""
    stand = build(timezone=BERLIN)
    shift = make_shift(
        shift_date=dt.date(2026, 3, 29),
        start_time=dt.time(2, 30),
        end_time=dt.time(11, 0),
    )
    with use_clock(FixedClock(utc(2026, 3, 27, 6, 0))):
        result = await stand.service.shift_saved(shift)

    assert result.scheduled_at == utc(2026, 3, 29, 1, 20)


async def test_autumn_an_ambiguous_start_is_taken_at_its_first_occurrence() -> None:
    """02:30 happens twice on 25 October; `fold=0` is summer time, 00:30 UTC."""
    stand = build(timezone=BERLIN)
    shift = make_shift(
        shift_date=dt.date(2026, 10, 25),
        start_time=dt.time(2, 30),
        end_time=dt.time(10, 0),
    )
    with use_clock(FixedClock(utc(2026, 10, 23, 6, 0))):
        result = await stand.service.shift_saved(shift)

    assert result.scheduled_at == utc(2026, 10, 25, 0, 20)


async def test_the_same_shift_time_is_a_different_instant_after_the_transition() -> None:
    """Two shifts written identically, 09:00-21:00, on either side of the spring night.

    The push is 07:50 UTC on Saturday and 06:50 UTC on Monday. Nothing about the shift
    changed — the venue's offset did, which is why the moment is computed at scheduling
    time and stored in UTC rather than kept as a local reading.
    """
    stand = build(timezone=BERLIN)
    with use_clock(FixedClock(utc(2026, 3, 27, 6, 0))):
        before = await stand.service.shift_saved(make_shift(shift_date=dt.date(2026, 3, 28)))
        after = await stand.service.shift_saved(
            make_shift(shift_id=SHIFT_ID + 1, shift_date=dt.date(2026, 3, 30))
        )

    assert before.scheduled_at == utc(2026, 3, 28, 7, 50)
    assert after.scheduled_at == utc(2026, 3, 30, 6, 50)


# --------------------------------------------------------------------------------------
# Event notifications to the manager (TZ 5.4, 5.5, 6)
# --------------------------------------------------------------------------------------


async def test_an_immediate_notification_is_a_row_scheduled_for_now() -> None:
    """TZ 6: there is no second delivery path. "Immediately" is `scheduled_at = now()`."""
    stand = build()
    now = utc(2026, 8, 13, 6, 30)
    with use_clock(FixedClock(now)):
        await stand.service.checklist_finished_with_skips(make_pending_alert())

    row = stand.notifications.rows[0]
    assert row.scheduled_at == now
    assert row.status is NotificationStatus.SCHEDULED
    assert row.type == str(NotificationType.CHECKLIST_SKIPPED)


async def test_every_manager_gets_their_own_row() -> None:
    stand = build(recipients=(MANAGER_ID, SECOND_MANAGER_ID))
    with use_clock(FixedClock(NOW)):
        await stand.service.checklist_finished_with_skips(make_pending_alert())

    rows = stand.notifications.rows
    assert [row.user_id for row in rows] == [MANAGER_ID, SECOND_MANAGER_ID]
    assert [row.chat_id for row in rows] == [MANAGER_CHAT_ID, SECOND_MANAGER_CHAT_ID]
    # One key per recipient, otherwise the second manager would deduplicate the first away.
    assert len({row.dedup_key for row in rows}) == 2
    assert stand.recipients.calls == [VENUE_ID]


async def test_a_venue_without_recipients_queues_nothing() -> None:
    stand = build(recipients=())
    with use_clock(FixedClock(NOW)):
        await stand.service.checklist_overdue(make_pending_alert())

    assert stand.notifications.rows == []


async def test_the_skipped_notification_carries_the_items_critical_first() -> None:
    stand = build()
    with use_clock(FixedClock(NOW)):
        await stand.service.checklist_finished_with_skips(make_pending_alert())

    payload = stand.notifications.rows[0].payload
    items = payload_items(payload)
    assert [item["is_critical"] for item in items] == [True, False]
    assert [item["item_id"] for item in items] == [3, 1]
    assert payload["skip_comment"] == "no delivery"
    assert payload["run_id"] == RUN_ID
    assert stand.notifications.rows[0].entity == CHECKLIST_RUN_ENTITY


async def test_the_overdue_notification_is_bound_to_the_run() -> None:
    stand = build()
    with use_clock(FixedClock(NOW)):
        await stand.service.checklist_overdue(make_pending_alert())

    row = stand.notifications.rows[0]
    assert row.type == str(NotificationType.CHECKLIST_OVERDUE)
    assert (row.entity, row.entity_id) == (CHECKLIST_RUN_ENTITY, RUN_ID)


async def test_the_empty_template_notification_is_bound_to_the_shift() -> None:
    """TZ 8.1: nothing goes to the employee, and deleting the shift takes this row with it."""
    stand = build()
    alert = EmptyTemplateAlert(
        checklist_type=ChecklistType.OPENING,
        template_id=3,
        shift_id=SHIFT_ID,
        user_id=OPENER_ID,
    )
    with use_clock(FixedClock(NOW)):
        await stand.service.checklist_template_empty(alert)

    row = stand.notifications.rows[0]
    assert (row.entity, row.entity_id) == (SHIFT_ENTITY, SHIFT_ID)
    assert row.user_id == MANAGER_ID
    assert row.payload["template_id"] == 3


async def test_an_empty_template_without_a_shift_is_still_reported() -> None:
    """A `stock` checklist has no shift (schema notes), so the row has no subject id."""
    stand = build()
    alert = EmptyTemplateAlert(
        checklist_type=ChecklistType.STOCK,
        template_id=None,
        shift_id=None,
        user_id=OPENER_ID,
    )
    with use_clock(FixedClock(NOW)):
        await stand.service.checklist_template_empty(alert)

    row = stand.notifications.rows[0]
    assert (row.entity, row.entity_id) == (None, None)
    assert row.payload["checklist_type"] == ChecklistType.STOCK.value


async def test_two_taps_on_report_missing_recipe_produce_one_live_row() -> None:
    stand = build()
    alert = MissingRecipeAlert(query="negroni", reported_by=OPENER_ID)
    with use_clock(FixedClock(NOW)):
        await stand.service.recipe_missing(alert)
        await stand.service.recipe_missing(alert)

    assert len(stand.notifications.rows) == 1
    assert stand.notifications.rows[0].payload["query"] == "negroni"


async def test_a_different_query_is_a_different_report() -> None:
    stand = build()
    with use_clock(FixedClock(NOW)):
        await stand.service.recipe_missing(MissingRecipeAlert(query="negroni", reported_by=1))
        await stand.service.recipe_missing(MissingRecipeAlert(query="americano", reported_by=1))

    assert len(stand.notifications.rows) == 2


# --------------------------------------------------------------------------------------
# One installation, many venues (acceptance 11.3)
# --------------------------------------------------------------------------------------


async def test_the_same_event_in_two_venues_is_two_rows() -> None:
    """`uq_notifications_dedup_key` is global, so a key without the venue is shared.

    Both stands write into one `store`, exactly as two venues write into one table. The
    event is the same in both — the same missing recipe, reported to a person who manages
    both bars — so every part of the key except the venue matches. Without the venue in it,
    the second venue's row would re-arm the first venue's instead of being queued, and its
    manager would never be told.
    """
    store: list[Notification] = []
    first = build(venue_id=VENUE_ID, store=store)
    second = build(venue_id=VENUE_ID + 1, store=store)
    alert = MissingRecipeAlert(query="negroni", reported_by=OPENER_ID)

    with use_clock(FixedClock(NOW)):
        await first.service.recipe_missing(alert)
        await second.service.recipe_missing(alert)

    assert len(store) == 2, "the venues share a table, not a notification"
    assert [row.venue_id for row in store] == [VENUE_ID, VENUE_ID + 1]
    assert len({row.dedup_key for row in store}) == 2
    assert all(row.status is NotificationStatus.SCHEDULED for row in store)


async def queue_for(
    stand: Stand,
    *,
    key: str,
    scheduled_at: dt.datetime = NOW,
    entity: str | None = SHIFT_ENTITY,
    entity_id: int | None = SHIFT_ID,
) -> Notification:
    """One row written through a venue's own repository, as its own service would."""
    return await stand.notifications.upsert_scheduled(
        dedup_key=key,
        notification_type=str(NotificationType.OPENING_CHECKLIST),
        scheduled_at=scheduled_at,
        user_id=OPENER_ID,
        chat_id=OPENER_CHAT_ID,
        payload={"shift_id": SHIFT_ID},
        entity=entity,
        entity_id=entity_id,
    )


async def test_the_timezone_and_the_lead_time_come_from_this_venue_and_not_a_neighbour() -> None:
    """Two venues, one directory and one settings table — each service reads only its own.

    `VenueRepo.get()` takes the id as an argument and `VenueSettingsRepo.get()` carries it in
    the `WHERE`; with a single row behind either fake both look correct even if the service
    grabbed whatever venue was lying around. The neighbour is deliberately different in both
    respects — another timezone and another lead time — so a mix-up moves the scheduled
    moment instead of hiding.

    Berlin is two hours behind Moscow in August and the neighbour's lead is half an hour, so
    reading the wrong row would land the push at 06:30 UTC (Berlin, 30 min) or 05:30 UTC
    (Moscow, 30 min) rather than 05:50.
    """
    venue_table: dict[int, Venue] = {}
    settings_table: dict[int, VenueSettings] = {}
    # The neighbour is seeded first on purpose: it is the row a lookup that ignored the
    # scope would reach for, so a mix-up moves the moment instead of landing on the right
    # answer by luck. Verified by mutation — dropping the scope from either fake fails here.
    build(
        venue_id=OTHER_VENUE_ID,
        timezone=BERLIN,
        lead_minutes=30,
        venue_table=venue_table,
        settings_table=settings_table,
    )
    here = build(venue_table=venue_table, settings_table=settings_table)

    assert set(venue_table) == {VENUE_ID, OTHER_VENUE_ID}
    assert set(settings_table) == {VENUE_ID, OTHER_VENUE_ID}

    with use_clock(FixedClock(NOW)):
        result = await here.service.shift_saved(make_shift())

    # 09:00 Moscow minus this venue's own ten minutes, in UTC.
    assert result.outcome is ScheduleOutcome.SCHEDULED
    assert result.scheduled_at == utc(2026, 8, 13, 5, 50)
    assert here.notifications.rows[0].venue_id == VENUE_ID


async def test_a_notification_of_another_venue_is_addressed_by_nothing_here() -> None:
    """Every read and every write of `NotificationRepo` carries `venue_id = :venue_id`.

    The neighbour's row is a row that genuinely exists, in the table this venue also writes
    into, about a `shift_id` this venue also uses — and it is outside every query this
    repository can build (TZ 3.3, TZ 9, acceptance 11.3). Without this test the predicate in
    the fake is code nothing depends on: it can be deleted and every other test here stays
    green, which is precisely how a leak between venues survives a green suite.
    """
    store: list[Notification] = []
    here = build(store=store)
    next_door = build(venue_id=OTHER_VENUE_ID, store=store)
    foreign = await queue_for(
        next_door,
        key=dedup_key(OTHER_VENUE_ID, NotificationType.OPENING_CHECKLIST, SHIFT_ENTITY, SHIFT_ID),
    )

    assert await here.notifications.get(foreign.id) is None
    assert (
        list(await here.notifications.list_for_entity(entity=SHIFT_ENTITY, entity_id=SHIFT_ID))
        == []
    )
    assert await here.notifications.cancel_for_entity(entity=SHIFT_ENTITY, entity_id=SHIFT_ID) == 0
    # The service route to the same statement: deleting *this* venue's shift 42.
    with use_clock(FixedClock(NOW)):
        assert await here.service.shift_removed(SHIFT_ID) == 0
    assert foreign.status is NotificationStatus.SCHEDULED

    # Hidden by the predicate, not by an absence: its own venue reads and cancels it.
    assert await next_door.notifications.get(foreign.id) is foreign
    assert [
        row.id
        for row in await next_door.notifications.list_for_entity(
            entity=SHIFT_ENTITY, entity_id=SHIFT_ID
        )
    ] == [foreign.id]
    assert (
        await next_door.notifications.cancel_for_entity(entity=SHIFT_ENTITY, entity_id=SHIFT_ID)
        == 1
    )
    assert foreign.status is NotificationStatus.CANCELLED


async def test_a_live_key_held_by_another_venue_is_refused_rather_than_re_armed() -> None:
    """The venue lives inside `ON CONFLICT DO UPDATE`, so the update matches nothing.

    `uq_notifications_dedup_key` is one index over the whole table (schema notes), so a key
    can be held by a row of another venue — not through `dedup_key()`, which leads with the
    venue, but any key that ever loses that prefix lands here. The insert loses to the index,
    the update is filtered out by `WHERE venue_id = :venue_id`, `RETURNING` comes back empty
    and `NotificationRepo` raises. Re-arming instead would move the neighbour's message to
    this venue's moment and its recipient — the leak, written by hand.
    """
    store: list[Notification] = []
    here = build(store=store)
    next_door = build(venue_id=OTHER_VENUE_ID, store=store)
    shared = "a-key-that-forgot-the-venue"
    foreign = await queue_for(next_door, key=shared, scheduled_at=utc(2026, 8, 13, 5, 50))

    with pytest.raises(NoResultFound):
        await queue_for(here, key=shared, scheduled_at=utc(2026, 8, 13, 9, 30))

    assert len(store) == 1, "the refused call wrote nothing"
    assert (foreign.venue_id, foreign.scheduled_at) == (OTHER_VENUE_ID, utc(2026, 8, 13, 5, 50))

    # The index is partial: once the neighbour's row is history, the key is free again.
    foreign.status = NotificationStatus.SENT
    mine = await queue_for(here, key=shared, scheduled_at=utc(2026, 8, 13, 9, 30))

    assert mine.venue_id == VENUE_ID
    assert len(store) == 2


async def test_the_venue_leads_the_key_of_every_type_the_stage_declares() -> None:
    """The prefix is added in `_enqueue`, so no type can be written without it.

    Asserted over the whole registry rather than type by type: a type declared by a later
    stage goes through the same seam and is covered by this test the day it is added.
    """
    stand = build()
    alert = make_pending_alert()
    with use_clock(FixedClock(NOW)):
        await stand.service.shift_saved(make_shift())
        await stand.service.checklist_finished_with_skips(alert)
        await stand.service.checklist_overdue(alert)
        await stand.service.checklist_template_empty(
            EmptyTemplateAlert(
                checklist_type=ChecklistType.OPENING,
                template_id=3,
                shift_id=SHIFT_ID,
                user_id=OPENER_ID,
            )
        )
        await stand.service.recipe_missing(MissingRecipeAlert(query="negroni", reported_by=1))

    queued = {row.type for row in stand.notifications.rows}
    assert queued == set(stand.service.registry.types)
    for row in stand.notifications.rows:
        assert row.dedup_key.startswith(f"{VENUE_ID}:{row.type}:"), row.dedup_key


async def test_a_recipient_who_no_longer_exists_leaves_the_chat_unresolved() -> None:
    """The row is still written: the queue is the record of what was meant to happen."""
    stand = build(recipients=(4242,))
    with use_clock(FixedClock(NOW)):
        await stand.service.checklist_overdue(make_pending_alert())

    row = stand.notifications.rows[0]
    assert row.user_id == 4242
    assert row.chat_id is None


# --------------------------------------------------------------------------------------
# The registry: the worker never branches on the type
# --------------------------------------------------------------------------------------


def test_stage_zero_declares_the_types_of_the_stage() -> None:
    registry = NotificationRegistry.stage_zero()
    assert registry.types == {str(member) for member in NotificationType}
    assert len(registry) == len(NotificationType)
    assert str(NotificationType.OPENING_CHECKLIST) in registry


def test_a_type_may_not_be_declared_twice() -> None:
    registry = NotificationRegistry.stage_zero()
    with pytest.raises(DuplicateNotificationTypeError):
        registry.register(NotificationSpec(type=NotificationType.RECIPE_MISSING))


def test_an_unknown_type_is_refused_at_the_registry() -> None:
    registry = NotificationRegistry.stage_zero()
    with pytest.raises(UnknownNotificationTypeError):
        registry.spec("shift_reminder")
    with pytest.raises(UnknownNotificationTypeError):
        registry.renderer_for("shift_reminder")


def test_a_known_type_without_a_renderer_says_so() -> None:
    """A distinct error: the wiring is incomplete, the row is not from the future."""
    registry = NotificationRegistry.stage_zero()
    with pytest.raises(RendererNotRegisteredError):
        registry.renderer_for(NotificationType.CHECKLIST_OVERDUE)


async def test_a_type_nobody_declared_cannot_be_queued() -> None:
    """The service validates against the registry, so a row can never outlive its renderer."""
    stand = build(registry=NotificationRegistry())
    with use_clock(FixedClock(NOW)), pytest.raises(UnknownNotificationTypeError):
        await stand.service.shift_saved(make_shift())

    assert stand.notifications.rows == []


async def test_adding_a_type_does_not_touch_the_worker() -> None:
    """The point of the registry (plan, task 19).

    ``deliver`` below is the whole of the worker's knowledge about notification types: it
    asks the registry and sends what comes back. A type invented after it was written is
    delivered by exactly the same three lines.
    """
    sent: list[OutgoingMessage] = []

    async def deliver(registry: NotificationRegistry, row: Notification) -> None:
        message = await registry.render(row)
        if message is not None:
            sent.append(message)

    async def render_reminder(row: Notification) -> OutgoingMessage | None:
        return OutgoingMessage(chat_id=row.chat_id or 0, text=str(row.payload["shift_id"]))

    async def render_nothing(row: Notification) -> OutgoingMessage | None:
        return None

    registry = NotificationRegistry.stage_zero()
    registry.attach_renderer(NotificationType.OPENING_CHECKLIST, render_nothing)
    # A type of stage 1, declared without touching this module or the worker.
    reminder = registry.register(NotificationSpec(type="shift_reminder", entity=SHIFT_ENTITY))
    registry.attach_renderer(reminder.type, render_reminder)

    stand = build(registry=registry)
    with use_clock(FixedClock(NOW)):
        await stand.service.shift_saved(make_shift())

    row = stand.notifications.rows[0]
    await deliver(registry, row)
    assert sent == []

    row.type = "shift_reminder"
    await deliver(registry, row)
    assert sent == [OutgoingMessage(chat_id=OPENER_CHAT_ID, text=str(SHIFT_ID))]


# --------------------------------------------------------------------------------------
# Contracts with the rest of the service layer
# --------------------------------------------------------------------------------------


def test_the_service_is_the_notifier_the_other_services_expect() -> None:
    """Checked by mypy as much as by pytest: three protocols, one implementation.

    Each of the three services that has something to tell somebody declares the narrow
    protocol it needs — and none of them may reach for the queue, a chat id or the bot.
    This is the assertion that the seams actually meet.
    """
    stand = build()
    checklists: ChecklistNotifier = stand.service
    recipes: RecipeNotifier = stand.service
    shifts: ShiftNotifier = stand.service
    assert checklists is stand.service
    assert recipes is stand.service
    assert shifts is stand.service


def test_the_pure_moment_never_looks_backwards() -> None:
    start = utc(2026, 8, 13, 6, 0)
    assert opening_checklist_moment(
        shift_start=start, lead_minutes=10, now=utc(2026, 8, 13, 5, 0)
    ) == utc(2026, 8, 13, 5, 50)
    assert opening_checklist_moment(
        shift_start=start, lead_minutes=10, now=utc(2026, 8, 13, 5, 55)
    ) == utc(2026, 8, 13, 5, 55)
    assert opening_checklist_moment(shift_start=start, lead_minutes=0, now=start) == start


def test_payload_items_survives_a_row_without_items() -> None:
    assert payload_items({}) == ()
    assert payload_items({"items": "not a list"}) == ()


# --------------------------------------------------------------------------------------
# TZ 6: the service layer sends nothing itself
# --------------------------------------------------------------------------------------

SENDING_CALLS = frozenset({"send_message", "send_photo", "edit_message_text", "answer"})


def test_the_service_talks_to_no_telegram_api() -> None:
    """Every notification goes through the table (TZ 6), so this module has no bot at all."""
    tree = ast.parse(read(SERVICE_MODULE), filename=relative(SERVICE_MODULE))
    imports = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "aiogram" not in imports

    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert not SENDING_CALLS.intersection(calls)
