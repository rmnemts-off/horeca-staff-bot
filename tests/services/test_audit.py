"""The audit trail (plan block F; TZ 2, 4.7, 5.1).

What is under test here is a set of promises the services make silently by holding an
:class:`~src.services.audit.AuditTrail` instead of writing records by hand: that an update
which moved nothing writes nothing, that a diff carries the fields the caller named and no
others, that the actor is recorded by `users.id` and never by `telegram_id`, and that a
value JSON cannot carry never reaches the JSONB column raw.

The sink is a fake, like everywhere in this package, and it is a copy of the contract of
`AuditLogRepo` rather than a list with an `append`:

* **it stamps the venue itself.** The real repository takes the venue from its own scope
  (`venue_id=self.venue_id`) and the trail never passes one — that is the whole reason a
  service cannot write into a neighbouring bar's history even holding a forged id. The fake
  keeps the rows of every venue in one shared list and filters on read, so the scope is a
  `WHERE` and not a separate box (TZ 3.3, acceptance 11.3);
* **it reads back scoped too** (`list_for_entity`, `entries`), which is what makes
  `test_another_venue_is_not_visible` able to fail at all.

`user_id` and `telegram_id` of the actor below are deliberately different numbers. With the
usual test habit of `user_id=1, telegram_id=1` the single most valuable assertion in this
file — that the trail records the id the `audit_log.user_id` foreign key points at — would
pass against an implementation that records the wrong one.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from src.db.models import MemberRole
from src.db.repositories.audit import AFTER_KEY, BEFORE_KEY
from src.services.access import AccessContext
from src.services.audit import SILENT, AuditAction, AuditEntity, AuditTrail, snapshot

VENUE_ID = 1
OTHER_VENUE_ID = 2

#: Far apart on purpose: see the module docstring.
USER_ID = 7
TELEGRAM_ID = 987654321


# --------------------------------------------------------------------------------------
# Fake sink
# --------------------------------------------------------------------------------------


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
    """`AuditLogRepo`, venue-scoped the way the real one is.

    The store is shared between venues because the table is: a record of another bar is a
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

    def list_for_entity(self, *, entity: str, entity_id: int, limit: int = 50) -> list[Entry]:
        """`for_venue()` plus the entity filter, newest first."""
        rows = [
            row
            for row in self.store
            if row.venue_id == self.venue_id and row.entity == entity and row.entity_id == entity_id
        ]
        rows.sort(key=lambda row: row.id, reverse=True)
        return rows[:limit]

    @property
    def entries(self) -> list[Entry]:
        """Everything this venue wrote, oldest first."""
        return [row for row in self.store if row.venue_id == self.venue_id]


@dataclass
class Row:
    """A stand-in for an ORM row: `snapshot` only ever does `getattr` on one."""

    full_name: str = "Ivan"
    position: str | None = "bar"
    is_active: bool = True


class Grade(enum.Enum):
    """A non-string enum, to exercise the branch `MemberRole` (a `StrEnum`) cannot."""

    HIGH = 3


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def store() -> list[Entry]:
    return []


@pytest.fixture
def sink(store: list[Entry]) -> FakeAuditLog:
    return FakeAuditLog(store, VENUE_ID)


@pytest.fixture
def trail(sink: FakeAuditLog) -> AuditTrail:
    return AuditTrail(sink)


@pytest.fixture
def actor() -> AccessContext:
    return AccessContext(
        user_id=USER_ID,
        telegram_id=TELEGRAM_ID,
        venue_id=VENUE_ID,
        member_id=42,
        role=MemberRole.MANAGER,
        full_name="Mira",
        position="bar",
    )


def only(sink: FakeAuditLog) -> Entry:
    """The single record this venue wrote, asserting there is exactly one."""
    assert len(sink.entries) == 1
    return sink.entries[0]


def diff_of(sink: FakeAuditLog) -> dict[str, Any]:
    diff = only(sink).diff
    assert diff is not None
    return diff


# --------------------------------------------------------------------------------------
# created / deleted
# --------------------------------------------------------------------------------------


