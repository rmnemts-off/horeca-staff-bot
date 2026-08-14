"""`/start`, the invite code and the way in (TZ 5.1; plan task 22).

This module owns the only road into the bot, which is why it is the first router of the tree
(`src/bot/handlers/__init__.py`). Four roads arrive here and all four end in the same place —
a membership and the main menu of TZ 5.2:

1. **the deep link** `t.me/<bot>?start=inv_XXXX`. The gate has already parsed the payload
   into `data["invite_code"]` (`src/bot/middlewares/auth.py`), so this module never sees a
   `/start` argument and never splits one;
2. **a code typed into the chat**, which is the same fact arriving on a message with no
   command. :class:`CarriesInviteCode` reads the very same context key, so both roads run
   the same three screens — and `Onboarding.code` is registered next to it for the message
   that was *meant* as a code and is not one, which the gate would otherwise answer with the
   refusal of TZ 5.1 and no explanation of which code failed;
3. **a `/start` from somebody who already works here** — straight to the menu, TZ 5.1 word
   for word. Somebody who works in two venues picks one first, and the choice is remembered
   on `users.active_venue_id` by `AccessService.select_venue`, not in the FSM state: it has
   to outlive both a restart and the day-long TTL of the store;
4. **the bootstrap owner of decision A3**, who belongs to no venue because none exists.
   That screen is the empty state of stage 0 (TZ 8.1) and it carries exactly one button.

**A `/start` from a stranger is not here at all.** TZ 5.1 answers it with the code prompt
and, in its own words, nothing else; the gate does that before any router runs — so this
module has no branch for it, and cannot grow one by accident.

**The name is confirmed, not read off the profile** (decision B8). The manager types it when
issuing the code, TZ 5.3 matches the imported schedule to people by full name, and a Telegram
profile holds nicknames. So the confirmation screen is a step of its own and correcting
the name is a first-class answer to it rather than something somebody has to ask for.

Everything the screens print comes from `AccessService`; everything they draw comes from
`src/bot/views/onboarding.py`. This module decides which of the two happens and nothing else.
"""

from __future__ import annotations

from typing import Any, Final

from aiogram import Bot, Router
from aiogram.filters import CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from src.bot import texts
from src.bot.callbacks import InviteConfirm, VenueSelect
from src.bot.keyboards.menu import main_menu
from src.bot.middlewares.auth import ACTOR_KEY, IDENTITY_KEY, INVITE_CODE_KEY
from src.bot.middlewares.menu import STATE_KEY
from src.bot.middlewares.resolver import PAYLOAD_KEY
from src.bot.middlewares.services import ACCESS_KEY
from src.bot.safe_edit import safe_edit
from src.bot.states import Onboarding
from src.bot.views import Screen
from src.bot.views import onboarding as views
from src.db.models import MemberRole
from src.services.access import (
    AccessContext,
    AccessError,
    AccessService,
    Identity,
    InviteRejection,
)
from src.services.timezones import utc_now

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "onboarding"

#: aiogram puts the `Bot` of the update into the handler context under this name.
BOT_KEY: Final = "bot"

#: The code being activated, kept in the FSM state because it is text: rule 2 of
#: `src/bot/callbacks.py` refuses free text in a button, and a code is nothing else.
CODE_FIELD: Final = "invite_code"

#: The name the code carries, so that the yes button need not read the code a second time.
NAME_FIELD: Final = "full_name"

#: Shortest thing this module will accept as a full name. It is a guard on typing and
#: not a policy: TZ 5.1 sets no format for a name, `MemberService.rename` refuses only an
#: empty one, and inventing a stricter rule here would reject people the manager can enter
#: by hand a screen away. It exists because TZ 5.1's own wording
#: ("too short, write your name and surname") presumes that something is measured.
MIN_FULL_NAME_LENGTH: Final = 3


class CarriesInviteCode(Filter):
    """True when the gate found an invite code on this update (TZ 5.1).

    A filter over the handler context rather than over the text: `invite_code_in()` in
    `src/bot/middlewares/auth.py` is the one place that decides what a code looks like — it
    strips `/start`, `/start@bot`, the `inv_` prefix and the case — and a second opinion
    here would be a second definition, drifting from the first the day either is edited.
    """

    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        return data.get(INVITE_CODE_KEY) is not None


