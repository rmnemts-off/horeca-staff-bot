"""The root of the management section (TZ 5.2, 5.8; plan task 26).

Owner: plan task 26. One screen — the five blocks of TZ 5.8 as buttons — reached three ways
and drawn once:

* `texts.MENU_MANAGEMENT_BUTTON`, the caption on the reply keyboard (TZ 5.2 makes the main
  menu permanent, so a section is opened by its caption and not by a command);
* :class:`~src.bot.callbacks.OpenSection` naming the same section from an inline button —
  which is how the interrupt question of TZ 5.2 re-opens it after a wizard was dropped;
* `Nav(BACK, section=MANAGEMENT)` from any screen inside the section (decision D10).

**All three belong to this module and to no other** — the rule of
`src/bot/handlers/__init__.py`. Each block behind a button answers its own `OpenAdmin`
payload in its own `admin_*` module.

**The role is checked here even though the button is not drawn for `staff`.** TZ 9 and
CLAUDE.md are explicit that rights are checked on the server and not by hiding buttons:
`entries_for(STAFF)` leaves the caption out of the keyboard, and a bartender who types the
same words by hand — or replays an `OpenSection(MANAGEMENT)` payload — still reaches this
module. The resolver's rule for `OpenSection` is `staff`, because the factory serves every
section of the menu; the one section that needs more says so itself, here.

This module also holds :func:`deliver` and :func:`refuse`, the two answers every screen of
the block needs, so that `admin_venue.py` and the blocks written next to it do not each
grow their own copy of "edit the message if a button produced this update, send one if a
person typed it".
"""

from __future__ import annotations

from typing import Any, Final

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from src.bot import texts
from src.bot.callbacks import MenuAction, Nav, NavTarget, OpenSection
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.safe_edit import safe_edit
from src.bot.views import Screen
from src.bot.views import admin as screens
from src.services.access import AccessContext

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "admin"

#: Handler-context key aiogram fills with the `Bot` of the update.
BOT_KEY: Final = "bot"

Event = Message | CallbackQuery


def manager_of(data: dict[str, Any]) -> AccessContext | None:
    """The actor behind an update, if they may manage this venue at all (TZ 2, TZ 9).

    `None` covers both refusals with one answer, which is the same discipline the resolver
    keeps: whoever is told "no" is not told which half of the check failed.
    """
    actor = data.get(ACTOR_KEY)
    if isinstance(actor, AccessContext) and actor.is_manager:
        return actor
    return None


async def refuse(event: Event) -> None:
    """One wording for every refusal of this section (TZ 9: it reveals nothing)."""
    if isinstance(event, CallbackQuery):
        await event.answer(texts.ERROR_NOT_ALLOWED, show_alert=True)
        return
    await event.answer(texts.ERROR_NOT_ALLOWED)


async def deliver(event: Event, screen: Screen, *, bot: Bot) -> None:
    """Put a screen in front of whoever produced the update (TZ 8.2).

    A button press **edits the message it was pressed on** — that is the rule of TZ 8.2, and
    `safe_edit` is what makes it survive the three answers Telegram gives. Something typed
    cannot edit anything: the person's own message is now the last thing in the chat, so the
    next step is sent, and the wizard reads as a conversation instead of as a screen
    scrolling away above the keyboard.
    """
    if isinstance(event, CallbackQuery):
        screen_message = event.message
        if not isinstance(screen_message, Message):
            # The message the button lives on is older than Telegram keeps, so there is
            # nothing to edit and no chat we may assume. The spinner is still closed (TZ 9).
            await event.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
            return
        await safe_edit(
            bot,
            chat_id=screen_message.chat.id,
            message_id=screen_message.message_id,
            text=screen.text,
            reply_markup=screen.markup,
            answer=event.answer,
        )
        return
    await event.answer(screen.text, reply_markup=screen.markup)


async def open_board(event: Event, **data: Any) -> None:
    """The board of TZ 5.8 — the one screen this module owns."""
    if manager_of(data) is None:
        await refuse(event)
        return
    await deliver(event, screens.board(), bot=data[BOT_KEY])


def router() -> Router:
    """The screens of this block (see the module docstring)."""
    instance = Router(name=ROUTER_NAME)
    instance.message.register(open_board, F.text == texts.MENU_MANAGEMENT_BUTTON)
    instance.callback_query.register(
        open_board,
        OpenSection.filter(F.section == MenuAction.MANAGEMENT),
    )
    instance.callback_query.register(
        open_board,
        Nav.filter((F.target == NavTarget.BACK) & (F.section == MenuAction.MANAGEMENT)),
    )
    return instance


__all__ = [
    "BOT_KEY",
    "ROUTER_NAME",
    "deliver",
    "manager_of",
    "open_board",
    "refuse",
    "router",
]
