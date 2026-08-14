"""One queued row -> one Telegram message (TZ 6; plan tasks 31 and 32).

The worker holds no knowledge of notification types: it asks
:class:`~src.services.notifications.NotificationRegistry` for a renderer and sends whatever
comes back (`src/services/notifications.py` says why — a chain of ``if notification.type ==``
inside the loop turns every type of stage 1 into an edit of the scheduler). This module is
the other half of that seam: the five functions the bot layer attaches at start-up, each of
them a :class:`~src.services.notifications.NotificationRenderer` — a `Notification` in, an
:class:`~src.services.notifications.OutgoingMessage` or ``None`` out.

Nothing here sends anything, and nothing here decides who receives what. The recipient is
already on the row (``chat_id``, written when the row was queued), the wording is
`src/bot/texts/notifications.py`, and the checklist screen is `src/bot/views/checklist.py`
— the same function the handler draws, because the employee must not be able to tell the
push apart from the screen he opens himself (TZ 5.4, `src/bot/views/__init__.py`).

**The registry is built per transaction, not once per process.** A renderer of the opening
checklist has to *create* the run, which is a write, and a write belongs to the transaction
the delivery is concluded in — otherwise a run exists for a message that was never sent.
The protocol fixes the call to ``(notification)``, so the services cannot arrive as an
argument: they arrive as the closure the renderer is built from, and that closure is one
unit of work. :func:`stage_zero_registry` is therefore called by the worker for every
notification it delivers; five dataclasses in a dict is not a cost worth a global for.

**Why the opening checklist carries a callback.** Its message *is* the run (decision D11):
the employee ticks lines on it, and `checklist_runs.message_id` is what lets the next tap
edit that message instead of sending a second copy. The id, however, exists only after
Telegram has accepted the message, and a renderer does not send — the worker does. Two ways
out, and only one of them keeps the seam:

* the worker knows that a row of type ``opening_checklist`` needs its id written back. That
  is the type branch the registry exists to avoid, and stage 1 adds four more types to it;
* the message says so itself. :class:`TrackedMessage` is an ``OutgoingMessage`` plus
  :meth:`TrackedMessage.record_delivery`, and the worker calls that method on any message
  that has one (a structural check, `src/scheduler/worker.py`). The type stays where the
  type is known, the worker keeps asking "does this message want anything else" instead of
  "which type is this", and a stage-1 renderer that needs the same thing gets it for free.

**A template with no lines produces no message** (TZ 8.1, decision B1). The employee is not
sent an empty checklist; the manager has already been told by ``ChecklistService`` itself.
The renderer returns ``None``, which the registry's contract defines as "nothing to
deliver, mark the row sent" — the row is concluded rather than retried forever.

**The manager's four types sort nothing.** ``payload`` carries the snapshot as it was at
the moment of the event, critical lines first, put there by
:func:`~src.services.notifications._pending_payload`; this module marks and prints. A
renderer that sorted could sort differently from the screen the same run is drawn on.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from src.bot import texts
from src.bot.views import quoted
from src.bot.views.checklist import BLOCK_SEPARATOR, LINE_SEPARATOR, render_run
from src.bot.views.shifts import format_time
from src.db.models import ChecklistType, Notification, User
from src.services.checklists import ChecklistService
from src.services.notifications import (
    NotificationRegistry,
    NotificationRenderer,
    NotificationType,
    OutgoingMessage,
    payload_items,
)
from src.services.timezones import utc_now

#: What a person is called when the row points at a user who is no longer there. The same
#: answer as `MemberService.RosterEntry.full_name`, and for the same reason: a notification
#: about a deleted employee is still worth delivering, and the manager reads the rest of it.
UNKNOWN_PERSON: Final = ""


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class RenderError(RuntimeError):
    """Base class for a row this module cannot turn into a message."""


class MissingRecipientError(RenderError):
    """No chat to deliver to: no ``chat_id`` on the row and no user behind it either.

    Not a permanent failure by itself — a user record can come back before the queue gives
    up — so it is left to the worker's ordinary attempt counter rather than concluded here.
    """

    def __init__(self, notification_id: int) -> None:
        super().__init__(f"notification {notification_id} has no chat to be delivered to")
        self.notification_id = notification_id


class MalformedPayloadError(RenderError):
    """The row's payload does not hold what its type promised (TZ 6).

    Reachable only across versions — a row written by a build that spelled the payload
    differently — which is exactly why it is an error and not a default: a notification
    delivered with a guessed value is worse than one that fails loudly.
    """

    def __init__(self, notification_id: int, key: str) -> None:
        super().__init__(f"notification {notification_id} has no usable {key!r} in its payload")
        self.notification_id = notification_id
        self.key = key


# --------------------------------------------------------------------------------------
# What a renderer is allowed to reach
# --------------------------------------------------------------------------------------


class PersonDirectory(Protocol):
    """Who a ``user_id`` is, in the two ways a notification needs it.

    Deliberately one method: the renderers want a name to print and a personal chat to fall
    back on, and nothing else about a person. ``UserRepository`` satisfies it as it stands,
    so the worker hands over the repository of the transaction it opened.
    """

    async def get(self, user_id: int) -> User | None: ...


@dataclass(frozen=True, slots=True)
class RenderDeps:
    """Everything the five renderers of stage 0 are built on, over one transaction."""

    checklists: ChecklistService
    people: PersonDirectory


#: What is done with the id Telegram gave a message, once it has one.
DeliveryRecorder = Callable[[int, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TrackedMessage(OutgoingMessage):
    """An outgoing message that has bookkeeping of its own once it has been sent.

    The worker looks for :meth:`record_delivery` and not for a type name, so this stays a
    property of the message rather than a branch in the loop (see the module docstring).
    """

    record: DeliveryRecorder | None = None

    async def record_delivery(self, chat_id: int, message_id: int) -> None:
        if self.record is not None:
            await self.record(chat_id, message_id)


# --------------------------------------------------------------------------------------
# The five renderers of stage 0
# --------------------------------------------------------------------------------------


class StageZeroRenderers:
    """The renderers of TZ section 6, bound to one venue's services.

    A class rather than five closures because they share the two helpers at the bottom —
    the recipient of a row and the name of a person — and because a bound method is already
    the shape :class:`~src.services.notifications.NotificationRenderer` describes.
    """

    __slots__ = ("_deps",)

    def __init__(self, deps: RenderDeps) -> None:
        self._deps = deps

    # -- the one scheduled type (TZ 5.4, 6; plan task 32) --------------------------------

    async def opening_checklist(self, notification: Notification, /) -> OutgoingMessage | None:
        """Create the run and draw it, or deliver nothing at all (TZ 8.1, decision B1).

        The run is created here and not when the row was queued: what the employee gets is
        the checklist as the template stands *at the moment of the shift*, and a run created
        ten minutes earlier would freeze a version the manager may still have been editing
        (decision B3 pins the version to the run, so the moment the run is made is the
        moment the wording is fixed).

        ``create_run`` is idempotent per shift, which is what makes a redelivery safe: a
        second pass over the same row finds the run that already exists and draws it again
        rather than opening a second checklist for one shift (TZ 5.4).
        """
        chat_id = await self._chat_id(notification)
        payload = notification.payload
        user_id = notification.user_id
        if user_id is None:
            user_id = _int_or_none(payload.get("user_id"))
        if user_id is None:
            raise MalformedPayloadError(notification.id, "user_id")

        created = await self._deps.checklists.create_run(
            shift_id=_int_or_none(payload.get("shift_id")),
            user_id=user_id,
            checklist_type=_checklist_type(notification),
            moment=utc_now(),
        )
        if created.run is None:
            # No active template, or one without a single line. The manager has been told
            # by the service; the employee is sent nothing, and the row is concluded.
            return None
        view = await self._deps.checklists.view(created.run.id)
        if view is None or view.is_empty:
            return None

        screen = render_run(view)
        headline = texts.notification_headline(notification.type).format(
            start=_start_time(notification)
        )
        return TrackedMessage(
            chat_id=chat_id,
            text=BLOCK_SEPARATOR.join((headline, screen.text)),
            markup=screen.markup,
            record=_remember_message(self._deps.checklists, created.run.id),
        )

    # -- the four event types to the manager (TZ 5.4, 5.5, 6) ---------------------------

    async def checklist_template_empty(self, notification: Notification, /) -> OutgoingMessage:
        """TZ 8.1: the checklist did not go out, and the manager hears why."""
        return OutgoingMessage(
            chat_id=await self._chat_id(notification),
            text=texts.notification_headline(notification.type).format(
                checklist=texts.checklist_title(_checklist_type(notification)),
                full_name=await self._full_name(notification.payload.get("user_id")),
            ),
        )

    async def checklist_skipped(self, notification: Notification, /) -> OutgoingMessage:
        """TZ 5.4: finished with lines left unticked, plus the reason the employee gave."""
        return OutgoingMessage(
            chat_id=await self._chat_id(notification),
            text=await self._pending_message(notification, with_comment=True),
        )

    async def checklist_overdue(self, notification: Notification, /) -> OutgoingMessage:
        """TZ 6: past the deadline and still unfinished. No comment exists to quote yet."""
        return OutgoingMessage(
            chat_id=await self._chat_id(notification),
            text=await self._pending_message(notification, with_comment=False),
        )

    async def recipe_missing(self, notification: Notification, /) -> OutgoingMessage:
        """TZ 5.5: somebody looked for a card the venue has not entered."""
        payload = notification.payload
        query = payload.get("query")
        if not isinstance(query, str):
            raise MalformedPayloadError(notification.id, "query")
        return OutgoingMessage(
            chat_id=await self._chat_id(notification),
            text=texts.notification_headline(notification.type).format(
                full_name=await self._full_name(payload.get("reported_by")),
                query=quoted(query),
            ),
        )

    # -- shared parts -------------------------------------------------------------------

    async def _pending_message(self, notification: Notification, *, with_comment: bool) -> str:
        """The body both checklist alerts share: the headline, the lines, the reason."""
        payload = notification.payload
        blocks = [
            texts.notification_headline(notification.type).format(
                checklist=texts.checklist_title(_checklist_type(notification)),
                full_name=await self._full_name(payload.get("user_id")),
            )
        ]
        lines = _pending_block(payload)
        if lines:
            blocks.append(lines)
        comment = payload.get("skip_comment")
        if with_comment and isinstance(comment, str) and comment.strip():
            blocks.append(texts.NOTIFY_SKIP_COMMENT_TEMPLATE.format(comment=quoted(comment)))
        return BLOCK_SEPARATOR.join(blocks)

    async def _chat_id(self, notification: Notification) -> int:
        """Where this row goes.

        ``chat_id`` was resolved when the row was queued and is normally there; the lookup
        behind it is for the row written while the user did not exist yet (the service
        writes the row anyway — the queue is the record of what was meant to happen).
        """
        if notification.chat_id is not None:
            return notification.chat_id
        if notification.user_id is not None:
            user = await self._deps.people.get(notification.user_id)
            if user is not None:
                return user.telegram_id
        raise MissingRecipientError(notification.id)

    async def _full_name(self, user_id: object) -> str:
        """A person's name, quoted for the HTML every screen is written in."""
        resolved = _int_or_none(user_id)
        if resolved is None:
            return UNKNOWN_PERSON
        user = await self._deps.people.get(resolved)
        return quoted(user.full_name) if user is not None else UNKNOWN_PERSON


