"""The notification queue and the audit log (plan, task 14).

The test the plan names for this task is the last one in the file: two workers, one queue,
no row carried twice. It needs two real connections — the point of `FOR UPDATE SKIP LOCKED`
is what one transaction does while another holds a lock — so it creates its own rows,
commits them, and removes them afterwards, instead of using the shared rolled-back
`session` fixture.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.db.models import Notification, NotificationStatus, Venue
from src.db.repositories.audit import AuditLogRepo, as_json, changed_fields
from src.db.repositories.notifications import NotificationRepo

# The recording session lives next door: one stand-in for `AsyncSession`, used by every
# shape test of this package.
from tests.db.test_repositories_checklists import Recorder, Rows, as_session
from tests.factories import DEFAULT_TIMEZONE, create_user, create_venue

MOMENT = dt.datetime(2026, 1, 1, 6, 0, tzinfo=dt.UTC)
LATER = MOMENT + dt.timedelta(minutes=30)
VENUE_ID = 7

#: Enough rows that two workers with a batch limit of four have to split them.
QUEUE_SIZE = 6
BATCH = 4


def _key() -> str:
    """A `dedup_key` unique to one test: the index is partial but still global."""
    return f"test:{uuid.uuid4().hex}"


async def _notification(
    session: AsyncSession,
    venue: Venue,
    *,
    dedup_key: str | None = None,
    scheduled_at: dt.datetime = MOMENT,
    status: NotificationStatus = NotificationStatus.SCHEDULED,
    entity: str | None = None,
    entity_id: int | None = None,
    claimed_at: dt.datetime | None = None,
) -> Notification:
    row = Notification(
        venue_id=venue.id,
        type="probe",
        payload={},
        dedup_key=dedup_key if dedup_key is not None else _key(),
        scheduled_at=scheduled_at,
        status=status,
        entity=entity,
        entity_id=entity_id,
        claimed_at=claimed_at,
    )
    session.add(row)
    await session.flush()
    return row


# --------------------------------------------------------------------------------------
# Shape of the statements — no database
# --------------------------------------------------------------------------------------


async def test_claiming_skips_what_another_worker_holds() -> None:
    recorder = Recorder(Rows(1))
    repo = NotificationRepo(as_session(recorder), VENUE_ID)

    await repo.claim_batch(now=MOMENT, limit=BATCH)

    statement = recorder.sql()
    assert "FOR UPDATE SKIP LOCKED" in statement
    assert statement.startswith("UPDATE notifications")
    assert statement.count("notifications.venue_id") >= 2, "outer update and inner select"


async def test_the_upsert_names_the_partial_index_with_literal_values() -> None:
    """PostgreSQL matches the arbiter against the index; a parameter never matches."""
    recorder = Recorder(Rows(1), Rows("row"))
    repo = NotificationRepo(as_session(recorder), VENUE_ID)

    await repo.upsert_scheduled(
        dedup_key="k",
        notification_type="t",
        scheduled_at=MOMENT,
        user_id=1,
        chat_id=2,
        payload={},
    )

    statement = recorder.sql()
    assert "ON CONFLICT (dedup_key) WHERE status IN ('scheduled', 'sending')" in statement
    assert "DO UPDATE SET" in statement
    assert "WHERE notifications.venue_id" in statement


async def test_a_delivery_is_recorded_only_while_the_row_is_still_claimed() -> None:
    """`sending` *and* the claim token; anything else is somebody else's row now (11.4)."""
    recorder = Recorder()
    repo = NotificationRepo(as_session(recorder), VENUE_ID)

    await repo.mark_sent(1, MOMENT, claimed_at=MOMENT)
    await repo.mark_failed(1, "blocked", claimed_at=MOMENT)

    for index in (0, 1):
        compiled = recorder.statements[index].compile(dialect=postgresql.dialect())
        condition = str(compiled).rsplit("WHERE", 1)[1]
        assert "notifications.status = " in condition, "one status, not a set of them"
        assert "notifications.claimed_at = " in condition, "the claim token is matched too"
        matched = {getattr(value, "value", value) for value in compiled.params.values()}
        assert NotificationStatus.SENDING.value in matched
        assert NotificationStatus.SCHEDULED.value not in matched


