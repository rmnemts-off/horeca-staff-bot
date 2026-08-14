"""The main menu, the shift screen and the schedule (TZ 5.2, 5.3; plan task 23).

This module owns three sections' worth of routing and not one line of schedule logic. It
takes an update, asks :class:`~src.services.shifts.ShiftService` a question, and hands the
answer to a view (TZ 3.2).

**What "my shift" means, and why it is the service that decides.** TZ 5.3 asks for the
current shift and only then for the next one, and the difference between the two is a
window rather than a date: a bartender working 14:00-00:00 who opens the bot at 00:30 is
*in* that shift, on a calendar day that has already ended.
:meth:`~src.services.shifts.ShiftService.nearest_shift` answers exactly that question —
:meth:`current_shift` looks a day back and matches by window, :meth:`next_shift` is the
fallback — so this module never compares a date with anything.

**Now arrives from the one clock of the project** (``src/services/timezones.utc_now``,
decision D12) and is passed down as an argument. Nothing here calls ``datetime.now()``,
and neither does the view: that is what lets the 00:30 case be a snapshot test with a
pinned clock instead of a comment hoping somebody checks it by hand.

**A section is owned end to end** (`src/bot/handlers/__init__.py`). Each of the two
sections is reached three ways — the caption on the reply keyboard, an inline
:class:`~src.bot.callbacks.OpenSection`, and :class:`~src.bot.callbacks.Nav` coming back
from a screen one level deeper — and all three land on the same function here. The three
navigation buttons that lead to the *main* menu (`HOME`, `CANCEL`, and `BACK` with no
section) belong here too, because the main menu is nobody else's screen.

**Getting back into the checklist** (TZ 5.4: the message was swiped away, or simply
scrolled past). The shift screen offers the button, and the run it names comes from
``ChecklistService.run_for_shift`` — a read, never a create. Opening this screen an hour
before the shift must not bring the checklist forward: the bot decides when it arrives
(principle 1.4#2), and the moment is `opening_checklist_lead_minutes` before the start.
So a shift with no run yet simply has no button, which is also the state of every shift
whose template is still empty (decision B1).
"""

from __future__ import annotations

from typing import Any, Final

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot import texts
from src.bot.callbacks import MenuAction, Nav, NavTarget, OpenSection
from src.bot.keyboards.menu import action_for
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.menu import STATE_KEY
from src.bot.middlewares.services import SERVICES_KEY, VenueServices
from src.bot.safe_edit import safe_edit
from src.bot.views import Screen
from src.bot.views.shifts import schedule_screen, shift_screen
from src.db.models import ChecklistType
from src.services.access import AccessContext
from src.services.shifts import ShiftView
from src.services.timezones import utc_now

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "menu"

#: aiogram's own key for the `Bot` of the update. Taken from the context rather than off
#: the event: `event.bot` is bound by the dispatcher and raises when it is not, and the
#: screen a scheduled job redraws has no event to read it from at all.
BOT_KEY: Final = "bot"


# --------------------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------------------


class MenuCaption(BaseFilter):
    """A press on one caption of the reply keyboard (TZ 5.2).

    Goes through :func:`~src.bot.keyboards.menu.action_for` rather than comparing with a
    text constant, so the registry of `keyboards/menu.py` stays the one place that decides
    what the menu draws (decision B7). A section whose `is_available` flag is off is not in
    that lookup, so it is not answered here either — which is the same silence a wizard
    waiting for a line needs from a caption nobody can see.
    """

    def __init__(self, action: MenuAction) -> None:
        self.action = action

    async def __call__(self, message: Message) -> bool:
        return action_for(message.text or "") is self.action


# --------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------


def menu_screen() -> Screen:
    """The main menu: a line of text and no inline keyboard at all.

    The menu itself is the *reply* keyboard at the bottom of the chat (TZ 5.2), which is
    persistent and was installed with `/start` — it is not part of any message, so coming
    home is a matter of taking the screen that is open out of the way rather than of
    drawing a new one. Editing in place instead of sending a fresh message is TZ 8.2: one
    screen, not forty.

    It lives here rather than in `src/bot/views/` for the reason it is two lines long:
    there is no markup to build, no service view to render and nothing a second caller
    could need.
    """
    return Screen(text=texts.MENU_PROMPT)


# --------------------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------------------


async def open_shift(event: Message | CallbackQuery, **data: Any) -> None:
    """The shift screen (TZ 5.3): the shift being worked, else the nearest one.

    Two service calls and no arithmetic: the shift, then who else is on its date. The
    roster is asked for the *shift's* date and not for today's — at 00:30 those are two
    different days, and asking about today would show the night shift with an empty team.
    """
    actor: AccessContext | None = data.get(ACTOR_KEY)
    services: VenueServices | None = data.get(SERVICES_KEY)
    if actor is None or services is None:
        await _no_venue_yet(event)
        return

    moment = utc_now()
    shift: ShiftView | None = await services.shifts.nearest_shift(actor, now=moment)
    roster: tuple[ShiftView, ...] = ()
    run_id: int | None = None
    if shift is not None:
        roster = await services.shifts.roster(actor, shift.shift_date)
        run_id = await _opening_run_of(services, shift)
    await _draw(
        event,
        shift_screen(shift, now=moment, roster=roster, checklist_run_id=run_id),
        data=data,
    )


async def _opening_run_of(services: VenueServices, shift: ShiftView) -> int | None:
    """The opening checklist already sent for this shift, if there is one (TZ 5.4).

    A finished run keeps its button: TZ 5.4 lets the employee look at what they ticked, and
    the checklist screen refuses to change a completed run on its own.
    """
    run = await services.checklists.run_for_shift(
        shift_id=shift.shift_id,
        checklist_type=ChecklistType.OPENING,
    )
    return None if run is None else run.run_id


