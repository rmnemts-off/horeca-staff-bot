"""Recipe search and the card (TZ 5.5; plan task 25).

This module owns the catalogue section (`MENU_CATALOGUE_BUTTON`) end to end, as
`src/bot/handlers/__init__.py` requires: the caption on the reply keyboard, the
`OpenSection(CATALOGUE)` button that means the same thing, and `Nav(BACK, CATALOGUE)`
returning to it. Six screens, and every one of them is a view called with what a service
returned — the handler routes, the service decides, the view writes.

**The query lives in the FSM state and never in a button.** Free text in `callback_data` is
refused by the scheme (rule 2 of `src/bot/callbacks.py`), so `RecipePage` carries an offset
and `RecipeMissing` carries nothing at all; both read what was searched for out of
`RecipeSearch`. That is also what makes pagination survive the screen being redrawn: the
page is rebuilt from the state, not from the message it was drawn on.

**A search is re-run, never cached.** The pager asks the service again with a new offset,
because a page held in the state would be a page that stopped matching the database the
moment a manager added a drink — and TZ 5.5 measures this screen in seconds, not in
queries.

Two things this section cannot do yet, and neither is a decision of this module:

* **browsing by category.** `TTK_SEARCH_PROMPT` offers it and `RecipeService.browse` is
  written, but a button naming a category needs a callback factory carrying an index into a
  page held in the state — the shape `TimezoneChoice` has — and the scheme declares none.
  Rather than draw a caption that answers nothing (TZ 8.1), the section offers search only;
* **the volume switch and the jump to a prep card.** TZ 5.5 describes both, the preps
  section is not part of stage 0, and a composition line pointing at a prep is therefore
  printed rather than linked.

**The card is HTML** (`TTK_CARD_NAME_TEMPLATE` is `<b>{name}</b>`) and the views quote what
the venue typed, but nothing in this project sets a default parse mode yet — see the report
of task 25. Until it does, the tags arrive as characters.
"""

from __future__ import annotations

from typing import Any, Final

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot import texts
from src.bot.callbacks import (
    MenuAction,
    Nav,
    NavTarget,
    OpenSection,
    RecipeMissing,
    RecipePage,
    RecipeShow,
)
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.services import SERVICES_KEY, VenueServices
from src.bot.safe_edit import safe_edit
from src.bot.states import RecipeSearch
from src.bot.views import Screen
from src.bot.views import recipes as views
from src.services.access import AccessContext

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "recipes"

#: FSM keys of the search. The query is here precisely because it may not be in a button.
QUERY_KEY: Final = "query"
OFFSET_KEY: Final = "offset"


# --------------------------------------------------------------------------------------
# Entering the section (TZ 5.2, 5.5)
# --------------------------------------------------------------------------------------


async def open_from_menu(message: Message, state: FSMContext, **data: Any) -> None:
    """The catalogue caption on the reply keyboard: invite a search, or say there is none."""
    services = _services(data)
    if services is None:
        return
    await _await_query(state)
    screen = await _section(services)
    await message.answer(screen.text, reply_markup=screen.markup)


async def open_from_button(
    callback: CallbackQuery,
    state: FSMContext,
    **data: Any,
) -> None:
    """The same section, opened by an inline `OpenSection` (TZ 5.2).

    A new message rather than an edit: this button is also the "yes, interrupt" of the
    question `src/bot/middlewares/menu.py` asks, and that question has just been deleted —
    there is no screen left to rewrite.
    """
    services = _services(data)
    await callback.answer()
    if services is None or not isinstance(callback.message, Message):
        return
    await _await_query(state)
    screen = await _section(services)
    await callback.message.answer(screen.text, reply_markup=screen.markup)


# --------------------------------------------------------------------------------------
# Searching (TZ 5.5)
# --------------------------------------------------------------------------------------


async def search(message: Message, state: FSMContext, **data: Any) -> None:
    """A line typed into the section is a search; the next line is the next search."""
    services = _services(data)
    if services is None:
        return
    page = await services.recipes.search(message.text or "")
    await state.set_state(RecipeSearch.results)
    await state.set_data({QUERY_KEY: page.query, OFFSET_KEY: page.offset})
    screen = views.results(page)
    await message.answer(screen.text, reply_markup=screen.markup)


async def turn_page(
    callback: CallbackQuery,
    callback_payload: RecipePage,
    state: FSMContext,
    bot: Bot,
    **data: Any,
) -> None:
    """The pager: the same query, a different window (TZ 5.5)."""
    await _redraw(callback, state, bot, data, offset=callback_payload.offset)


async def back_to_section(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    **data: Any,
) -> None:
    """The back button of a card or a page: the list the person came from.

    Rebuilt from the state, so returning from a card lands on the page that was open rather
    than at the top of the section — the "wrong one of the ten" case is the whole reason
    this button is worth a screen. A state that has expired leaves no query, and the view
    answers that with the invitation to search, which is what the section is anyway.
    """
    await _redraw(callback, state, bot, data)