async def test_reclaiming_counts_the_abandoned_attempt() -> None:
    recorder = Recorder()
    repo = NotificationRepo(as_session(recorder), VENUE_ID)

    await repo.reclaim_stale(older_than=MOMENT)

    statement = recorder.sql()
    assert "attempts=(notifications.attempts +" in statement.replace(" = ", "=")
    assert "notifications.status = " in statement


# --------------------------------------------------------------------------------------
# The diff helper of the audit log — no database
# --------------------------------------------------------------------------------------


def test_the_diff_holds_only_what_moved_and_is_json_safe() -> None:
    before = {"name": "old", "hours": Decimal("15.0"), "at": MOMENT, "kept": 1}
    after = {"name": "new", "hours": Decimal("12.5"), "at": LATER, "kept": 1}

    diff = changed_fields(before, after)

    assert diff is not None
    assert set(diff) == {"name", "hours", "at"}, "an unchanged field is not a change"
    assert diff["hours"] == {"from": "15.0", "to": "12.5"}
    assert diff["at"]["to"] == LATER.isoformat()
    assert changed_fields(before, before) is None


def test_json_conversion_covers_what_a_column_can_hold() -> None:
    assert as_json(NotificationStatus.SENT) == "sent"
    assert as_json([Decimal("1.5"), None]) == ["1.5", None]
    assert as_json({"nested": MOMENT}) == {"nested": MOMENT.isoformat()}


# --------------------------------------------------------------------------------------
# Behaviour — against a real PostgreSQL
# --------------------------------------------------------------------------------------


@pytest.mark.db
async def test_the_same_key_re_arms_the_live_row(session: AsyncSession) -> None:
    """Decision D5: one live notification per key, and scheduling it again moves it."""
    venue = await create_venue(session)
    user = await create_user(session)
    repo = NotificationRepo(session, venue.id)
    key = _key()

    first = await repo.upsert_scheduled(
        dedup_key=key,
        notification_type="opening_checklist",
        scheduled_at=MOMENT,
        user_id=user.id,
        chat_id=user.telegram_id,
        payload={"shift_id": 1},
        entity="shift",
        entity_id=1,
    )
    second = await repo.upsert_scheduled(
        dedup_key=key,
        notification_type="opening_checklist",
        scheduled_at=LATER,
        user_id=user.id,
        chat_id=user.telegram_id,
        payload={"shift_id": 1},
        entity="shift",
        entity_id=1,
    )

    assert second.id == first.id, "the live row is re-armed, not duplicated"
    assert second.scheduled_at == LATER
    assert second.status is NotificationStatus.SCHEDULED

    listed = await repo.list_for_entity(entity="shift", entity_id=1)
    assert [row.id for row in listed] == [first.id]


@pytest.mark.db
async def test_a_delivered_key_can_be_scheduled_again(session: AsyncSession) -> None:
    """The unique index is partial: history holds no key (schema notes, tasks 19 and 33)."""
    venue = await create_venue(session)
    repo = NotificationRepo(session, venue.id)
    key = _key()

    first = await repo.upsert_scheduled(
        dedup_key=key,
        notification_type="probe",
        scheduled_at=MOMENT,
        user_id=None,
        chat_id=None,
        payload={},
    )
    # Claimed first: a delivery is recorded by the worker that took the row, and only
    # while it still holds it (see `mark_sent`).
    claimed = await repo.claim_batch(now=MOMENT, limit=BATCH)
    assert [row.id for row in claimed] == [first.id]
    await repo.mark_sent(first.id, MOMENT, claimed_at=claimed[0].claimed_at)
    second = await repo.upsert_scheduled(
        dedup_key=key,
        notification_type="probe",
        scheduled_at=LATER,
        user_id=None,
        chat_id=None,
        payload={},
    )

    assert second.id != first.id
    delivered = await repo.get(first.id)
    assert delivered is not None
    assert delivered.status is NotificationStatus.SENT
    assert delivered.sent_at == MOMENT