# --------------------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------------------


def stage_zero_renderers(deps: RenderDeps) -> Mapping[str, NotificationRenderer]:
    """Type -> renderer for the five types of stage 0, bound to one unit of work."""
    renderers = StageZeroRenderers(deps)
    return {
        NotificationType.OPENING_CHECKLIST: renderers.opening_checklist,
        NotificationType.CHECKLIST_TEMPLATE_EMPTY: renderers.checklist_template_empty,
        NotificationType.CHECKLIST_SKIPPED: renderers.checklist_skipped,
        NotificationType.CHECKLIST_OVERDUE: renderers.checklist_overdue,
        NotificationType.RECIPE_MISSING: renderers.recipe_missing,
    }


def attach_stage_zero(registry: NotificationRegistry, deps: RenderDeps) -> NotificationRegistry:
    """Bind the renderers above to a registry that already declares the types."""
    for notification_type, renderer in stage_zero_renderers(deps).items():
        registry.attach_renderer(notification_type, renderer)
    return registry


def stage_zero_registry(deps: RenderDeps) -> NotificationRegistry:
    """A registry of the stage-0 specs with every renderer attached (plan, task 31).

    Called once per delivery rather than once per process: see the module docstring — the
    renderers write, and a write belongs to the transaction the delivery is concluded in.
    """
    return attach_stage_zero(NotificationRegistry.stage_zero(), deps)


