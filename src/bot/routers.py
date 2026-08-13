"""The router tree of the bot (TZ 3.2, plan task 20).

This module owns the *shape* of the tree; the screens live in `src/bot/handlers/`, one
module per functional block, and their order is decided and explained there.

**The interruption of TZ 5.2 is not a router**, and that is deliberate: a main-menu press
has to be decided *before* routing starts, because aiogram resolves the FSM state once per
update and a router that clears it cannot stop a state-filtered sibling from matching. It
lives in `src/bot/middlewares/menu.py`, which explains the mechanics in full.

Routers are built by a factory rather than declared as module-level singletons: an aiogram
router may be included in one dispatcher only, so a second `build_dispatcher()` — which any
test of the assembly needs — would fail on a router already attached to the first.
"""

from __future__ import annotations

from aiogram import Router

from src.bot import handlers


def all_routers() -> tuple[Router, ...]:
    """Every router of the bot, in include order (see `src/bot/handlers/__init__.py`)."""
    return handlers.all_routers()


def router_names() -> tuple[str, ...]:
    """The names of the tree, in order — what the assembly test asserts against.

    Read off the routers themselves rather than listed a second time here: a list that
    repeats the tree is a list that stops matching it, and the test would then be asserting
    that two copies of the same mistake agree.
    """
    return tuple(str(instance.name) for instance in all_routers())


__all__ = ["all_routers", "router_names"]
