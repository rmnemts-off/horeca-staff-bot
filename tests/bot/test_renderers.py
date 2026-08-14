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

from typing import Final

import pytest
from src.bot.renderers import RenderDeps, stage_zero_registry, stage_zero_renderers
from src.db.models import User
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