# --------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------


async def start(message: Message, **data: Any) -> None:
    """`/start`, in the order TZ 5.1 puts the four cases in.

    The code comes first on purpose: a deep link followed by somebody who already works in
    another venue is a person joining a second one, and answering them with the menu of the
    first would silently drop the invitation they were sent.
    """
    invite_code: str | None = data.get(INVITE_CODE_KEY)
    if invite_code is not None:
        await _offer_the_code(message, code=invite_code, **data)
        return

    actor: AccessContext | None = data.get(ACTOR_KEY)
    if actor is not None:
        await message.answer(texts.MENU_PROMPT, reply_markup=main_menu(actor.role))
        return

    identity: Identity | None = data.get(IDENTITY_KEY)
    if identity is None:
        # No gate ran, so there is nothing this module can honestly say (TZ 5.1).
        return
    if identity.needs_venue_choice:
        await _ask_which_venue(message, **data)
        return
    if identity.may_create_venue:
        # Decision A3, and the empty state of TZ 8.1: there is no venue in the database yet.
        # The `users` row is written here and not by the wizard, because the wizard's first
        # question is the venue's name and by then the person must already exist to own it.
        who = message.from_user
        if who is None:
            # The gate refuses an update with nobody behind it, so this cannot happen; it is
            # spelled out because `from_user` is optional in the Bot API and mypy is right.
            return
        access: AccessService = data[ACCESS_KEY]
        await access.ensure_bootstrap_user(
            telegram_id=who.id,
            full_name=_profile_name(message),
            username=who.username,
        )
        await _send(message, views.venue_missing())


def _profile_name(message: Message) -> str:
    """The name the first owner arrives with — their Telegram profile.

    The only source there is: they have no invite code, and the wizard asks for the venue's
    name rather than for theirs. Decision B8 keeps profile names away from *employees*,
    because an imported schedule is matched to people by full name; the person creating the
    venue is matched to nothing.
    """
    who = message.from_user
    if who is None:
        return ""
    return " ".join(part for part in (who.first_name, who.last_name) if part)


async def by_code(message: Message, **data: Any) -> None:
    """A code typed into the chat, and the answer to a line that was meant to be one."""
    invite_code: str | None = data.get(INVITE_CODE_KEY)
    if invite_code is None:
        # `Onboarding.code` with nothing the gate could read as a code: the person is
        # retyping and got it wrong, which is worth naming (TZ 5.1) rather than ignoring.
        await _send(message, views.invite_rejected(InviteRejection.MALFORMED))
        return
    await _offer_the_code(message, code=invite_code, **data)


async def confirm(callback: CallbackQuery, **data: Any) -> None:
    """Yes activates the code; correcting the name opens `Onboarding.name` (B8).

    Both answers edit the screen they were pressed on (TZ 8.2) instead of writing a new
    message, so the question cannot be answered twice — which for yes would mean a second
    activation of a code that is single use, and an error message where there is no error.
    """
    state: FSMContext = data[STATE_KEY]
    payload: InviteConfirm = data[PAYLOAD_KEY]
    bot: Bot = data[BOT_KEY]

    stored = await state.get_data()
    code = stored.get(CODE_FIELD)
    screen_at = _screen_of(callback)
    if not isinstance(code, str) or screen_at is None:
        # The store forgot the scenario (a day's TTL) or the message is unreachable. Both
        # are the stale screen of TZ 8.2, and neither is worth a guess.
        await callback.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
        return
    chat_id = screen_at[0]

    if not payload.is_correct:
        await state.set_state(Onboarding.name)
        await _edit(bot, callback, screen_at, views.name_prompt())
        return

    stored_name = stored.get(NAME_FIELD)
    screen, role = await _join(
        access=data[ACCESS_KEY],
        state=state,
        code=code,
        full_name=stored_name if isinstance(stored_name, str) else "",
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
    )
    await _edit(bot, callback, screen_at, screen)
    if role is not None:
        # The permanent keyboard of TZ 5.2 is a reply keyboard, and a reply keyboard cannot
        # be attached to an edit — it arrives with the one message this screen sends.
        await bot.send_message(chat_id, texts.MENU_PROMPT, reply_markup=main_menu(role))


