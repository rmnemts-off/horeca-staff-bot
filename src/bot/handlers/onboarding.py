"""`/start`, the invite code and the way in (TZ 5.1; plan task 22).

Owner: plan task 22. What is here now is the one screen the framework of wave 1.5 drew — a
member who sends `/start` gets the main menu — moved out of `src/bot/routers.py` so that
every handler of this project lives in `src/bot/handlers/`, where the guard of
`tests/bot/test_handler_boundary.py` looks first.

The rest of TZ 5.1 — the deep link `?start=inv_XXXX`, a code typed by hand, confirming the
name the manager wrote on the code (decision B8), and the venue choice for somebody who
works in two — arrives with task 22. The gate in `src/bot/middlewares/auth.py` has already
decided that whoever reaches this module may be here at all: a stranger with no code never
gets past it.
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

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "onboarding"


async def start(message: Message, **data: Any) -> None:
    """A member who says `/start` gets the keyboard (TZ 5.2).

    `actor is None` is the person the gate let through *without* a membership: an invite
    code on its way to being activated, somebody who works in two venues and has not picked
    one, or the bootstrap owner of decision A3. Task 22 gives each of them a screen; until
    then the silence is deliberate — answering them with the menu of a venue they do not
    belong to would be worse than answering nothing.
    """
    actor: AccessContext | None = data.get(ACTOR_KEY)
    if actor is None:
        return
    await message.answer(texts.MENU_PROMPT, reply_markup=main_menu(actor.role))


def router() -> Router:
    """The screens of TZ 5.1 (see the module docstring)."""
    instance = Router(name=ROUTER_NAME)
    instance.message.register(start, CommandStart())
    return instance


__all__ = ["ROUTER_NAME", "router", "start"]
