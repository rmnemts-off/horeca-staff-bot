"""Notifications: the registry of types, and the only place a row enters the queue.

TZ section 6 is categorical: *every* notification goes through the ``notifications`` table,
"scheduled -> sent", so that a change of the schedule can cancel it and a restart of the
service cannot duplicate it. Two consequences shape this module.

**Nothing here sends anything.** There is no ``bot`` object, no aiogram import and no
``send_message`` in sight. A service that needs to tell somebody something writes a row;
the worker (plan, task 31) is the single process that talks to Telegram. An "immediate"
notification is not an exception — it is a row whose ``scheduled_at`` is *now*, which the
worker picks up on its next pass. That is what makes criterion 11.4 checkable at all: one
queue, one delivery path, one place where a crash can be reasoned about.

**The worker does not know the types.** Delivery differs per type — an ordinary text to a
manager, the checklist message with its buttons to an opener — and the naive version of
that is a chain of ``if notification.type == ...`` inside the worker loop, which turns
every new type of stage 1 into an edit of the scheduler. Instead the type registry lives
here: :class:`NotificationRegistry` maps a type to its :class:`NotificationSpec`, the bot
layer attaches a renderer per type at start-up, and the worker only ever asks the registry.
Adding "shift reminder" later is a new spec plus a new renderer, and the worker is not
touched. A type nobody declared cannot even be queued (:meth:`NotificationService`
validates against the registry), and a row whose type is unknown at delivery time raises
:class:`UnknownNotificationTypeError` — which the worker turns into ``failed`` instead of
dying (plan, task 31).

**The one scheduled type of stage 0** is the opening checklist. Its moment is
``shift start - opening_checklist_lead_minutes``, and both halves of that sentence matter:

* the shift start is a *venue-local* wall clock reading (``shifts.start_time`` is a ``TIME``
  column, decision D12), so it becomes an instant only through
  :func:`src.services.timezones.shift_window`;
* the lead is subtracted **from the instant**, never from the wall clock. On the night
  Europe/Berlin springs forward there is no 02:50 at all, and a lead subtracted in local
  time would produce a reading that never existed — or, worse, one silently resolved to an
  hour on the wrong side of the transition. Subtracting from the instant is always "ten
  minutes of real time before the shift".

The moment is never in the past: if it has already gone by while the shift itself has not
ended, the row is queued for *now*. This is not a theoretical branch — in the acceptance
run a manager creates a shift that starts in five minutes with a lead of ten, and the
opener must still get the checklist.

**Every key starts with the venue.** ``uq_notifications_dedup_key`` is one column and the
whole table, so the idempotency key is the only thing keeping two venues out of each
other's queue slots. It is assembled in :meth:`NotificationService._enqueue` rather than by
the callers, and the venue is its first part — see :func:`dedup_key` for what an omitted
prefix costs (acceptance 11.3).

**Where the other services plug in.** ``checklists`` and ``recipes`` must be able to tell a
manager something without knowing that a queue exists, so each declares the narrowest
protocol it needs — :class:`src.services.checklists.ChecklistNotifier` (three methods) and
:class:`src.services.recipes.RecipeNotifier` (one). :class:`NotificationService` is the
adapter that satisfies both on top of the registry above: the four methods under "event
notifications to the manager" are that implementation and exist for no other caller. Nothing
is declared twice — the protocols stay in the services that consume them, the types stay
here, and the seam is asserted structurally in ``tests/services/test_notifications.py``
rather than by an inheritance the run time would not check.

The other half of the seam is the recipient list. "A notification to the manager" is answer
A4 and has exactly one implementation, ``AccessService.manager_recipient_ids``; this module
reaches it through :class:`ManagerRecipients`, a hole the size of that one method. So the
rule "every active manager, or the owners when the venue has none" is never restated here,
and no payload below carries a phrase — only type names, ids and the item snapshot. The
wording lives in ``src/bot/texts/`` and is applied by the renderer at delivery time (TZ 8.2).

Timezone conversion and the clock both come from ``src/services/timezones.py`` (decision
D12): this module calls :func:`src.services.timezones.utc_now` and never ``datetime.now``,
so a test pins time with ``use_clock(FixedClock(...))`` and gets deterministic
``scheduled_at`` values.
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from src.db.models import (
    ChecklistType,
    Notification,
    NotificationStatus,
    Shift,
    ShiftStatus,
)
from src.db.repositories.protocols import (
    NotificationRepository,
    UserRepository,
    VenueRepository,
    VenueSettingsRepository,
)
from src.services.checklists import EmptyTemplateAlert, PendingItemsAlert
from src.services.recipes import MissingRecipeAlert
from src.services.timezones import ensure_utc, shift_window, utc_now, venue_timezone

# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------

#: What a row is *about*, stored in `notifications.entity` next to `entity_id`. TZ section 6
#: requires that a change of a shift finds every notification bound to it without parsing
#: the payload, and that is exactly what this pair is for.
SHIFT_ENTITY = "shift"
CHECKLIST_RUN_ENTITY = "checklist_run"

#: Length of the digest that stands in for free text inside a `dedup_key`, see
#: `recipe_missing`. Sixty-four bits: a collision would merely merge two live rows of the
#: same type in the same venue, and the key has to stay short enough to read in a log.
_DIGEST_LENGTH = 16


class NotificationType(enum.StrEnum):
    """The types stage 0 delivers, out of the closed list of TZ section 6.

    A string rather than a PG enum on purpose (see the comment on ``notifications.type``):
    the list grows stage by stage and belongs next to its renderer, not in a migration.
    Stage 1 adds the remaining rows of the table in section 6 — a shift reminder, the
    closing checklist, the order reminder, tomorrow's shifts, a schedule change.
    """

    #: TZ 5.4 and 6: to the opener, `opening_checklist_lead_minutes` before the shift.
    OPENING_CHECKLIST = "opening_checklist"
    #: TZ 8.1 and decision B1: the template has no items, so the employee gets nothing and
    #: the manager is told why (plan, "three event notifications to the manager").
    CHECKLIST_TEMPLATE_EMPTY = "checklist_template_empty"
    #: TZ 5.4: finished with items left unticked; critical ones first (TZ 6).
    CHECKLIST_SKIPPED = "checklist_skipped"
    #: TZ 6: `checklist_overdue_minutes` after the start of the shift, still unfinished.
    CHECKLIST_OVERDUE = "checklist_overdue"
    #: TZ 5.5: "Report that the recipe is missing".
    RECIPE_MISSING = "recipe_missing"


class ScheduleOutcome(enum.StrEnum):
    """Why a scheduling call did or did not leave a live row behind."""

    #: Queued for a moment in the future.
    SCHEDULED = "scheduled"
    #: The computed moment had already passed, so the row is queued for now.
    IMMEDIATE = "immediate"
    #: This shift wants no checklist (not the opener any more, or cancelled): rows dropped.
    CANCELLED = "cancelled"
    #: The shift is over; there is nothing left to open.
    IN_THE_PAST = "in_the_past"
    #: This checklist has already been delivered for this shift, so the queue is left alone
    #: (see :meth:`NotificationService.shift_saved`).
    ALREADY_SENT = "already_sent"


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class NotificationError(RuntimeError):
    """Base class for everything this module refuses to do."""


class UnknownNotificationTypeError(NotificationError):
    """A type that no spec declares — at scheduling time, or in a row being delivered."""

    def __init__(self, notification_type: str) -> None:
        super().__init__(
            f"notification type {notification_type!r} is not in the registry; declare a "
            f"NotificationSpec for it (TZ 6 keeps the list of types closed)"
        )
        self.notification_type = notification_type


class DuplicateNotificationTypeError(NotificationError):
    """Two specs for one type: the second would silently shadow the first."""

    def __init__(self, notification_type: str) -> None:
        super().__init__(f"notification type {notification_type!r} is already registered")
        self.notification_type = notification_type


class RendererNotRegisteredError(NotificationError):
    """The type is known but nothing can turn it into a message yet.

    Raised in the worker when the bot layer forgot to attach a renderer at start-up. It is
    deliberately distinct from :class:`UnknownNotificationTypeError`: one means "wiring is
    incomplete", the other "this row was written by a version that knew more types".
    """

    def __init__(self, notification_type: str) -> None:
        super().__init__(
            f"no renderer is attached to notification type {notification_type!r}; the bot "
            f"layer registers one for every type it can deliver"
        )
        self.notification_type = notification_type


class ForeignShiftError(NotificationError):
    """A shift of another venue reached a service scoped to this one.

    Not reachable through the normal path — the shift service is venue-scoped and reads
    from a venue-scoped repository — but the failure it prevents is silent and expensive:
    the checklist would be timed in the wrong venue's timezone and queued into the wrong
    venue's rows (TZ 3.3, acceptance 11.3).
    """

    def __init__(self, venue_id: int, shift_venue_id: int) -> None:
        super().__init__(
            f"this service is scoped to venue {venue_id}, the shift belongs to {shift_venue_id}"
        )
        self.venue_id = venue_id
        self.shift_venue_id = shift_venue_id


class UnknownVenueError(NotificationError):
    """The venue this service was created for does not exist (or was deleted mid-flight)."""

    def __init__(self, venue_id: int) -> None:
        super().__init__(f"venue {venue_id} not found")
        self.venue_id = venue_id


class VenueSettingsMissingError(NotificationError):
    """No `venue_settings` row: the venue was not created through the wizard (task 26)."""

    def __init__(self, venue_id: int) -> None:
        super().__init__(
            f"venue {venue_id} has no settings row, so the checklist lead time is unknown"
        )
        self.venue_id = venue_id


# --------------------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """One Telegram message, as the worker will send it.

    ``markup`` is typed as ``object`` on purpose: the service layer holds no aiogram types
    (TZ 3.2). The bot layer builds the keyboard and the worker passes it straight through.
    """

    chat_id: int
    text: str
    markup: object | None = None


class NotificationRenderer(Protocol):
    """Turns one queued row into the message to send.

    Returning ``None`` means "nothing to deliver, mark the row sent": the opening checklist
    renderer creates the run, and TZ 8.1 says a template without items produces no message
    to the employee at all — the manager has already been told separately.
    """

    # Positional-only: a renderer is an ordinary function in the bot layer and must not be
    # forced to name its argument the way this protocol happens to.
    async def __call__(self, notification: Notification, /) -> OutgoingMessage | None: ...


@dataclass(frozen=True, slots=True)
class NotificationSpec:
    """Everything the queue needs to know about one type, minus the wording.

    ``entity`` is what rows of this type are about, so that cancelling by object
    (``cancel_for_entity``) does not have to be spelled out by every caller.
    """

    #: A member of :class:`NotificationType`, or any string a later stage declares — the
    #: column is text (see the comment on ``notifications.type``) and the list of types
    #: grows by stage, so a spec registered by stage 1 needs no change here.
    type: str
    entity: str | None = None
    renderer: NotificationRenderer | None = None


def stage_zero_specs() -> tuple[NotificationSpec, ...]:
    """The five types of stage 0: one scheduled notification plus four to the manager.

    ``checklist_overdue`` is the fourth manager notification and comes straight from the
    table in TZ section 6; the checklist service already raises it (plan, task 17).
    """
    return (
        NotificationSpec(type=NotificationType.OPENING_CHECKLIST, entity=SHIFT_ENTITY),
        NotificationSpec(type=NotificationType.CHECKLIST_TEMPLATE_EMPTY, entity=SHIFT_ENTITY),
        NotificationSpec(type=NotificationType.CHECKLIST_SKIPPED, entity=CHECKLIST_RUN_ENTITY),
        NotificationSpec(type=NotificationType.CHECKLIST_OVERDUE, entity=CHECKLIST_RUN_ENTITY),
        NotificationSpec(type=NotificationType.RECIPE_MISSING),
    )


class NotificationRegistry:
    """Type -> spec, with the renderer attached separately by the bot layer.

    The split is what keeps wording out of the service layer: the specs are declared here
    (they are queue mechanics — the type name and the object it is about), the renderers
    are functions in the bot layer that read ``src/bot/texts/`` and build keyboards. The
    worker holds one registry, asks it for a renderer and knows nothing else.
    """

    __slots__ = ("_specs",)

    def __init__(self, specs: Iterable[NotificationSpec] = ()) -> None:
        self._specs: dict[str, NotificationSpec] = {}
        for spec in specs:
            self.register(spec)

    @classmethod
    def stage_zero(cls) -> NotificationRegistry:
        """A registry with the stage-0 specs and no renderers yet."""
        return cls(stage_zero_specs())

    def register(self, spec: NotificationSpec) -> NotificationSpec:
        """Declare a type. A second spec for the same type is a mistake, not an override."""
        key = str(spec.type)
        if key in self._specs:
            raise DuplicateNotificationTypeError(key)
        self._specs[key] = spec
        return spec

    def spec(self, notification_type: str) -> NotificationSpec:
        """The spec of a type, or :class:`UnknownNotificationTypeError`."""
        key = str(notification_type)
        found = self._specs.get(key)
        if found is None:
            raise UnknownNotificationTypeError(key)
        return found

    def attach_renderer(
        self,
        notification_type: str,
        renderer: NotificationRenderer,
    ) -> NotificationSpec:
        """Bind a renderer to a declared type (the bot layer does this at start-up)."""
        key = str(notification_type)
        updated = replace(self.spec(key), renderer=renderer)
        self._specs[key] = updated
        return updated

    def renderer_for(self, notification_type: str) -> NotificationRenderer:
        """The renderer of a type. The worker calls this instead of branching on the type."""
        spec = self.spec(notification_type)
        if spec.renderer is None:
            raise RendererNotRegisteredError(str(notification_type))
        return spec.renderer

    async def render(self, notification: Notification) -> OutgoingMessage | None:
        """Render one queued row — the whole of the worker's knowledge about types."""
        return await self.renderer_for(notification.type)(notification)

    @property
    def types(self) -> frozenset[str]:
        return frozenset(self._specs)

    def __contains__(self, notification_type: object) -> bool:
        return isinstance(notification_type, str) and str(notification_type) in self._specs

    def __iter__(self) -> Iterator[NotificationSpec]:
        return iter(tuple(self._specs.values()))

    def __len__(self) -> int:
        return len(self._specs)