async def named(message: Message, **data: Any) -> None:
    """The corrected name, and the activation that follows it (TZ 5.1)."""
    state: FSMContext = data[STATE_KEY]
    typed = (message.text or "").strip()
    if len(typed) < MIN_FULL_NAME_LENGTH:
        await _send(message, views.name_too_short())
        return

    stored = await state.get_data()
    code = stored.get(CODE_FIELD)
    sender = message.from_user
    if not isinstance(code, str) or sender is None:
        await state.set_state(Onboarding.code)
        await _send(message, views.invite_rejected(InviteRejection.UNKNOWN))
        return

    screen, role = await _join(
        access=data[ACCESS_KEY],
        state=state,
        code=code,
        full_name=typed,
        telegram_id=sender.id,
        username=sender.username,
    )
    # One message, unlike the button road above: nothing has to be edited here, so the
    # greeting and the keyboard of TZ 5.2 arrive together.
    await message.answer(
        screen.text,
        reply_markup=None if role is None else main_menu(role),
    )


async def choose_venue(callback: CallbackQuery, **data: Any) -> None:
    """One of the venues this person works in becomes the active one (TZ 5.1).

    The list keeps its buttons after the choice, and that is not an oversight: TZ 5.1 says
    the venue is remembered and switched again later, so a second press on the same message
    is a switch and not a stale screen.
    """
    payload: VenueSelect = data[PAYLOAD_KEY]
    identity: Identity | None = data.get(IDENTITY_KEY)
    access: AccessService = data[ACCESS_KEY]
    bot: Bot = data[BOT_KEY]

    user = None if identity is None else identity.user
    screen_at = _screen_of(callback)
    if user is None or screen_at is None:
        await callback.answer(texts.ERROR_NOT_ALLOWED, show_alert=True)
        return
    try:
        context = await access.select_venue(user.id, payload.venue_id)
    except AccessError:
        # A venue this person is not an active member of. The neutral refusal of TZ 9: the
        # presser learns nothing about which venues exist.
        await callback.answer(texts.ERROR_NOT_ALLOWED, show_alert=True)
        return

    await callback.answer()
    chat_id, _ = screen_at
    await bot.send_message(chat_id, texts.MENU_PROMPT, reply_markup=main_menu(context.role))


# --------------------------------------------------------------------------------------
# The two steps every road shares
# --------------------------------------------------------------------------------------


async def _offer_the_code(message: Message, *, code: str, **data: Any) -> None:
    """Show what the code promises, without spending it (TZ 5.1)."""
    state: FSMContext = data[STATE_KEY]
    access: AccessService = data[ACCESS_KEY]

    preview = await access.preview_invite_code(code, now=utc_now())
    if preview.rejection is not None:
        await _refuse_the_code(message, state=state, rejection=preview.rejection)
        return

    venue = (await access.venue_names([preview.venue_id])).get(preview.venue_id)
    if venue is None:
        # The code names a venue that is gone. Answered as an unknown code, so that trying
        # ids cannot tell a deleted venue from one that never existed (TZ 9).
        await _refuse_the_code(message, state=state, rejection=InviteRejection.UNKNOWN)
        return

    suggested = preview.suggested_full_name
    # `set_data`, not `update_data`: a second code replaces the first one entirely, and a
    # name left over from the previous attempt is exactly the wrong thing to keep.
    await state.set_data({CODE_FIELD: code, NAME_FIELD: suggested or ""})
    if suggested:
        await state.set_state(Onboarding.confirm)
        screen = views.invite_confirmation(
            venue=venue,
            full_name=suggested,
            position=preview.position,
        )
    else:
        # Decision B8 leaves the name optional on the code. There is nothing to confirm, so
        # the greeting and the question are one screen and the answer is typed.
        await state.set_state(Onboarding.name)
        screen = views.name_prompt(venue=venue)
    await _send(message, screen)


async def _refuse_the_code(
    message: Message,
    *,
    state: FSMContext,
    rejection: InviteRejection,
) -> None:
    """Name what was wrong and wait for another try — TZ 5.1 knows no other way in."""
    await state.set_state(Onboarding.code)
    await _send(message, views.invite_rejected(rejection))


