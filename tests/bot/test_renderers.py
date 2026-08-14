"""The seam between the queue and the messages: every declared type must be renderable.

`src/services/notifications.py` keeps the list of types closed and the worker asks the
registry for a renderer instead of branching on the type
(`src/scheduler/worker.py`). That design has exactly one way to fail silently: a type is
declared, a spec is registered, wording is written for it — and nobody attaches a
renderer. Nothing catches that at import time. The row is claimed at run time, raises
`RendererNotRegisteredError`, and is marked ``failed`` for good: no retry can help, so the
notification is simply never delivered.

`tests/services/test_notifications.py` proves the *specs* cover `NotificationType`, and
`tests/bot/test_texts.py` proves the *wording* does. This file closes the third side of
the triangle — the renderers — over `stage_zero_registry`, which is the same function
`src/scheduler/__main__.py` calls per transaction, so a stage-1 type added without a
renderer turns this file red rather than the customer's queue.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

import pytest
from src.bot import texts
from src.bot.renderers import RenderDeps, stage_zero_registry, stage_zero_renderers
from src.db.models import ChecklistType, Notification, NotificationStatus, User
from src.services.notifications import NotificationType

from tests.services.test_checklists import Harness


class FakeUsers:
    """`PersonDirectory`: the one thing a renderer asks about a person (a name, a chat)."""

    def __init__(self, users: tuple[User, ...] = ()) -> None:
        self.users = {user.id: user for user in users}

    async def get(self, user_id: int) -> User | None:
        return self.users.get(user_id)


def deps() -> RenderDeps:
    """Deps of the shape production builds, over an empty in-memory checklist harness.

    Nothing here is rendered, so the services need no rows: what is under test is the
    wiring, and the wiring is decided by `stage_zero_renderers` alone.
    """
    return RenderDeps(checklists=Harness(templates=[], items=[]).service, people=FakeUsers())


#: Named so the failure message says which type is unwired rather than "index 3".
ALL_TYPES: Final[tuple[NotificationType, ...]] = tuple(NotificationType)


def test_the_types_of_the_stage_are_not_empty() -> None:
    """A guard on the guards below: an empty enum would make every check vacuous."""
    assert ALL_TYPES, "NotificationType declares the closed list of TZ 6 and cannot be empty"


@pytest.mark.parametrize("notification_type", ALL_TYPES, ids=str)
def test_every_notification_type_has_a_renderer_attached(
    notification_type: NotificationType,
) -> None:
    """The worker must find a renderer for every type the registry declares.

    Without it the row fails terminally on first claim (`src/scheduler/worker.py`: "unknown
    type / no renderer -> failed immediately"), which is a notification the customer never
    receives and no retry recovers.
    """
    registry = stage_zero_registry(deps())
    assert callable(registry.renderer_for(notification_type))


def test_the_renderer_table_matches_the_types_exactly() -> None:
    """No type without a renderer, and no renderer for a type nobody declares.

    The second half matters as much as the first: a renderer keyed by a string that is not
    a declared type is dead code that reads like coverage.
    """
    assert set(stage_zero_renderers(deps())) == {str(member) for member in NotificationType}


def test_the_registry_renders_nothing_it_does_not_declare() -> None:
    """The registry stays closed: attaching renderers must not widen the list of types."""
    assert stage_zero_registry(deps()).types == {str(member) for member in NotificationType}


def test_each_type_gets_its_own_renderer() -> None:
    """Two types sharing one function would be a copy-paste slip, not a design.

    Bound methods compare by ``__func__``, so this asks whether five distinct functions are
    wired and not whether five distinct objects were created.
    """
    functions = [renderer.__func__ for renderer in stage_zero_renderers(deps()).values()]  # type: ignore[attr-defined]
    assert len(set(functions)) == len(NotificationType)


# --------------------------------------------------------------------------------------
# The bodies (TZ 5.4, 6)
# --------------------------------------------------------------------------------------
#
# The wiring above is one half of the seam. The other half is what the manager actually
# reads, and nothing was asserting it: `_pending_block` could be deleted whole and three
# thousand tests stayed green, because every test of these types checked the *payload* the
# service wrote or the *escaping* of the text, and none of them checked that the lines
# reached the message at all. TZ 6 escalates on the critical flag, so "the manager sees
# which lines were skipped, critical ones first" is the requirement, not a detail.


MANAGER_ID: Final = 5
MANAGER_CHAT: Final = 500
OPENER_ID: Final = 7
OPENER_CHAT: Final = 700


def people() -> FakeUsers:
    return FakeUsers(
        (
            User(id=MANAGER_ID, telegram_id=MANAGER_CHAT, full_name="Olga"),
            User(id=OPENER_ID, telegram_id=OPENER_CHAT, full_name="Anna"),
        )
    )


def notification(notification_type: NotificationType, payload: dict[str, object]) -> Notification:
    return Notification(
        id=1,
        venue_id=1,
        user_id=MANAGER_ID,
        chat_id=MANAGER_CHAT,
        type=str(notification_type),
        payload=payload,
        scheduled_at=dt.datetime(2026, 8, 13, 5, 50, tzinfo=dt.UTC),
        status=NotificationStatus.SENDING,
    )


def pending_payload(**extra: object) -> dict[str, object]:
    """What `PendingItemsAlert` is written as — critical first, as the service ordered it."""
    return {
        "run_id": 3,
        "checklist_type": ChecklistType.OPENING.value,
        "user_id": OPENER_ID,
        "items": [
            {"item_id": 3, "text": "Ice well filled", "group_name": "Station", "is_critical": True},
            {"item_id": 1, "text": "Napkins", "group_name": "Hall", "is_critical": False},
        ],
        **extra,
    }


async def render(notification_type: NotificationType, payload: dict[str, object]) -> str:
    registry = stage_zero_registry(
        RenderDeps(checklists=Harness(templates=[], items=[]).service, people=people())
    )
    message = await registry.render(notification(notification_type, payload))
    assert message is not None
    return message.text


async def test_the_skipped_notification_shows_every_line_with_the_critical_one_first() -> None:
    """TZ 5.4 and 6: what was left unticked, and which of it escalates."""
    text = await render(NotificationType.CHECKLIST_SKIPPED, pending_payload(skip_comment="no ice"))

    assert texts.NOTIFY_PENDING_TITLE in text
    assert "Ice well filled" in text
    assert "Napkins" in text
    assert text.index("Ice well filled") < text.index("Napkins"), (
        "the service ordered them critical-first and the renderer must not resort them"
    )
    assert texts.NOTIFY_PENDING_CRITICAL_LINE_TEMPLATE.format(text="Ice well filled") in text
    assert "no ice" in text, "TZ 5.4: the reason the employee typed"


async def test_the_overdue_notification_shows_the_same_lines() -> None:
    text = await render(NotificationType.CHECKLIST_OVERDUE, pending_payload())

    assert texts.NOTIFY_PENDING_TITLE in text
    assert "Ice well filled" in text
    assert "Napkins" in text


async def test_a_notification_with_nothing_pending_prints_no_empty_heading() -> None:
    """An alert whose list is empty is a heading over nothing, which reads as a bug."""
    text = await render(NotificationType.CHECKLIST_SKIPPED, pending_payload(items=[]))

    assert texts.NOTIFY_PENDING_TITLE not in text


async def test_the_empty_template_notification_names_the_person_whose_shift_it_was() -> None:
    """TZ 8.1: the employee got nothing, so the manager is told whose shift it was."""
    text = await render(
        NotificationType.CHECKLIST_TEMPLATE_EMPTY,
        {
            "checklist_type": ChecklistType.OPENING.value,
            "template_id": 3,
            "shift_id": 42,
            "user_id": OPENER_ID,
        },
    )

    assert "Anna" in text


async def test_the_missing_recipe_notification_names_the_query_and_the_asker() -> None:
    text = await render(
        NotificationType.RECIPE_MISSING,
        {"query": "negroni", "reported_by": OPENER_ID},
    )

    assert "negroni" in text
    assert "Anna" in text