# --------------------------------------------------------------------------------------
# Payload helpers
# --------------------------------------------------------------------------------------


def _remember_message(checklists: ChecklistService, run_id: int) -> DeliveryRecorder:
    """What the checklist message does with its own id once Telegram has given it one."""

    async def record(chat_id: int, message_id: int) -> None:
        await checklists.remember_message(run_id=run_id, chat_id=chat_id, message_id=message_id)

    return record


def _pending_block(payload: Mapping[str, Any]) -> str:
    """The unticked lines, in the order the service put them in (critical first, TZ 6).

    A critical line is marked instead of bulleted: TZ 6 escalates on that flag, so it has to
    be visible in the one glance a manager gives a push at eight in the morning.
    """
    items = payload_items(payload)
    if not items:
        return ""
    lines = [texts.NOTIFY_PENDING_TITLE]
    lines.extend(_pending_line(item) for item in items)
    return LINE_SEPARATOR.join(lines)


def _pending_line(item: Mapping[str, Any]) -> str:
    text = quoted(str(item.get("text", "")))
    if item.get("is_critical"):
        return texts.NOTIFY_PENDING_CRITICAL_LINE_TEMPLATE.format(text=text)
    group_name = item.get("group_name")
    if isinstance(group_name, str) and group_name:
        return texts.NOTIFY_PENDING_WITH_GROUP_TEMPLATE.format(
            text=text,
            group_name=quoted(group_name),
        )
    return texts.NOTIFY_PENDING_LINE_TEMPLATE.format(text=text)