async def _join(
    *,
    access: AccessService,
    state: FSMContext,
    code: str,
    full_name: str,
    telegram_id: int,
    username: str | None,
) -> tuple[Screen, MemberRole | None]:
    """Spend the code, and answer with the screen and the role the menu is drawn from.

    The role comes back with the screen rather than being looked up afterwards: the
    membership was created inside this call, and asking `resolve()` about it a line later
    would be a second read of a fact we are already holding.
    """
    activation = await access.activate_invite_code(
        code,
        telegram_id=telegram_id,
        full_name=full_name,
        now=utc_now(),
        username=username,
    )
    if activation.rejection is not None:
        # The code expired, was revoked or was spent between the preview and the press.
        await state.set_state(Onboarding.code)
        return views.invite_rejected(activation.rejection), None

    member, venue_id, user = activation.member, activation.venue_id, activation.user
    if member is None or venue_id is None:
        # `InviteActivation` fills both whenever it activated. Answered rather than raised:
        # the person is in no worse a place for being told to ask for another code.
        await state.set_state(Onboarding.code)
        return views.invite_rejected(InviteRejection.UNKNOWN), None

    await state.clear()
    venue = (await access.venue_names([venue_id])).get(venue_id)
    if venue is None:
        # Unreachable while the activation above read the code through that venue's own
        # repository; the menu is what matters here, so it is not worth failing over.
        return Screen(text=texts.MENU_PROMPT), member.role
    return (
        views.activated(
            full_name=full_name if user is None else user.full_name,
            venue=venue,
        ),
        member.role,
    )


async def _ask_which_venue(message: Message, **data: Any) -> None:
    """The choice of TZ 5.1, drawn from the memberships the gate already resolved."""
    access: AccessService = data[ACCESS_KEY]
    identity: Identity = data[IDENTITY_KEY]
    venue_ids = [context.venue_id for context in identity.contexts]
    names = await access.venue_names(venue_ids)
    options = [
        views.VenueOption(venue_id=venue_id, name=names[venue_id])
        for venue_id in venue_ids
        if venue_id in names
    ]
    await _send(message, views.venue_choice(options))


# --------------------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------------------


def _screen_of(callback: CallbackQuery) -> tuple[int, int] | None:
    """`(chat_id, message_id)` of the message a button was pressed on, or `None`.

    Telegram hands back an inaccessible stub for a message that is too old to edit, and both
    shapes carry the two numbers `safe_edit` needs, so the check is for the message being
    there at all.
    """
    screen = callback.message
    return None if screen is None else (screen.chat.id, screen.message_id)


async def _send(message: Message, screen: Screen) -> None:
    await message.answer(screen.text, reply_markup=screen.markup)


async def _edit(
    bot: Bot,
    callback: CallbackQuery,
    at: tuple[int, int],
    screen: Screen,
) -> None:
    """Rewrite the screen in place and close the spinner (TZ 8.2, 9)."""
    chat_id, message_id = at
    await safe_edit(
        bot,
        chat_id=chat_id,
        message_id=message_id,
        text=screen.text,
        reply_markup=screen.markup,
        answer=callback.answer,
    )


def router() -> Router:
    """The screens of TZ 5.1 (see the module docstring).

    The order is the design. `by_code` sits above `named` so that a fresh code pasted into
    the middle of the name question starts the road again with the new code instead of being
    written down as somebody's surname; a name that parses as `<digits>-<code letters>` is
    not a name.
    """
    instance = Router(name=ROUTER_NAME)
    instance.message.register(start, CommandStart())
    instance.message.register(by_code, CarriesInviteCode())
    instance.message.register(by_code, StateFilter(Onboarding.code))
    instance.message.register(named, StateFilter(Onboarding.name))
    instance.callback_query.register(confirm, InviteConfirm.filter())
    instance.callback_query.register(choose_venue, VenueSelect.filter())
    return instance


__all__ = [
    "CODE_FIELD",
    "MIN_FULL_NAME_LENGTH",
    "NAME_FIELD",
    "ROUTER_NAME",
    "CarriesInviteCode",
    "by_code",
    "choose_venue",
    "confirm",
    "named",
    "router",
    "start",
]
