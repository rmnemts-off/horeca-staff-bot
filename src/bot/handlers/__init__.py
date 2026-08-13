"""Update handlers, one module per functional block (TZ 3.2).

A handler accepts an update, calls a service and renders the reply; business logic never
lives here, so a web admin panel can later sit on top of the same services (CLAUDE.md).
`tests/bot/test_handler_boundary.py` is what keeps that true — a handler that imports a
repository, opens a session or takes a raw `callback_data` apart fails the build.

**The order below is the router tree, and it is the only thing this file decides.** aiogram
consults routers in the order they were included and stops at the first handler that
matches, so the order is a rule and not a listing:

1. `onboarding` — TZ 5.1. It owns `/start` and the invite code, which is the one road in
   for somebody who has no membership yet; nothing else may answer them.
2. `menu` — TZ 5.2 and 5.3: the sections of the main menu, «Моя смена» and «График». It is
   above the scenarios because a menu press ends the scenario it interrupts, and the
   middleware that enforces that (`src/bot/middlewares/menu.py`) has already run.
3. `checklist` — TZ 5.4, the employee's screen.
4. `recipes` — TZ 5.5, the search and the card.
5. the `admin_*` modules — TZ 5.8, one per block of the management section. They come last
   because every one of them is reached from a button of its own and nothing routes to them
   by accident; among themselves the order does not matter, because each filters on its own
   callback factories.

Registration is by factory (`Factory.filter()`) and by state, never by a raw prefix string:
the payload has already been parsed and checked by the resolver before any of this runs.
"""

from __future__ import annotations

from aiogram import Router

from src.bot.handlers import (
    admin_checklists,
    admin_recipes,
    admin_schedule,
    admin_staff,
    admin_venue,
    checklist,
    menu,
    onboarding,
    recipes,
)


def all_routers() -> tuple[Router, ...]:
    """Every handler router, in the order of the module docstring.

    A factory rather than module-level singletons: an aiogram router may be included in one
    dispatcher only, so a second `build_dispatcher()` — which every test of the assembly
    needs — would fail on a router already attached to the first.
    """
    return (
        onboarding.router(),
        menu.router(),
        checklist.router(),
        recipes.router(),
        admin_venue.router(),
        admin_staff.router(),
        admin_checklists.router(),
        admin_schedule.router(),
        admin_recipes.router(),
    )


__all__ = [
    "admin_checklists",
    "admin_recipes",
    "admin_schedule",
    "admin_staff",
    "admin_venue",
    "all_routers",
    "checklist",
    "menu",
    "onboarding",
    "recipes",
]