# --------------------------------------------------------------------------------------
# Keys and moments
# --------------------------------------------------------------------------------------


def dedup_key(venue_id: int, notification_type: str, *parts: str | int) -> str:
    """The idempotency key of a row (decision D5): venue, type, and the subject.

    **The venue comes first, and it is not decoration.** ``uq_notifications_dedup_key`` is
    a single-column index over the whole table (schema notes), so a key that leaves the
    venue out is shared by every venue in the installation: the same event in venue B would
    find A's live row and re-arm it instead of queueing its own, and B's manager would
    simply never hear about it. That is acceptance 11.3 broken through a column nobody
    thinks of as data. The key is therefore assembled in one place —
    :meth:`NotificationService._enqueue` — so that a type added by a later stage cannot
    forget it.

    Uniqueness holds over the live part of the queue only (``scheduled`` and ``sending``,
    see the partial index in the schema notes), so the same key may be used again once the
    previous row has been delivered or cancelled — which is what makes "the opener was
    removed and put back" work.
    """
    return ":".join([str(venue_id), str(notification_type), *(str(part) for part in parts)])


def _digest(value: str) -> str:
    """A short, stable stand-in for free text inside a key (case and spacing folded)."""
    normalised = " ".join(value.split()).casefold()
    return hashlib.sha256(normalised.encode()).hexdigest()[:_DIGEST_LENGTH]


