"""Who changed what, recorded once per change (TZ 2, 4.7; plan block F).

TZ 2 asks that every data change be written to `audit_log` with the actor, the object and
the difference. The plan adds *how*: "in the service layer, not line by line in the
handlers". This module is that one place.

**Why a trail object rather than a call to the repository.** An audit record written by
hand at each call site is written differently at each call site: one forgets the diff,
another records the whole row (so the log stops being readable), a third records the actor's
`telegram_id` instead of `users.id` and the report joins to nothing. The repository already
refuses to invent the venue — it is scoped to one — but the rest is discipline, and
discipline spread over five services and thirty methods is not discipline.

So a service takes an :class:`AuditTrail`, and the trail:

* reads the actor's `user_id` off the :class:`~src.services.access.AccessContext`, because
  that is the id the `audit_log.user_id` foreign key points at;
* computes the diff with `changed_fields()` — the same helper the repository ships — and
  **writes nothing when nothing moved**. An "update" that set a field to the value it
  already had is not a change, and a log full of empty diffs is a log nobody reads;
* names the entity from :class:`AuditEntity`, a closed list, so the reports of stage 1 can
  filter on it instead of matching strings that drifted apart across services.

**A trail with no repository is silent, not broken** (:data:`SILENT`). Two callers need
that and neither is a shortcut: the venue wizard runs *before* the venue exists, so there
is no venue-scoped repository to record into yet (the creation itself is recorded once the
row is there, by a trail built around the new venue), and the service tests that do not
exercise the audit build their services without one. It is spelled as an object rather than
`None` checks scattered through the services.

**The snapshot is explicit.** :func:`snapshot` takes the fields to record; a service says
which columns it is about to touch. Dumping every column of a row would put a person's name
and a venue's settings into the diff of an unrelated edit, and TZ 9 is about not carrying
data further than it has to go.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Any, Final, Protocol

from src.db.repositories.audit import changed_fields
from src.services.access import AccessContext


class AuditEntity(enum.StrEnum):
    """What an `audit_log` row is about — the closed list of stage 0.

    The values are the table names of the schema (TZ 4), so a record can be read without a
    second lookup table and a report can join to the object it names.
    """

    VENUE = "venues"
    VENUE_SETTINGS = "venue_settings"
    MEMBER = "venue_members"
    INVITE_CODE = "invite_codes"
    SHIFT = "shifts"
    CHECKLIST_TEMPLATE = "checklist_templates"
    CHECKLIST_ITEM = "checklist_items"
    CHECKLIST_RUN = "checklist_runs"
    RECIPE = "recipes"


class AuditAction(enum.StrEnum):
    """What happened to it.

    `ACTIVATE`/`DEACTIVATE` are separate from `UPDATE` because TZ 5.1 makes them a distinct
    event: an employee is never deleted, they are switched off, and "who switched Ivanov
    off and when" is the question the log is asked.
    """

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    ACTIVATE = "activate"
    DEACTIVATE = "deactivate"


class AuditSink(Protocol):
    """What this module needs of `AuditLogRepository`, and nothing more.

    Narrower than the repository protocol on purpose: the trail appends and never reads, so
    a service that holds one cannot use it to look at another venue's history.
    """

    async def record(
        self,
        *,
        user_id: int | None,
        entity: str,
        entity_id: int | None,
        action: str,
        diff: dict[str, Any] | None = None,
    ) -> Any: ...


def snapshot(row: object, *fields: str) -> dict[str, Any]:
    """The named columns of a row, as a mapping the diff helper can compare.

    A missing attribute is recorded as `None` rather than raising: the "before" of a row
    that does not exist yet is exactly that, and a create is the common case.
    """
    return {field: getattr(row, field, None) for field in fields}


class AuditTrail:
    """The audit log of one venue, as the services use it."""

    __slots__ = ("_sink",)

    def __init__(self, sink: AuditSink | None) -> None:
        self._sink = sink

    @property
    def is_silent(self) -> bool:
        """`True` when there is nowhere to record; see the module docstring."""
        return self._sink is None

    async def created(
        self,
        actor: AccessContext | None,
        entity: AuditEntity,
        entity_id: int | None,
        *,
        after: Mapping[str, Any] | None = None,
    ) -> None:
        """A row appeared. The diff carries what it was created with."""
        diff = None if after is None else changed_fields({}, after)
        await self._write(actor, entity, entity_id, AuditAction.CREATE, diff)

    async def updated(
        self,
        actor: AccessContext | None,
        entity: AuditEntity,
        entity_id: int | None,
        *,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        action: AuditAction = AuditAction.UPDATE,
    ) -> bool:
        """A row changed. Records only what actually moved; `False` when nothing did.

        The return value is the honest answer to "was there anything to record", which a
        caller occasionally needs (the toggle of TZ 5.4 fires on every tap and most taps
        change nothing). Nobody has to look at it.
        """
        diff = changed_fields(before, after)
        if diff is None:
            return False
        await self._write(actor, entity, entity_id, action, diff)
        return True

    async def deleted(
        self,
        actor: AccessContext | None,
        entity: AuditEntity,
        entity_id: int | None,
        *,
        before: Mapping[str, Any] | None = None,
    ) -> None:
        """A row went away. The diff carries what it was, since nothing else will."""
        diff = None if before is None else changed_fields({}, before)
        await self._write(actor, entity, entity_id, AuditAction.DELETE, diff)

    async def set_active(
        self,
        actor: AccessContext | None,
        entity: AuditEntity,
        entity_id: int | None,
        *,
        is_active: bool,
    ) -> None:
        """TZ 5.1: switched off keeping the history, or switched back on."""
        action = AuditAction.ACTIVATE if is_active else AuditAction.DEACTIVATE
        await self._write(
            actor,
            entity,
            entity_id,
            action,
            changed_fields({"is_active": not is_active}, {"is_active": is_active}),
        )

    async def _write(
        self,
        actor: AccessContext | None,
        entity: AuditEntity,
        entity_id: int | None,
        action: AuditAction,
        diff: dict[str, Any] | None,
    ) -> None:
        if self._sink is None:
            return
        await self._sink.record(
            user_id=None if actor is None else actor.user_id,
            entity=str(entity),
            entity_id=entity_id,
            action=str(action),
            diff=diff,
        )


#: A trail that records nothing. See the module docstring for the two callers that need it.
SILENT: Final = AuditTrail(None)


__all__ = [
    "SILENT",
    "AuditAction",
    "AuditEntity",
    "AuditSink",
    "AuditTrail",
    "snapshot",
]
