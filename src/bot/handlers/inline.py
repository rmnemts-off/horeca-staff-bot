"""Inline search: the list that appears while a name is being typed (TZ 5.5, part IV).

**This is the only autocomplete Telegram has, and it is worth being clear about why.** A bot
is not told anything about a message until it is sent — there is no "user is typing the
third letter" update — so a list that refreshes inside an ordinary dialogue cannot exist. An
*inline* query is the one exception: while the input field starts with the bot's username,
every keystroke reaches the bot and the answer is drawn above the keyboard. The button that
puts that username into the field for you (`switch_inline_query_current_chat`, drawn by
`src/bot/keyboards/recipes.py`) is what makes that reachable without teaching anybody a
syntax.

Four properties of this module are requirements rather than choices; three of them live in
`src/bot/inline.py` because every refusal has to hold them too — `is_personal=True`, which is
the difference between a per-user answer and Telegram serving venue A's recipe to venue B out
of its own cache, and `cache_time=0`, which is TZ 5.5's promise that an edited recipe is
immediately available. The fourth is here:

**Only the chat with the bot** (open question 12 of the stage 1 spec, answered by the
customer on 15.08.2026).
What an inline result sends goes into whatever chat it was typed in, and a recipe is the
venue's asset — so a query from a group, a channel or somebody else's dialogue is answered
with nothing plus the button that leads into the bot's own chat. `chat_type` is Telegram's
own word for where the query came from, and `sender` is its name for "the private chat with
the person who sent it", which for a bot is exactly its own dialogue.

**A stranger gets an empty list, not an explanation** (TZ 5.1). The gate refuses them before
this handler is reached; what is left here is the person with no venue context — somebody
mid-invite, or between two venues — and the answer is the same emptiness.

Everything else is the ordinary shape of TZ 3.2: take the update, call one service, render.
The service is `RecipeService.search_cards`, which is the search plus the compositions in two
queries — a card per row would be ten round trips for every letter typed.
"""

from __future__ import annotations

from typing import Any, Final, Protocol

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.types import InlineQuery

from src.bot.inline import NO_MORE_RESULTS, answer, answer_nothing
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.services import SERVICES_KEY
from src.bot.views import inline as views
from src.services.access import AccessContext
from src.services.recipes import CardPage

#: Name of this router; read in a traceback and in the assembly test.
ROUTER_NAME: Final = "inline"

#: Where an inline query may be typed (open question 12). Telegram spells the bot's own
#: dialogue `sender` — "the private chat with the sender of the query" — and everything else
#: is somewhere the recipe would leave the venue. A query with no `chat_type` at all is
#: treated as "somewhere else": the field is optional in the Bot API, and the safe reading of
#: an unknown chat is not the one that publishes a recipe into it.
OWN_CHAT: Final[frozenset[str]] = frozenset({ChatType.SENDER.value})


class Catalogue(Protocol):
    """The one call this screen makes (TZ 5.5)."""

    async def search_cards(self, query: str, *, offset: int, limit: int = ...) -> CardPage: ...


class CatalogueServices(Protocol):
    """`data["services"]`, narrowed to what the inline answer reads."""

    @property
    def recipes(self) -> Catalogue: ...


def is_own_chat(query: InlineQuery) -> bool:
    """Whether this query was typed in the chat with the bot; see the module docstring."""
    return query.chat_type in OWN_CHAT


def page_offset(raw: str) -> int:
    """The `offset` Telegram echoes back, which is whatever we last put there.

    It is a *string* in the Bot API and it comes back from the client, so it is parsed and
    never trusted: anything that is not a number is the first page. `int()` would accept a
    leading sign and a page cannot start at minus ten.
    """
    return int(raw) if raw.isdigit() else 0


async def search(query: InlineQuery, **data: Any) -> None:
    """A name being typed, answered with the drinks it matches (TZ 5.5)."""
    actor = data.get(ACTOR_KEY)
    services: CatalogueServices | None = data.get(SERVICES_KEY)
    if not isinstance(actor, AccessContext) or services is None:
        # No venue behind this person yet: an invite being typed, or a choice between two
        # venues not made. There is nothing venue-scoped to search (TZ 5.1).
        await answer_nothing(query)
        return
    if not is_own_chat(query):
        await answer_nothing(query, button=views.elsewhere_button())
        return

    page = await services.recipes.search_cards(query.query, offset=page_offset(query.offset))
    await answer(query, views.results(page.cards), next_offset=_next_offset(page))


def _next_offset(page: CardPage) -> str:
    """Where the client should continue when the list is scrolled to its end (TZ 5.5)."""
    following = page.next_offset
    return NO_MORE_RESULTS if following is None else str(following)


def router() -> Router:
    """The inline half of TZ 5.5.

    One handler and no filter: an inline query is the whole of what this router is
    registered for, and every branch of the decision — no venue, the wrong chat, nothing
    found — is an *answer*, because a query left unanswered loads on the client until it
    gives up.
    """
    instance = Router(name=ROUTER_NAME)
    instance.inline_query.register(search)
    return instance


__all__ = ["OWN_CHAT", "ROUTER_NAME", "is_own_chat", "page_offset", "router", "search"]