async def report_missing(
    callback: CallbackQuery,
    state: FSMContext,
    **data: Any,
) -> None:
    """TZ 5.5: the button that says the recipe is not there.

    The handler tells nobody anything: the service hands the alert to
    `NotificationService`, which is the one road a message to another person travels (TZ 6).
    What comes back here is the toast for the person who pressed.
    """
    services = _services(data)
    actor = data.get(ACTOR_KEY)
    if services is None or not isinstance(actor, AccessContext):
        return
    stored = await state.get_data()
    alert = await services.recipes.report_missing(
        query=str(stored.get(QUERY_KEY, "")),
        reported_by=actor.user_id,
    )
    if alert is None:
        # Nothing was searched for — the screen outlived the state that gave it meaning.
        await callback.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
        return
    await callback.answer(texts.TTK_REPORTED)


# --------------------------------------------------------------------------------------
# The card (TZ 5.5)
# --------------------------------------------------------------------------------------


async def show(
    callback: CallbackQuery,
    callback_payload: RecipeShow,
    bot: Bot,
    **data: Any,
) -> None:
    """One drink, in place of the list it was chosen from.

    The resolver has already fetched the row and proved it is this venue's or the shared
    library's (TZ 3.3, 9); the service is asked again because a card is the row *and* its
    composition, and assembling that is not a handler's job.
    """
    services = _services(data)
    if services is None:
        return
    card = await services.recipes.card(callback_payload.recipe_id)
    if card is None:
        # Deleted between the list being drawn and the button being pressed.
        await callback.answer(texts.ERROR_GONE, show_alert=True)
        return
    await _draw(callback, bot, views.card(card))


# --------------------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------------------


def _services(data: dict[str, Any]) -> VenueServices | None:
    """The venue's services, or `None` for an update with no venue behind it (TZ 5.1).

    `None` is not a hypothetical: the gate lets through a person who has no membership yet
    — an invite being typed, somebody who works in two venues and has not chosen — and
    nothing venue-scoped exists for them (`src/bot/middlewares/venue.py`). They get silence
    rather than a screen of a venue they do not belong to.

    A read and not an `isinstance`: what the middleware puts here is one venue's bundle, and
    a test of these screens stands a fake with the four calls this module makes in its
    place rather than a database.
    """
    services: VenueServices | None = data.get(SERVICES_KEY)
    return services


async def _await_query(state: FSMContext) -> None:
    """Open the section on a clean scenario: the previous search is not this one's."""
    await state.set_state(RecipeSearch.query)
    await state.set_data({})


async def _section(services: VenueServices) -> Screen:
    """The section screen. Emptiness is a property of the venue, and the view says so."""
    return views.section(await services.recipes.categories())


async def _redraw(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    data: dict[str, Any],
    *,
    offset: int | None = None,
) -> None:
    """Re-run the remembered search and rewrite the screen with the result.

    `offset` is the pager's; omitted, it is the window the state remembers — which is what
    makes the back button of a card return to the page the drink was chosen on.
    """
    services = _services(data)
    if services is None:
        return
    stored = await state.get_data()
    window = int(stored.get(OFFSET_KEY, 0)) if offset is None else offset
    page = await services.recipes.search(str(stored.get(QUERY_KEY, "")), offset=window)
    await state.set_state(RecipeSearch.results)
    await state.set_data({QUERY_KEY: page.query, OFFSET_KEY: page.offset})
    await _draw(callback, bot, views.results(page))


async def _draw(callback: CallbackQuery, bot: Bot, screen: Screen) -> None:
    """Rewrite the message the button was pressed on (TZ 8.2: one screen, not forty)."""
    pressed = callback.message
    if not isinstance(pressed, Message):
        # Older than 48 hours: Telegram no longer says what the message contained, so
        # there is nothing to edit and the press has to be answered instead (TZ 9).
        await callback.answer(texts.ERROR_OUTDATED_SCREEN, show_alert=True)
        return
    await safe_edit(
        bot,
        chat_id=pressed.chat.id,
        message_id=pressed.message_id,
        text=screen.text,
        reply_markup=screen.markup,
        answer=callback.answer,
    )


def router() -> Router:
    """The screens of TZ 5.5 (see the module docstring).

    The caption is registered before the state-filtered search so that the catalogue caption
    typed while the section is open re-opens it instead of being searched for. Ordinary
    text reaches `search` only in the two states of `RecipeSearch`: outside them this
    module is not listening, because the chat belongs to whatever screen is open.
    """
    instance = Router(name=ROUTER_NAME)
    instance.message.register(open_from_menu, F.text == texts.MENU_CATALOGUE_BUTTON)
    instance.message.register(
        search,
        StateFilter(RecipeSearch.query, RecipeSearch.results),
        F.text,
    )
    instance.callback_query.register(
        open_from_button,
        OpenSection.filter(F.section == MenuAction.CATALOGUE),
    )
    instance.callback_query.register(
        back_to_section,
        Nav.filter((F.target == NavTarget.BACK) & (F.section == MenuAction.CATALOGUE)),
    )
    instance.callback_query.register(show, RecipeShow.filter())
    instance.callback_query.register(turn_page, RecipePage.filter())
    instance.callback_query.register(report_missing, RecipeMissing.filter())
    return instance


__all__ = [
    "OFFSET_KEY",
    "QUERY_KEY",
    "ROUTER_NAME",
    "back_to_section",
    "open_from_button",
    "open_from_menu",
    "report_missing",
    "router",
    "search",
    "show",
    "turn_page",
]