def opening_checklist_moment(
    *,
    shift_start: dt.datetime,
    lead_minutes: int,
    now: dt.datetime,
) -> dt.datetime:
    """When the opening checklist goes out, as an instant in UTC.

    ``shift_start`` is already an instant (``TIME`` plus date plus venue zone, resolved by
    ``src/services/timezones.py``), so the lead is plain instant arithmetic and survives a
    DST transition without a special case.

    The result is never in the past: a moment that has gone by becomes *now*, because a
    shift created five minutes before it starts still has to send its checklist (plan,
    task 32). Whether the shift is worth a checklist at all is decided by the caller —
    :meth:`NotificationService.shift_saved` refuses one for a shift that is already over.
    """
    planned = ensure_utc(shift_start) - dt.timedelta(minutes=lead_minutes)
    return max(planned, ensure_utc(now))


# --------------------------------------------------------------------------------------
# Collaborators
# --------------------------------------------------------------------------------------


class ManagerRecipients(Protocol):
    """Answer A4, as this module needs it.

    The rule — every active ``manager``, or the owners when the venue has none — has one
    implementation, ``AccessService.manager_recipient_ids``; this protocol is the hole it
    plugs into, so that the definition cannot be restated here.
    """

    async def manager_recipient_ids(self, venue_id: int) -> tuple[int, ...]: ...


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    """What a scheduling call did: one live row, or none and why."""

    outcome: ScheduleOutcome
    notification: Notification | None = None
    scheduled_at: dt.datetime | None = None
    cancelled: int = 0

    @property
    def is_queued(self) -> bool:
        return self.notification is not None


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------