async def open_schedule(event: Message | CallbackQuery, **data: Any) -> None:
    """The schedule (TZ 5.3): the employee's own shifts, two weeks ahead."""
    actor: AccessContext | None = data.get(ACTOR_KEY)
    services: VenueServices | None = data.get(SERVICES_KEY)
    if actor is None or services is None:
        await _no_venue_yet(event)
        return

    shifts = await services.shifts.fortnight(actor, now=utc_now())
    await _draw(event, schedule_screen(shifts), data=data)


async def go_home(event: CallbackQuery, **data: Any) -> None:
    """The home button, and back from a screen that sits one step below the menu (TZ 5.2).

    The scenario, if there is one, is dropped on the way. Every back button this project
    draws carries no section (`keyboards/menu.back_button`), so a press that arrives here
    is a press that lands on the main menu — and a wizard left half open behind it would
    eat the next line typed, which is the failure `src/bot/middlewares/menu.py` exists to
    prevent. Silently: nothing was being saved, so there is nothing to report.
    """
    await _leave_any_scenario(data)
    await _draw(event, menu_screen(), data=data)


async def cancel(event: CallbackQuery, **data: Any) -> None:
    """Cancel (TZ 8.2): drop the scenario, say so, and go back to the menu.

    The wording is a toast on the button rather than a line in the chat: the answer to
    "what happened" has to arrive within the second TZ 9 allows, and the screen underneath
    is already being replaced by the menu.
    """
    await _leave_any_scenario(data)
    await _draw(event, menu_screen(), data=data, notice=texts.CANCELLED)


# --------------------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------------------


async def _draw(
    event: Message | CallbackQuery,
    screen: Screen,
    *,
    data: dict[str, Any],
    notice: str | None = None,
) -> None:
    """Put a screen in front of the person, whichever road they arrived by.

    A caption on the reply keyboard is a new message and gets one; a button press edits the
    message it was pressed on (TZ 8.2), through `safe_edit`, which also closes the spinner
    on every branch including the one that raises (TZ 9).
    """
    if isinstance(event, CallbackQuery):
        opened = event.message
        if not isinstance(opened, Message):
            # Older than 48 hours: Telegram sends an `InaccessibleMessage`, which cannot be
            # edited and carries no text to compare against.
            await event.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
            return
        await safe_edit(
            data[BOT_KEY],
            chat_id=opened.chat.id,
            message_id=opened.message_id,
            text=screen.text,
            reply_markup=screen.markup,
            answer=event.answer,
            notice=notice,
        )
        return
    await event.answer(screen.text, reply_markup=screen.markup)


async def _leave_any_scenario(data: dict[str, Any]) -> None:
    """Clear the FSM if one is open; a no-op otherwise (TZ 8.2)."""
    state = data.get(STATE_KEY)
    if isinstance(state, FSMContext):
        await state.clear()


async def _no_venue_yet(event: Message | CallbackQuery) -> None:
    """Somebody the gate let through without a membership (TZ 5.1, decision A3).

    They have no venue, so they have no schedule; showing them the empty state would be
    telling them about a bar they do not work in. The screens of TZ 5.1 — the venue choice
    and the create-a-venue wizard — are `handlers/onboarding.py`'s, and this is not their
    road there. A button press still gets its answer, because TZ 9 gives it a second.
    """
    if isinstance(event, CallbackQuery):
        await event.answer(texts.ERROR_NOT_ALLOWED, show_alert=True)


# --------------------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------------------


def router() -> Router:
    """The screens of this block (see the module docstring).

    Registration is by factory and by caption, never by a raw prefix: the payload has
    already been parsed and checked against the venue by the resolver.
    """
    instance = Router(name=ROUTER_NAME)

    # TZ 5.3, the shift screen — the caption, the inline button that means the same, and
    # the way back from the checklist that hangs off this screen.
    instance.message.register(open_shift, MenuCaption(MenuAction.MY_SHIFT))
    instance.callback_query.register(
        open_shift,
        OpenSection.filter(F.section == MenuAction.MY_SHIFT),
    )
    instance.callback_query.register(
        open_shift,
        Nav.filter((F.target == NavTarget.BACK) & (F.section == MenuAction.MY_SHIFT)),
    )

    # TZ 5.3, the schedule.
    instance.message.register(open_schedule, MenuCaption(MenuAction.SCHEDULE))
    instance.callback_query.register(
        open_schedule,
        OpenSection.filter(F.section == MenuAction.SCHEDULE),
    )
    instance.callback_query.register(
        open_schedule,
        Nav.filter((F.target == NavTarget.BACK) & (F.section == MenuAction.SCHEDULE)),
    )

    # TZ 5.2 and 8.2: the three buttons that lead to the main menu itself. `BACK` with no
    # section is last of the three registrations on purpose — the two above claim their
    # sections first, so the fallthrough is the one that means "one step from the menu".
    instance.callback_query.register(go_home, Nav.filter(F.target == NavTarget.HOME))
    instance.callback_query.register(cancel, Nav.filter(F.target == NavTarget.CANCEL))
    instance.callback_query.register(
        go_home,
        Nav.filter((F.target == NavTarget.BACK) & (F.section.is_(None))),
    )
    return instance


__all__ = [
    "BOT_KEY",
    "ROUTER_NAME",
    "MenuCaption",
    "cancel",
    "go_home",
    "menu_screen",
    "open_schedule",
    "open_shift",
    "router",
]
