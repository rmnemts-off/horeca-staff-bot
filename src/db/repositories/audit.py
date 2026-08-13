"""Audit log (TZ 2, 4.7; plan task 14).

Two things live here: the venue-scoped repository, and the helper that turns "before" and
"after" into the `diff` column. The helper is not decoration — an audit record written by
hand is written differently in every call site, and TZ 2 asks for one readable answer to
"who changed what". It records only the fields that actually moved, and renders values as
JSON can carry them: `diff` is JSONB, and a `datetime`, a `Decimal` or an enum member put
into it raw is a serialisation error at the far end of an evening.
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from src.db.models import AuditLog
from src.db.repositories.base import BaseRepository

#: Keys of one entry of a diff: the value before the change and the value after it.
BEFORE_KEY = "from"
AFTER_KEY = "to"


def as_json(value: Any) -> Any:
    """The JSONB-safe form of a column value.

    Instants keep their offset (TZ 3.4: everything in the database is UTC), decimals stay
    exact as text rather than becoming a float, enum members become the value the schema
    stores, and anything else falls back to its string form instead of failing.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, enum.Enum):
        return as_json(value.value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): as_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [as_json(item) for item in value]
    return str(value)


def changed_fields(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, dict[str, Any]] | None:
    """What changed, as `{field: {"from": old, "to": new}}`; `None` when nothing did.

    Only the keys present in `after` are considered — that is the set of fields the caller
    meant to write — and a key whose value is equal on both sides is left out, so an
    "update" that changed nothing produces no diff and the record can be skipped entirely.
    """
    diff: dict[str, dict[str, Any]] = {}
    for field, new in after.items():
        old = before.get(field)
        if old == new:
            continue
        diff[field] = {BEFORE_KEY: as_json(old), AFTER_KEY: as_json(new)}
    return diff or None


class AuditLogRepo(BaseRepository[AuditLog]):
    """`audit_log` (TZ 4.7). Append only: a record of what happened is never rewritten."""

    model = AuditLog

    async def record(
        self,
        *,
        user_id: int | None,
        entity: str,
        entity_id: int | None,
        action: str,
        diff: dict[str, Any] | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            venue_id=self.venue_id,
            user_id=user_id,
            entity=entity,
            entity_id=entity_id,
            action=action,
            diff=diff,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def list_for_entity(
        self,
        *,
        entity: str,
        entity_id: int,
        limit: int,
    ) -> Sequence[AuditLog]:
        """History of one object, newest first."""
        stmt = (
            self.for_venue()
            .where(AuditLog.entity == entity, AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
        rows = await self.session.scalars(stmt)
        return rows.all()


__all__ = ["AFTER_KEY", "BEFORE_KEY", "AuditLogRepo", "as_json", "changed_fields"]