async def test_created_records_the_values_the_row_was_created_with(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    await trail.created(
        actor,
        AuditEntity.MEMBER,
        11,
        after=snapshot(Row(full_name="Ivan", position="bar"), "full_name", "position"),
    )

    entry = only(sink)
    assert entry.action == AuditAction.CREATE == "create"
    # The entity is the table name (TZ 4), so a report can join on it without a lookup.
    assert entry.entity == "venue_members"
    assert entry.entity_id == 11
    assert entry.diff == {
        "full_name": {BEFORE_KEY: None, AFTER_KEY: "Ivan"},
        "position": {BEFORE_KEY: None, AFTER_KEY: "bar"},
    }


async def test_created_without_a_snapshot_records_the_event_alone(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    """Some creations have nothing worth carrying; the record still has to exist."""
    await trail.created(actor, AuditEntity.CHECKLIST_RUN, 3)

    entry = only(sink)
    assert entry.action == "create"
    assert entry.diff is None


async def test_deleted_records_what_the_row_was(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    """The vanished values are the only thing left to record after the row is gone.

    They land under `AFTER_KEY`: `deleted` builds the diff as `changed_fields({}, before)`,
    the same one-sided shape `created` produces. Asserted as the code behaves — the choice
    is noted in the report, not silently rewritten here.
    """
    await trail.deleted(actor, AuditEntity.CHECKLIST_ITEM, 8, before={"title": "Fridge"})

    entry = only(sink)
    assert entry.action == AuditAction.DELETE == "delete"
    assert entry.diff == {"title": {BEFORE_KEY: None, AFTER_KEY: "Fridge"}}


async def test_deleted_without_a_snapshot_records_the_event_alone(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    await trail.deleted(actor, AuditEntity.INVITE_CODE, 4)

    assert only(sink).diff is None


# --------------------------------------------------------------------------------------
# updated
# --------------------------------------------------------------------------------------


async def test_updated_writes_nothing_when_nothing_moved(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    """The property the whole module exists for: no change, no row."""
    fields = {"full_name": "Ivan", "position": "bar", "is_active": True}

    recorded = await trail.updated(
        actor, AuditEntity.MEMBER, 11, before=dict(fields), after=dict(fields)
    )

    assert recorded is False
    assert sink.entries == []


async def test_updated_records_only_the_fields_that_moved(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    """Not the whole snapshot: a diff nobody can read is a log nobody reads."""
    recorded = await trail.updated(
        actor,
        AuditEntity.MEMBER,
        11,
        before={"full_name": "Ivan", "position": "bar", "is_active": True},
        after={"full_name": "Ivan", "position": "hall", "is_active": True},
    )

    assert recorded is True
    assert diff_of(sink) == {"position": {BEFORE_KEY: "bar", AFTER_KEY: "hall"}}


async def test_updated_ignores_fields_the_caller_did_not_touch(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    """`after` is the set of columns the caller meant to write; `before` may be wider."""
    await trail.updated(
        actor,
        AuditEntity.RECIPE,
        2,
        before={"title": "Negroni", "yield_ml": 90, "notes": "stir"},
        after={"yield_ml": 120},
    )

    assert diff_of(sink) == {"yield_ml": {BEFORE_KEY: 90, AFTER_KEY: 120}}


async def test_updated_defaults_to_the_update_action(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    await trail.updated(
        actor, AuditEntity.SHIFT, 5, before={"comment": None}, after={"comment": "late"}
    )

    assert only(sink).action == AuditAction.UPDATE == "update"


async def test_updated_carries_an_explicit_action_through(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    """A caller that already knows the event names it; the diff is computed the same way."""
    await trail.updated(
        actor,
        AuditEntity.CHECKLIST_TEMPLATE,
        6,
        before={"is_active": True},
        after={"is_active": False},
        action=AuditAction.DEACTIVATE,
    )

    assert only(sink).action == "deactivate"


# --------------------------------------------------------------------------------------
# set_active (TZ 5.1)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_active", "action"),
    [(True, "activate"), (False, "deactivate")],
)
async def test_set_active_names_the_event_instead_of_calling_it_an_update(
    trail: AuditTrail,
    sink: FakeAuditLog,
    actor: AccessContext,
    is_active: bool,
    action: str,
) -> None:
    """TZ 5.1: an employee is switched off, never deleted, and the log is asked exactly
    "who switched Ivanov off" — a row saying `update` does not answer that."""
    await trail.set_active(actor, AuditEntity.MEMBER, 11, is_active=is_active)

    entry = only(sink)
    assert entry.action == action
    assert entry.action != AuditAction.UPDATE
    assert entry.diff == {"is_active": {BEFORE_KEY: not is_active, AFTER_KEY: is_active}}


# --------------------------------------------------------------------------------------
# The actor
# --------------------------------------------------------------------------------------


async def test_user_id_is_the_database_id_not_the_telegram_id(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    """`audit_log.user_id` is a foreign key to `users.id`.

    Recording `telegram_id` there points the key at a row that either does not exist or —
    worse — belongs to somebody else, and the stage 1 report joins to nothing.
    """
    await trail.created(actor, AuditEntity.SHIFT, 5, after={"status": "planned"})

    entry = only(sink)
    assert entry.user_id == USER_ID
    assert entry.user_id != actor.telegram_id


async def test_every_method_records_the_same_actor(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    await trail.created(actor, AuditEntity.MEMBER, 11, after={"full_name": "Ivan"})
    await trail.updated(
        actor, AuditEntity.MEMBER, 11, before={"position": None}, after={"position": "bar"}
    )
    await trail.set_active(actor, AuditEntity.MEMBER, 11, is_active=False)
    await trail.deleted(actor, AuditEntity.INVITE_CODE, 4, before={"code": "1-ABCDEFGH"})

    assert [entry.user_id for entry in sink.entries] == [USER_ID] * 4


async def test_an_actorless_change_is_recorded_without_a_user(
    trail: AuditTrail, sink: FakeAuditLog
) -> None:
    """The scheduler acts on nobody's behalf, and the column is nullable for that reason."""
    await trail.created(None, AuditEntity.CHECKLIST_RUN, 3, after={"status": "open"})

    assert only(sink).user_id is None


# --------------------------------------------------------------------------------------
# SILENT
# --------------------------------------------------------------------------------------


async def test_silent_records_nothing_and_raises_nothing(actor: AccessContext) -> None:
    """The venue wizard runs before the venue exists; there is nowhere to record yet."""
    assert SILENT.is_silent is True

    await SILENT.created(actor, AuditEntity.VENUE, None, after={"title": "Invasion"})
    await SILENT.deleted(actor, AuditEntity.VENUE, 1, before={"title": "Invasion"})
    await SILENT.set_active(actor, AuditEntity.MEMBER, 11, is_active=False)
    recorded = await SILENT.updated(
        actor, AuditEntity.MEMBER, 11, before={"position": "bar"}, after={"position": "hall"}
    )

    # The answer stays honest about the data even when nobody is listening.
    assert recorded is True
    assert (
        await SILENT.updated(
            actor, AuditEntity.MEMBER, 11, before={"position": "bar"}, after={"position": "bar"}
        )
        is False
    )


async def test_a_trail_with_a_sink_is_not_silent(trail: AuditTrail) -> None:
    assert trail.is_silent is False


# --------------------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------------------


def test_snapshot_takes_only_the_named_fields() -> None:
    """TZ 9: an unrelated edit must not drag a person's name into its diff."""
    assert snapshot(Row(), "position") == {"position": "bar"}


def test_snapshot_reads_a_missing_attribute_as_none() -> None:
    """The "before" of a row that does not exist yet is exactly `None`, not an error."""
    assert snapshot(object(), "full_name", "position") == {"full_name": None, "position": None}


async def test_a_snapshot_of_a_missing_row_makes_a_create_diff(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    """The pattern a service uses: snapshot before, snapshot after, one diff out of both."""
    row = Row(full_name="Ivan", position=None)
    before = snapshot(object(), "full_name", "position")
    after = snapshot(row, "full_name", "position")

    await trail.updated(actor, AuditEntity.MEMBER, 11, before=before, after=after)

    assert diff_of(sink) == {"full_name": {BEFORE_KEY: None, AFTER_KEY: "Ivan"}}


# --------------------------------------------------------------------------------------
# Values JSON cannot carry
# --------------------------------------------------------------------------------------


async def test_values_json_cannot_carry_are_rendered_before_they_reach_the_column(
    trail: AuditTrail, sink: FakeAuditLog, actor: AccessContext
) -> None:
    """`diff` is JSONB: a `datetime`, a `Decimal` or an enum member put in raw fails at
    flush, hours after the change it was meant to describe."""
    moment = dt.datetime(2026, 8, 14, 18, 30, tzinfo=dt.UTC)

    await trail.updated(
        actor,
        AuditEntity.SHIFT,
        5,
        before={"starts_at": None, "rate": None, "role": None, "grade": None},
        after={
            "starts_at": moment,
            "rate": Decimal("1250.50"),
            "role": MemberRole.MANAGER,
            "grade": Grade.HIGH,
        },
    )

    diff = diff_of(sink)
    # TZ 3.4: the database is UTC and the offset stays in the text.
    assert diff["starts_at"][AFTER_KEY] == "2026-08-14T18:30:00+00:00"
    # Exact as text rather than a float that is nearly the money it stands for.
    assert diff["rate"][AFTER_KEY] == "1250.50"
    assert diff["role"][AFTER_KEY] == "manager"
    assert diff["grade"][AFTER_KEY] == 3
    # The end-to-end statement: whatever the shapes above are, psycopg can write them.
    assert json.loads(json.dumps(diff))["grade"] == {BEFORE_KEY: None, AFTER_KEY: 3}


# --------------------------------------------------------------------------------------
# Venue scope
# --------------------------------------------------------------------------------------


async def test_another_venue_is_not_visible(
    store: list[Entry], sink: FakeAuditLog, trail: AuditTrail, actor: AccessContext
) -> None:
    """The record goes into the shared table and only this venue can read it back.

    Written against the fake's own predicate (`row.venue_id == self.venue_id`), which is the
    `for_venue()` of `AuditLogRepo`: drop it and this is the test that fails.
    """
    neighbour = FakeAuditLog(store, OTHER_VENUE_ID)
    await AuditTrail(neighbour).created(actor, AuditEntity.MEMBER, 11, after={"full_name": "Nina"})

    await trail.created(actor, AuditEntity.MEMBER, 11, after={"full_name": "Ivan"})

    assert len(store) == 2
    assert [entry.diff for entry in sink.entries] == [
        {"full_name": {BEFORE_KEY: None, AFTER_KEY: "Ivan"}}
    ]
    assert [entry.diff for entry in neighbour.entries] == [
        {"full_name": {BEFORE_KEY: None, AFTER_KEY: "Nina"}}
    ]
    # Same entity, same id — the number a forged callback_data would carry.
    assert [
        entry.venue_id for entry in sink.list_for_entity(entity="venue_members", entity_id=11)
    ] == [VENUE_ID]


async def test_the_venue_comes_from_the_sink_and_never_from_the_actor(
    store: list[Entry], sink: FakeAuditLog, trail: AuditTrail
) -> None:
    """The trail passes no venue at all, which is what makes the scope unforgeable.

    An actor carrying another venue's id — the shape of a stale context or a forged
    callback — cannot move the record out of the venue the repository was built for.
    """
    stranger = AccessContext(
        user_id=USER_ID,
        telegram_id=TELEGRAM_ID,
        venue_id=OTHER_VENUE_ID,
        member_id=99,
        role=MemberRole.OWNER,
        full_name="Oleg",
        position=None,
    )

    await trail.set_active(stranger, AuditEntity.MEMBER, 11, is_active=False)

    assert only(sink).venue_id == VENUE_ID
    assert FakeAuditLog(store, OTHER_VENUE_ID).entries == []