class NotificationService:
    """Writes rows into the queue; reads the venue's clock settings to know when.

    Scoped to one venue like the repositories it is built on (TZ 3.3). Every method is safe
    to call twice: scheduling goes through ``upsert_scheduled`` on a ``dedup_key``, so a
    shift saved twice moves one row instead of queueing two messages.
    """

    def __init__(
        self,
        *,
        venue_id: int,
        venues: VenueRepository,
        settings: VenueSettingsRepository,
        users: UserRepository,
        notifications: NotificationRepository,
        recipients: ManagerRecipients,
        registry: NotificationRegistry | None = None,
    ) -> None:
        self._venue_id = venue_id
        self._venues = venues
        self._settings = settings
        self._users = users
        self._notifications = notifications
        self._recipients = recipients
        # The service only validates types against the registry; rendering happens in the
        # worker, which holds the instance the bot layer attached its renderers to.
        self._registry = registry if registry is not None else NotificationRegistry.stage_zero()

    @property
    def venue_id(self) -> int:
        return self._venue_id

    @property
    def registry(self) -> NotificationRegistry:
        return self._registry

    # ---------------------------------------------------------------------------------
    # The scheduled type: the opening checklist (TZ 5.4, 6; plan tasks 19, 32, 33)
    # ---------------------------------------------------------------------------------

    async def shift_saved(self, shift: Shift) -> ScheduleResult:
        """Plan or replan everything that hangs off one shift (plan, task 33).

        Called by the shift service on every create and every edit — handlers never call
        the scheduler themselves (plan, task 16). Stage 0 has exactly one scheduled type,
        so this is the opening checklist and nothing else.

        A shift that no longer wants it — the opener flag was moved to somebody else, or
        the shift was cancelled — has its live rows cancelled rather than left to fire.

        **A checklist that has already gone out is not queued a second time.** The partial
        unique index behind ``dedup_key`` covers the live half of the queue only (see the
        constant), which is what lets an opener be removed and put back; the same property
        means a *delivered* row holds no key, so without the check below any later edit of
        the shift — a closer assigned at three in the afternoon, an end time corrected —
        would insert a fresh row and the opener would receive the whole checklist again in
        the middle of their shift. Worse than the noise: the run already exists, so the
        renderer draws it again and ``checklist_runs.message_id`` moves to the new copy,
        and the message the bartender has been ticking stops being the one the bot updates
        (criterion 11.4, TZ 8.2, decision D11).
        """
        if shift.venue_id != self._venue_id:
            raise ForeignShiftError(self._venue_id, shift.venue_id)

        if not _wants_opening_checklist(shift):
            return ScheduleResult(
                outcome=ScheduleOutcome.CANCELLED,
                cancelled=await self.shift_removed(shift.id),
            )

        zone = await self._zone()
        start, end = shift_window(shift.shift_date, shift.start_time, shift.end_time, zone)
        now = utc_now()
        if now >= end:
            # The shift is over: an opening checklist for it can no longer be opened, so
            # queueing one would only be noise. Anything queued earlier goes too — a shift
            # corrected to a past date must not fire the push it had while it was future.
            return ScheduleResult(
                outcome=ScheduleOutcome.IN_THE_PAST,
                cancelled=await self.shift_removed(shift.id),
            )

        if await self._already_delivered(shift.id, NotificationType.OPENING_CHECKLIST):
            # Delivered rows are history and are left exactly as they are — cancelling one
            # would rewrite the record of a message that was really sent.
            return ScheduleResult(outcome=ScheduleOutcome.ALREADY_SENT)

        moment = opening_checklist_moment(
            shift_start=start,
            lead_minutes=await self._opening_lead_minutes(),
            now=now,
        )
        notification = await self._enqueue(
            notification_type=NotificationType.OPENING_CHECKLIST,
            key_parts=(SHIFT_ENTITY, shift.id),
            scheduled_at=moment,
            user_id=shift.user_id,
            entity_id=shift.id,
            payload={
                "shift_id": shift.id,
                "user_id": shift.user_id,
                "shift_date": shift.shift_date.isoformat(),
                "start_time": shift.start_time.isoformat(),
                "checklist_type": ChecklistType.OPENING.value,
            },
        )
        outcome = ScheduleOutcome.IMMEDIATE if moment <= now else ScheduleOutcome.SCHEDULED
        return ScheduleResult(outcome=outcome, notification=notification, scheduled_at=moment)

    async def _already_delivered(self, shift_id: int, notification_type: str) -> bool:
        """Whether a row of this type for this shift has already reached its recipient.

        Asked of the queue rather than of the checklist service, because the queue is what
        the answer is about: a run may exist for other reasons (an employee who opened the
        checklist from the shift screen), and a run may be missing after a delivery that
        found the template empty — in both cases what must not happen twice is the *push*.
        """
        rows = await self._notifications.list_for_entity(
            entity=SHIFT_ENTITY,
            entity_id=shift_id,
        )
        return any(
            row.type == notification_type and row.status is NotificationStatus.SENT for row in rows
        )

    async def shift_removed(self, shift_id: int) -> int:
        """Cancel every live row bound to a shift (TZ 6: a deleted shift notifies nobody).

        Returns how many rows were cancelled. Rows already delivered are history and are
        left alone.
        """
        return await self._notifications.cancel_for_entity(
            entity=SHIFT_ENTITY,
            entity_id=shift_id,
        )

    # ---------------------------------------------------------------------------------
    # Event notifications to the manager (implements ChecklistNotifier and RecipeNotifier)
    # ---------------------------------------------------------------------------------

    async def checklist_template_empty(self, alert: EmptyTemplateAlert) -> None:
        """TZ 8.1: the template has no items, so the manager hears about it and the
        employee gets nothing at all."""
        await self._notify_managers(
            notification_type=NotificationType.CHECKLIST_TEMPLATE_EMPTY,
            subject=(SHIFT_ENTITY, alert.shift_id),
            payload={
                "checklist_type": alert.checklist_type.value,
                "template_id": alert.template_id,
                "shift_id": alert.shift_id,
                "user_id": alert.user_id,
            },
        )

    async def checklist_finished_with_skips(self, alert: PendingItemsAlert) -> None:
        """TZ 5.4: finished with items left unticked, plus the reason the employee gave."""
        await self._notify_managers(
            notification_type=NotificationType.CHECKLIST_SKIPPED,
            subject=(CHECKLIST_RUN_ENTITY, alert.run_id),
            payload=_pending_payload(alert),
        )

    async def checklist_overdue(self, alert: PendingItemsAlert) -> None:
        """TZ 6: the checklist passed its deadline unfinished."""
        await self._notify_managers(
            notification_type=NotificationType.CHECKLIST_OVERDUE,
            subject=(CHECKLIST_RUN_ENTITY, alert.run_id),
            payload=_pending_payload(alert),
        )

    async def recipe_missing(self, alert: MissingRecipeAlert) -> None:
        """TZ 5.5: an employee looked for a recipe that is not there.

        The subject is free text, so the key carries a digest of the query: two taps on the
        same button produce one live row, while a different query is a different report.
        """
        await self._notify_managers(
            notification_type=NotificationType.RECIPE_MISSING,
            subject=(None, None),
            discriminator=_digest(alert.query),
            payload={"query": alert.query, "reported_by": alert.reported_by},
        )

    # ---------------------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------------------

    async def _notify_managers(
        self,
        *,
        notification_type: NotificationType,
        subject: tuple[str | None, int | None],
        payload: Mapping[str, Any],
        discriminator: str | None = None,
    ) -> tuple[Notification, ...]:
        """One row per recipient, queued for now — the worker delivers all of them.

        "Immediate" means ``scheduled_at = now()`` and nothing else (TZ 6). The recipient
        is part of the key: two managers are two rows, and each is deduplicated on its own.

        **The subject reaches the key and not the `entity` columns.** Those two columns
        exist for one purpose — :meth:`shift_removed` cancels by them — and an event that
        has already happened is not cancelled by anything: "the template was empty when
        Anna's shift opened" stays true after the shift is deleted, and the manager still
        has to hear it. Writing `entity='shift'` on these rows put them in the path of
        `cancel_for_entity`, so deleting the shift silently withdrew the alert about it,
        which is precisely the report nobody would then notice was missing.
        """
        entity, entity_id = subject
        moment = utc_now()
        queued: list[Notification] = []
        for user_id in await self._recipients.manager_recipient_ids(self._venue_id):
            parts: list[str | int] = [part for part in (entity, entity_id) if part is not None]
            if discriminator is not None:
                parts.append(discriminator)
            parts.append(user_id)
            queued.append(
                await self._enqueue(
                    notification_type=notification_type,
                    key_parts=parts,
                    scheduled_at=moment,
                    user_id=user_id,
                    entity_id=None,
                    payload=payload,
                )
            )
        return tuple(queued)

    async def _enqueue(
        self,
        *,
        notification_type: NotificationType,
        key_parts: Sequence[str | int],
        scheduled_at: dt.datetime,
        user_id: int | None,
        entity_id: int | None,
        payload: Mapping[str, Any],
    ) -> Notification:
        """Write one row. The only place in the project that does.

        Callers hand over what identifies the *subject* of the notification; the key itself
        is assembled here, because its first two parts — the venue and the type — are true
        of every type there will ever be. Leaving the venue to the callers is how acceptance
        11.3 gets broken by omission: the unique index is global (see :func:`dedup_key`), so
        one forgotten prefix makes two venues share a queue slot.

        The spec lookup is a guard, not decoration: a type without a spec has no renderer
        either, and the row would sit in the queue until a human noticed. Failing here
        makes that a bug in the caller instead.
        """
        spec = self._registry.spec(str(notification_type))
        return await self._notifications.upsert_scheduled(
            dedup_key=dedup_key(self._venue_id, str(notification_type), *key_parts),
            notification_type=str(notification_type),
            scheduled_at=ensure_utc(scheduled_at),
            user_id=user_id,
            chat_id=await self._chat_id(user_id),
            payload=dict(payload),
            entity=spec.entity if entity_id is not None else None,
            entity_id=entity_id,
        )

    async def _chat_id(self, user_id: int | None) -> int | None:
        """The personal chat of a user, so that the worker needs no lookup of its own.

        ``None`` when the user is gone: the row is still written (the queue is the record
        of what was meant to happen) and the worker resolves or fails it.
        """
        if user_id is None:
            return None
        user = await self._users.get(user_id)
        return None if user is None else user.telegram_id

    async def _zone(self) -> ZoneInfo:
        venue = await self._venues.get(self._venue_id)
        if venue is None:
            raise UnknownVenueError(self._venue_id)
        return venue_timezone(venue.timezone)

    async def _opening_lead_minutes(self) -> int:
        """Read fresh every time: changing the setting has to move the next push (task 30)."""
        settings = await self._settings.get()
        if settings is None:
            raise VenueSettingsMissingError(self._venue_id)
        return settings.opening_checklist_lead_minutes


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _wants_opening_checklist(shift: Shift) -> bool:
    """TZ 5.4: only the opener gets the opening checklist, and only for a live shift."""
    return shift.is_opener and shift.status is not ShiftStatus.CANCELLED


