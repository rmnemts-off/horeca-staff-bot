"""The keyboards of the way in (TZ 5.1, plan task 22).

**Nothing here carries the navigation row, and that is the point of this module.** Every
other screen of the bot is built with :func:`src.bot.keyboards.menu.submenu` or
:func:`~src.bot.keyboards.menu.wizard`, which add the back, home and cancel buttons TZ 5.2
and 8.2 require. Onboarding is the one place where all three would be buttons into nowhere,
which TZ 8.1 forbids more strongly than 5.2 requires them:

* **home** leads to the main menu, and the main menu is drawn per role
  (`keyboards/menu.py`) — the person answering these screens has no role yet, because the
  membership is what the screens are about;
* **back** has no previous screen: the road in starts at `/start` or at a deep link, and
  neither is a screen one can return to;
* **cancel** returns to the menu without saving (TZ 8.2's own wording) — again, to a menu
  that does not exist for this person.

So the way out of an onboarding screen is to answer it, or to stop writing to the bot; the
gate in `src/bot/middlewares/auth.py` keeps the door in exactly the same place tomorrow.

The captions are `src/bot/texts/onboarding.py`'s and the payloads `src/bot/callbacks.py`'s,
as everywhere else: a keyboard module formats nothing and knows no venue data.
"""

from __future__ import annotations

from collections.abc import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot import texts
from src.bot.callbacks import InviteConfirm, VenueCreate, VenueSelect


def invite_confirmation() -> InlineKeyboardMarkup:
    """Yes / "correct the name" under the name the manager wrote on the code (B8).

    Both answers come from one factory, so the pair cannot drift apart; the negative one is
    a correction and not a refusal — there is no way to decline the invitation here, and
    inventing one would be a business rule TZ 5.1 does not have.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.YES_BUTTON,
                    callback_data=InviteConfirm(is_correct=True).pack(),
                ),
                InlineKeyboardButton(
                    text=texts.ONBOARDING_NAME_EDIT_BUTTON,
                    callback_data=InviteConfirm(is_correct=False).pack(),
                ),
            ]
        ]
    )


def venue_choice(options: Iterable[tuple[int, str]]) -> InlineKeyboardMarkup:
    """One venue per row (TZ 5.1: somebody who works in two places picks one).

    A row each rather than two abreast: a venue's name is free text entered by its owner and
    a pair of long ones is cut on a phone, while the list is two or three entries long — the
    height costs nothing and the reading costs everything.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=name,
                    callback_data=VenueSelect(venue_id=venue_id).pack(),
                )
            ]
            for venue_id, name in options
        ]
    )


def venue_missing() -> InlineKeyboardMarkup:
    """The "create a venue" button — decision A3, and all this screen can offer.

    The wizard behind the button is `src/bot/handlers/admin_venue.py` (plan task 26); this
    module's business ends at drawing the way to it.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.ONBOARDING_CREATE_VENUE_BUTTON,
                    callback_data=VenueCreate().pack(),
                )
            ]
        ]
    )


__all__ = ["invite_confirmation", "venue_choice", "venue_missing"]