@pytest.mark.db
async def test_re_arming_a_row_being_delivered_does_not_lose_it(session: AsyncSession) -> None:
    """The race criterion 11.4 is about: replanning against a delivery in flight.

    The worker holds the row (`sending`) and is talking to Telegram when the manager moves
    the shift. `upsert_scheduled` re-arms the same row for the new moment — and the worker's
    `mark_sent`, which is about the message it was already sending, must not conclude it:
    the row it names now carries a *different* notification. The delivery in flight is
    history, and what the schedule now says still has to go out.
    """
    venue = await create_venue(session)
    repo = NotificationRepo(session, venue.id)
    key = _key()

    queued = await repo.upsert_scheduled(
        dedup_key=key,
        notification_type="opening_checklist",
        scheduled_at=MOMENT,
        user_id=None,
        chat_id=None,
        payload={"shift_id": 1},
        entity="shift",
        entity_id=1,
    )
    claimed = await repo.claim_batch(now=MOMENT, limit=BATCH)
    assert [row.id for row in claimed] == [queued.id]
    token = claimed[0].claimed_at

    re_armed = await repo.upsert_scheduled(
        dedup_key=key,
        notification_type="opening_checklist",
        scheduled_at=LATER,
        user_id=None,
        chat_id=None,
        payload={"shift_id": 1},
        entity="shift",
        entity_id=1,
    )
    assert re_armed.id == queued.id, "one live row per key, as decision D5 says"

    await repo.mark_sent(queued.id, MOMENT, claimed_at=token)
    await repo.mark_failed(queued.id, "blocked", claimed_at=token)

    row = await repo.get(queued.id)
    assert row is not None
    assert row.status is NotificationStatus.SCHEDULED, "the re-armed row is still owed"
    assert row.sent_at is None
    assert row.scheduled_at == LATER
    assert row.attempts == 0
    assert row.error is None
    assert [item.id for item in await repo.claim_batch(now=LATER, limit=BATCH)] == [queued.id]


@pytest.mark.db
async def test_a_taken_over_claim_can_no_longer_conclude_the_row(session: AsyncSession) -> None:
    """The other half of criterion 11.4: a stale worker must not close a live claim.

    The worker takes the row and stalls; `reclaim_stale` hands it on and a second worker
    takes it. When the first one finally comes back, the row is `sending` again — the
    status it was left in — so status alone would let it stamp `sent` over a message not
    yet delivered, or `failed` over one still in flight, and `failed` is terminal. The
    claim token is what tells the two apart.
    """
    venue = await create_venue(session)
    repo = NotificationRepo(session, venue.id)
    row = await _notification(session, venue)

    stalled = await repo.claim_batch(now=MOMENT, limit=BATCH)
    assert [item.id for item in stalled] == [row.id]
    stale_token = stalled[0].claimed_at
    assert stale_token == MOMENT, "the claim hands its own instant back to the caller"

    assert await repo.reclaim_stale(older_than=LATER) == 1
    taken_over = await repo.claim_batch(now=LATER, limit=BATCH)
    assert [item.id for item in taken_over] == [row.id]
    live_token = taken_over[0].claimed_at
    assert live_token == LATER

    await repo.mark_sent(row.id, MOMENT, claimed_at=stale_token)
    await repo.mark_failed(row.id, "blocked", claimed_at=stale_token)

    held = await repo.get(row.id)
    assert held is not None
    assert held.status is NotificationStatus.SENDING, "the live claim survives both reports"
    assert held.sent_at is None
    assert held.error is None
    assert held.attempts == 1, "the abandoned claim only; the stale worker counted nothing"

    await repo.mark_sent(row.id, LATER, claimed_at=live_token)
    concluded = await repo.get(row.id)
    assert concluded is not None
    assert concluded.status is NotificationStatus.SENT
    assert concluded.sent_at == LATER