def _pending_payload(alert: PendingItemsAlert) -> dict[str, Any]:
    """The snapshot a manager notification carries.

    The items travel with the row rather than being read back at delivery time: what the
    manager must see is the state at the moment of the event, and the order is already
    critical-first (TZ 6), so the renderer neither sorts nor decides what to mark.
    """
    return {
        "run_id": alert.run_id,
        "checklist_type": alert.checklist_type.value,
        "user_id": alert.user_id,
        "completed_by": alert.completed_by,
        "skip_comment": alert.skip_comment,
        "items": [
            {
                "item_id": item.item_id,
                "text": item.text,
                "group_name": item.group_name,
                "is_critical": item.is_critical,
            }
            for item in alert.items
        ],
    }


def payload_items(payload: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """Read back the item snapshot of a manager notification, whatever the row holds.

    JSONB gives the worker plain dictionaries; this keeps the shape assertion in one place
    so a renderer does not have to guess whether the key is there.
    """
    items = payload.get("items")
    if not isinstance(items, list):
        return ()
    return tuple(item for item in items if isinstance(item, dict))


__all__ = [
    "CHECKLIST_RUN_ENTITY",
    "SHIFT_ENTITY",
    "DuplicateNotificationTypeError",
    "ForeignShiftError",
    "ManagerRecipients",
    "NotificationError",
    "NotificationRegistry",
    "NotificationRenderer",
    "NotificationService",
    "NotificationSpec",
    "NotificationType",
    "OutgoingMessage",
    "RendererNotRegisteredError",
    "ScheduleOutcome",
    "ScheduleResult",
    "UnknownNotificationTypeError",
    "UnknownVenueError",
    "VenueSettingsMissingError",
    "dedup_key",
    "opening_checklist_moment",
    "payload_items",
    "stage_zero_specs",
]
