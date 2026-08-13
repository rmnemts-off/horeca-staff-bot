"""The router tree of the bot (TZ 3.2, plan task 20).

Stage 0's framework draws exactly one screen: `/start` answers a member with the main menu.
Everything else — the onboarding of TZ 5.1, the shift and the schedule of TZ 5.3, the
checklist of TZ 5.4, the recipe card of TZ 5.5 and every management screen of TZ 5.8 —
arrives as its own router in plan tasks 22-30, included after this one.

**The interruption of TZ 5.2 is not here**, and that is deliberate: a main-menu press has
to be decided *before* routing starts, because aiogram resolves the FSM state once per
update and a router that clears it cannot stop a state-filtered sibling from matching. It
lives in `src/bot/middlewares/menu.py`, which explains the mechanics in full.

Routers are built by a factory rather than declared as module-level singletons: an aiogram
router may be included in one dispatcher only, so a second `build_dispatcher()` — which any
test of the assembly needs — would fail on a router already attached to the first.
"""

from __future__ import annotations

from typing import Any, Final

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from src.bot import texts
from src.bot.keyboards.menu import main_menu
from src.bot.middlewares.auth import ACTOR_KEY
from src.services.access import AccessContext

#: Name of the one router stage 0 declares; read in a traceback and in the assembly test.
SYSTEM_ROUTER: Final = "system"


async def start(message: Message, **data: Any) -> None:
    """The only screen stage 0's framework draws: the main menu (TZ 5.2).

    Everything TZ 5.1 asks of `/start` — the deep link, the invite code, confirming the
    name — belongs to plan task 22 and arrives as its own router in front of this one.
    Until then a member gets the keyboard, and a stranger never reaches this handler at
    all: the gate in `src/bot/middlewares/auth.py` answered them with the code prompt.
    """
    actor: AccessContext | None = data.get(ACTOR_KEY)
    if actor is None:
        return
    await message.answer(texts.MENU_PROMPT, reply_markup=main_menu(actor.role))


def system_router() -> Router:
    """Stage 0's one screen (see :func:`start`)."""
    router = Router(name=SYSTEM_ROUTER)
    router.message.register(start, CommandStart())
    return router


__all__ = ["SYSTEM_ROUTER", "start", "system_router"]
