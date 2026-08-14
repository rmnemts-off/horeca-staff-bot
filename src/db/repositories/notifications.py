"""The notification queue (TZ 4.7, section 6; decision D5, plan task 14).

`notifications` is the only queue in the project: APScheduler keeps no persistent job
store, so there is one place a pending message can be in. Criterion 11.4 — neither a lost
nor a duplicated notification — rests on three things this module implements:

* **claiming is a batch of one statement.** ``UPDATE ... WHERE id IN (SELECT ... FOR UPDATE
  SKIP LOCKED)`` moves rows to `sending` and hands them back. Two workers running at the
  same instant therefore split the queue instead of both delivering the same row: the
  second one skips what the first has locked and never waits for it;
* **scheduling is an upsert on `dedup_key`.** The unique index behind it is partial —
  only `scheduled` and `sending` rows own their key (schema notes) — so the same key may
  be scheduled again after the previous row was delivered or cancelled, which is what
  makes "the opener was removed from the shift and put back" work. The `ON CONFLICT`
  arbiter has to name that predicate, and it has to name it with the values rendered
  inline: PostgreSQL matches the parsed expression against the index, and a bound
  parameter never matches a constant;
* **status changes follow the claim, not the clock.** A row is claimed only while
  `scheduled`, reclaimed only while `sending`, cancelled only while it is still alive —
  and a delivery is recorded (:meth:`mark_sent`, :meth:`mark_failed`) only by the worker
  that still holds the claim it took. Anything else loses notifications: `upsert_scheduled`
  re-arms a live row *including one being delivered*, and a `mark_sent` that accepted
  `scheduled` too would stamp the re-armed row as delivered although what went out was the
  previous version of it. The re-arm wins instead, and the message the schedule now calls
  for is the one that goes out.

`attempts` counts *concluded* attempts: a failure and an abandoned claim each add one, a
success needs no counting (plan, task 31).

**`claimed_at` is the claim token, not bookkeeping.** `sending` alone cannot answer "am I
still the owner": it is one status shared by everyone, so a worker whose claim
:meth:`reclaim_stale` had already handed on would conclude *somebody else's* delivery —
stamping `sent` over a message not yet sent, or `failed` over one still in flight, and
`failed` is terminal, so nothing would ever pick that row up again. That is a lost
notification, and criterion 11.4 forbids it as firmly as a duplicated one.

So :meth:`claim_batch` writes the instant of the claim into `claimed_at` and hands the
claimed rows back carrying it; the worker keeps that value for as long as it talks to
Telegram and passes it to :meth:`mark_sent` / :meth:`mark_failed`, which match on it in
addition to the status. Everything that ends a claim — :meth:`reclaim_stale`,
:meth:`upsert_scheduled`, :meth:`reschedule`, :meth:`cancel_for_entity` — clears the
column, so the old token stops matching the moment the claim is taken away and the late
report simply changes nothing. The row then goes out under whoever holds it now, once.

The token is a timestamp because the schema is frozen and this column is what it has. It
identifies a claim as long as no two claims of the same row carry the same instant, and
they cannot: a row is claimable again only after `reclaim_stale`'s window has passed, and
the worker takes its `now` from `utc_now()` once per pass. The one way to break that is to
hand `claim_batch` the same `now` twice — a pinned clock in a test, never a running
worker — so a test about the token uses two distinct moments, as a worker does.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import NoResultFound

from src.db.models import Notification, NotificationStatus
from src.db.models.service import ACTIVE_NOTIFICATION_STATUSES
from src.db.repositories.base import BaseRepository


class NotificationRepo(BaseRepository[Notification]):
    """`notifications` (TZ 4.7). The worker owns claim and release, not the service."""

    model = Notification

    async def get(self, notification_id: int) -> Notification | None:
        return await self._reload(notification_id)

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
        """Schedule a notification, or re-arm the live row that already holds the key (D5).

        Re-arming keeps `attempts` — the history of what has already been tried — and
        clears `claimed_at` and `error`, because the row is going out again.
        """
        stmt = (
            pg_insert(Notification)
            .values(
                venue_id=self.venue_id,
                dedup_key=dedup_key,
                type=notification_type,
                scheduled_at=scheduled_at,
                user_id=user_id,
                chat_id=chat_id,
                payload=payload,
                entity=entity,
                entity_id=entity_id,
                status=NotificationStatus.SCHEDULED,
            )
            .on_conflict_do_update(
                index_elements=[Notification.dedup_key],
                index_where=Notification.status.in_(
                    [literal_column(f"'{status.value}'") for status in ACTIVE_NOTIFICATION_STATUSES]
                ),
                set_={
                    "type": notification_type,
                    "scheduled_at": scheduled_at,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "payload": payload,
                    "entity": entity,
                    "entity_id": entity_id,
                    "status": NotificationStatus.SCHEDULED,
                    "claimed_at": None,
                    "error": None,
                    "updated_at": func.now(),
                },
                where=Notification.venue_id == self.venue_id,
            )
            .returning(Notification.id)
        )
        written = await self.session.scalars(stmt)
        notification_id = written.first()
        if notification_id is None:
            # The key is held by a live row of another venue. Not reachable with the keys
            # the service builds, and silently dropping the notification would be worse.
            raise NoResultFound(
                f"dedup_key {dedup_key!r} is held by a live notification of another venue"
            )
        scheduled = await self._reload(notification_id)
        if scheduled is None:  # pragma: no cover - the row was just written in this transaction
            raise NoResultFound(f"notification {notification_id} disappeared after being written")
        return scheduled

    async def claim_batch(self, *, now: dt.datetime, limit: int) -> Sequence[Notification]:
        """Take up to `limit` due rows for this worker, skipping what others hold.

        `SKIP LOCKED` is the whole mechanism *against a worker running right now*: the rows
        another transaction has locked are not waited for and not returned, so two workers
        never carry the same row. It says nothing about a worker that took a row and then
        went quiet — that is :meth:`reclaim_stale`'s business, and the way the two are kept
        from concluding each other's work is `now`, written into `claimed_at` and carried
        back on every returned row. The caller keeps it and passes it to :meth:`mark_sent`
        or :meth:`mark_failed`; the module docstring says why nothing weaker does.
        """
        if limit <= 0:
            return []
        due = (
            select(Notification.id)
            .where(
                Notification.venue_id == self.venue_id,
                Notification.status == NotificationStatus.SCHEDULED,
                Notification.scheduled_at <= now,
            )
            .order_by(Notification.scheduled_at, Notification.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(Notification)
            .where(Notification.venue_id == self.venue_id, Notification.id.in_(due))
            .values(status=NotificationStatus.SENDING, claimed_at=now)
            .returning(Notification.id)
            .execution_options(synchronize_session=False)
        )
        claimed = await self.session.scalars(stmt)
        return await self._by_ids(claimed.all())

    async def reclaim_stale(self, *, older_than: dt.datetime) -> int:
        """Return rows stuck in `sending` to the queue (plan, task 31).

        A worker that died between claim and delivery leaves the row locked by nothing;
        after `older_than` it is scheduled again and the abandoned attempt is counted.

        Clearing `claimed_at` in the same statement is what makes this safe against a
        worker that was merely slow rather than dead: the token it still carries no longer
        matches anything, so its late `mark_sent` cannot conclude the claim that follows
        this one. The column is cleared rather than restamped because the row is not
        claimed any more, and because the only clock this repository may compare
        `claimed_at` against is the caller's — the same one `older_than` comes from.
        """
        stmt = (
            update(Notification)
            .where(
                Notification.venue_id == self.venue_id,
                Notification.status == NotificationStatus.SENDING,
                or_(
                    Notification.claimed_at.is_(None),
                    Notification.claimed_at <= older_than,
                ),
            )
            .values(
                status=NotificationStatus.SCHEDULED,
                claimed_at=None,
                attempts=Notification.attempts + 1,
            )
            .returning(Notification.id)
            .execution_options(synchronize_session=False)
        )
        reclaimed = await self.session.scalars(stmt)
        return len(reclaimed.all())

    async def mark_sent(
        self,
        notification_id: int,
        moment: dt.datetime,
        *,
        claimed_at: dt.datetime | None,
    ) -> None:
        """Delivered — recorded only while the row is still the caller's to conclude.

        Two conditions, and both are needed. `sending` says the row is being delivered at
        all: one that has left that status was re-armed by :meth:`upsert_scheduled` with a
        new moment or a new payload, or handed back by :meth:`reclaim_stale`, and stamping
        it `sent` would silently drop whatever it now says. `claimed_at` says the delivery
        being reported is *this* one: a row reclaimed and then taken by another worker is
        `sending` again, and without the token this call would conclude that worker's
        claim — a message stamped as delivered before it went out (criterion 11.4).

        `claimed_at` is the value :meth:`claim_batch` returned on the row. Passing ``None``
        is not a way to skip the check: it matches only a row with no claim at all, which
        `claim_batch` never leaves behind, so a caller that loses the token concludes
        nothing and the row is reclaimed instead of being wrongly closed.
        """
        stmt = (
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.venue_id == self.venue_id,
                Notification.status == NotificationStatus.SENDING,
                Notification.claimed_at == claimed_at,
            )
            .values(
                status=NotificationStatus.SENT,
                sent_at=moment,
                claimed_at=None,
                error=None,
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(stmt)

    async def mark_failed(
        self,
        notification_id: int,
        error: str,
        *,
        claimed_at: dt.datetime | None,
    ) -> None:
        """Not delivered (TZ section 6). The attempt is counted and the reason kept.

        Fenced by status *and* token for the same reason as :meth:`mark_sent`, and with
        more at stake: `failed` is terminal — :meth:`reclaim_stale` does not touch it —
        so a failure written over a claim that belongs to another worker buries a
        notification that is still on its way out, and nothing ever picks it up again.
        A failure belongs to the attempt that produced it and to no other.
        """
        stmt = (
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.venue_id == self.venue_id,
                Notification.status == NotificationStatus.SENDING,
                Notification.claimed_at == claimed_at,
            )
            .values(
                status=NotificationStatus.FAILED,
                error=error,
                claimed_at=None,
                attempts=Notification.attempts + 1,
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(stmt)

    async def reschedule(
        self,
        notification_id: int,
        scheduled_at: dt.datetime,
        *,
        claimed_at: dt.datetime | None = None,
    ) -> None:
        """Move a row to another moment, or put a failed one back into the queue.

        A `sent` or `cancelled` row is history and is left alone: rescheduling it would
        deliver the same message twice.

        `claimed_at` is the claim token, and it matters for the one caller that has one:
        the worker putting a row back after `TelegramRetryAfter`. By then its claim may
        already be gone — `reclaim_stale` returned the row and a second worker took it —
        and moving it without checking would strip that worker's claim mid-delivery, which
        is the double send `mark_sent` refuses for exactly the same reason. Omitted by a
        caller that never held a claim (the schedule moved, the venue's lead time changed).
        """
        stmt = (
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.venue_id == self.venue_id,
                Notification.status != NotificationStatus.SENT,
                Notification.status != NotificationStatus.CANCELLED,
            )
            .values(
                status=NotificationStatus.SCHEDULED,
                scheduled_at=scheduled_at,
                claimed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed_at is not None:
            stmt = stmt.where(
                or_(
                    Notification.status != NotificationStatus.SENDING,
                    Notification.claimed_at == claimed_at,
                )
            )
        await self.session.execute(stmt)

    async def cancel_for_entity(self, *, entity: str, entity_id: int) -> int:
        """Drop what is still pending about an object (TZ section 6: a shift was removed).

        Only live rows are touched, and cancelling frees the `dedup_key`, so the same
        notification can be scheduled again if the object comes back.
        """
        stmt = (
            update(Notification)
            .where(
                Notification.venue_id == self.venue_id,
                Notification.entity == entity,
                Notification.entity_id == entity_id,
                Notification.status.in_(ACTIVE_NOTIFICATION_STATUSES),
            )
            .values(status=NotificationStatus.CANCELLED, claimed_at=None)
            .returning(Notification.id)
            .execution_options(synchronize_session=False)
        )
        cancelled = await self.session.scalars(stmt)
        return len(cancelled.all())

    async def list_for_entity(self, *, entity: str, entity_id: int) -> Sequence[Notification]:
        stmt = (
            self.for_venue()
            .where(Notification.entity == entity, Notification.entity_id == entity_id)
            .order_by(Notification.scheduled_at, Notification.id)
        )
        rows = await self.session.scalars(stmt)
        return rows.all()

    async def _by_ids(self, ids: Sequence[int]) -> Sequence[Notification]:
        if not ids:
            return []
        stmt = (
            self.for_venue()
            .where(Notification.id.in_(ids))
            .order_by(Notification.scheduled_at, Notification.id)
            .execution_options(populate_existing=True)
        )
        rows = await self.session.scalars(stmt)
        return rows.all()

    async def _reload(self, notification_id: int) -> Notification | None:
        stmt = self.by_id(notification_id).execution_options(populate_existing=True)
        rows = await self.session.scalars(stmt)
        return rows.first()


__all__ = ["NotificationRepo"]