def _checklist_type(notification: Notification) -> ChecklistType:
    """The type of checklist a row is about, as the payload spells it."""
    raw = notification.payload.get("checklist_type")
    if not isinstance(raw, str):
        raise MalformedPayloadError(notification.id, "checklist_type")
    try:
        return ChecklistType(raw)
    except ValueError as error:
        raise MalformedPayloadError(notification.id, "checklist_type") from error


def _start_time(notification: Notification) -> str:
    """The venue's own wall clock reading of the shift start, as «10:00».

    ``shifts.start_time`` is a ``TIME`` column — the venue's local reading already (decision
    D12) — so this is printing and not arithmetic, exactly as on the shift screen.
    """
    raw = notification.payload.get("start_time")
    if not isinstance(raw, str):
        raise MalformedPayloadError(notification.id, "start_time")
    try:
        return format_time(dt.time.fromisoformat(raw))
    except ValueError as error:
        raise MalformedPayloadError(notification.id, "start_time") from error


def _int_or_none(value: object) -> int | None:
    """JSONB gives back whatever was written; an id is only an id when it is a whole one."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = [
    "UNKNOWN_PERSON",
    "DeliveryRecorder",
    "MalformedPayloadError",
    "MissingRecipientError",
    "PersonDirectory",
    "RenderDeps",
    "RenderError",
    "StageZeroRenderers",
    "TrackedMessage",
    "attach_stage_zero",
    "stage_zero_registry",
    "stage_zero_renderers",
]