@pytest.mark.db
async def test_after_a_reclaim_the_notification_goes_out_exactly_once(
    session: AsyncSession,
) -> None:
    """Criterion 11.4 over a revived row: neither lost nor delivered twice.

    The loop below is the worker's, reduced to what the queue can observe: claim, send,
    report. `sent` collects every row the queue actually handed to a sender, so the
    assertion at the end counts deliveries rather than statuses.
    """
    venue = await create_venue(session)
    repo = NotificationRepo(session, venue.id)
    row = await _notification(session, venue)
    sent: list[int] = []

    # Pass one: the row is taken and the worker goes quiet still holding this token.
    stalled = await repo.claim_batch(now=MOMENT, limit=BATCH)
    stale_token = stalled[0].claimed_at

    # The watchdog revives it; the next pass delivers it.
    assert await repo.reclaim_stale(older_than=LATER) == 1
    for claimed in await repo.claim_batch(now=LATER, limit=BATCH):
        sent.append(claimed.id)
        await repo.mark_sent(claimed.id, LATER, claimed_at=claimed.claimed_at)

    # Only now does the stalled worker come back and report what it had been doing.
    await repo.mark_sent(row.id, MOMENT, claimed_at=stale_token)
    await repo.mark_failed(row.id, "blocked", claimed_at=stale_token)

    # Every pass after that finds an empty queue — nothing to reclaim, nothing to claim.
    for moment in (LATER, LATER + dt.timedelta(hours=1)):
        assert await repo.reclaim_stale(older_than=moment) == 0
        for claimed in await repo.claim_batch(now=moment, limit=BATCH):
            sent.append(claimed.id)
            await repo.mark_sent(claimed.id, moment, claimed_at=claimed.claimed_at)

    assert sent == [row.id], "the queue handed the row to a sender exactly once"
    delivered = await repo.get(row.id)
    assert delivered is not None
    assert delivered.status is NotificationStatus.SENT
    assert delivered.sent_at == LATER, "the delivery on record is the one that happened"
    assert delivered.error is None, "the stale worker's failure did not bury the row"
    assert delivered.attempts == 1, "the abandoned claim, and nothing else"


@pytest.mark.db
async def test_a_live_key_belongs_to_the_installation_not_to_a_venue(
    session: AsyncSession,
) -> None:
    """Why every `dedup_key` starts with the venue (`src/services/notifications.py`).

    `uq_notifications_dedup_key` is one column over the whole table, so a key two venues
    can both produce is a key one of them silently loses. Here the second venue is refused
    outright — which is the *good* half of the story, and only because the upsert's
    `DO UPDATE` is fenced by `venue_id`. Were the fence missing, venue B would quietly
    re-arm venue A's row instead (acceptance 11.3).
    """
    venue = await create_venue(session)
    stranger = await create_venue(session)
    key = _key()

    await NotificationRepo(session, venue.id).upsert_scheduled(
        dedup_key=key,
        notification_type="recipe_missing",
        scheduled_at=MOMENT,
        user_id=None,
        chat_id=None,
        payload={},
    )

    with pytest.raises(NoResultFound):
        await NotificationRepo(session, stranger.id).upsert_scheduled(
            dedup_key=key,
            notification_type="recipe_missing",
            scheduled_at=MOMENT,
            user_id=None,
            chat_id=None,
            payload={},
        )


@pytest.mark.db
async def test_claim_release_and_cancel(session: AsyncSession) -> None:
    venue = await create_venue(session)
    stranger = await create_venue(session)
    row = await _notification(session, venue, entity="shift", entity_id=42)
    await _notification(session, venue, scheduled_at=LATER + dt.timedelta(days=1))
    await _notification(session, stranger)

    repo = NotificationRepo(session, venue.id)

    claimed = await repo.claim_batch(now=MOMENT, limit=BATCH)
    assert [item.id for item in claimed] == [row.id], "only what is due, only this venue"
    assert claimed[0].status is NotificationStatus.SENDING
    assert await repo.claim_batch(now=MOMENT, limit=BATCH) == [], "a claimed row is not due"

    assert await repo.reclaim_stale(older_than=LATER) == 1
    back = await repo.get(row.id)
    assert back is not None
    assert back.status is NotificationStatus.SCHEDULED
    assert back.attempts == 1
    assert back.claimed_at is None

    assert await repo.cancel_for_entity(entity="shift", entity_id=42) == 1
    cancelled = await repo.get(row.id)
    assert cancelled is not None
    assert cancelled.status is NotificationStatus.CANCELLED
    assert await repo.claim_batch(now=MOMENT, limit=BATCH) == []


@pytest.mark.db
async def test_another_venue_neither_claims_nor_cancels(session: AsyncSession) -> None:
    venue = await create_venue(session)
    stranger = await create_venue(session)
    row = await _notification(session, venue, entity="shift", entity_id=7)

    foreign = NotificationRepo(session, stranger.id)

    assert await foreign.get(row.id) is None
    assert await foreign.claim_batch(now=MOMENT, limit=BATCH) == []
    assert await foreign.cancel_for_entity(entity="shift", entity_id=7) == 0
    await foreign.mark_failed(row.id, "nope", claimed_at=None)

    own = NotificationRepo(session, venue.id)
    untouched = await own.get(row.id)
    assert untouched is not None
    assert untouched.status is NotificationStatus.SCHEDULED
    assert untouched.attempts == 0


@pytest.mark.db
async def test_the_audit_log_records_and_reads_back(session: AsyncSession) -> None:
    venue = await create_venue(session)
    user = await create_user(session)
    repo = AuditLogRepo(session, venue.id)

    diff = changed_fields({"name": "old"}, {"name": "new"})
    entry = await repo.record(
        user_id=user.id,
        entity="shift",
        entity_id=5,
        action="update",
        diff=diff,
    )

    listed = await repo.list_for_entity(entity="shift", entity_id=5, limit=10)
    assert [row.id for row in listed] == [entry.id]
    assert listed[0].diff == {"name": {"from": "old", "to": "new"}}

    other_venue = await create_venue(session)
    assert (
        await AuditLogRepo(session, other_venue.id).list_for_entity(
            entity="shift", entity_id=5, limit=10
        )
        == []
    )


@pytest.mark.db
async def test_two_workers_never_claim_the_same_row(engine: AsyncEngine) -> None:
    """The test task 14 is written for: one queue, two workers, no row carried twice.

    Two sessions on two connections, both claiming before either commits — which is the
    only way `SKIP LOCKED` can be observed at all. The rows are committed on purpose and
    removed at the end; the throwaway database is dropped after the run either way.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    venue_id: int | None = None
    try:
        async with factory() as setup:
            venue = Venue(
                name=f"Venue {uuid.uuid4().hex[:8]}",
                city="City",
                timezone=DEFAULT_TIMEZONE,
            )
            setup.add(venue)
            await setup.flush()
            venue_id = venue.id
            for _ in range(QUEUE_SIZE):
                await _notification(setup, venue)
            await setup.commit()

        async with factory() as first, factory() as second:
            one = NotificationRepo(first, venue_id)
            two = NotificationRepo(second, venue_id)
            claimed_one, claimed_two = await asyncio.gather(
                one.claim_batch(now=MOMENT, limit=BATCH),
                two.claim_batch(now=MOMENT, limit=BATCH),
            )
            ids_one = {row.id for row in claimed_one}
            ids_two = {row.id for row in claimed_two}
            await first.commit()
            await second.commit()

        assert not ids_one & ids_two, "the same row was carried by two workers"
        assert len(ids_one) + len(ids_two) == QUEUE_SIZE, "nothing was lost either"

        async with factory() as check:
            repo = NotificationRepo(check, venue_id)
            assert await repo.claim_batch(now=MOMENT, limit=QUEUE_SIZE) == []
    finally:
        if venue_id is not None:
            async with factory() as cleanup:
                await cleanup.execute(delete(Venue).where(Venue.id == venue_id))
                await cleanup.commit()
