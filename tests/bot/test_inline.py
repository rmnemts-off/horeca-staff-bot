"""Inline search: the list above the keyboard (TZ 5.5; part IV of the stage 1 spec).

**One test here is a security test and not a UX one.** `is_personal=True` is what keeps
Telegram from caching an answer *by query text, for everybody*: without it a bartender of
one venue typing a drink's name is served another venue's recipe out of Telegram's cache,
with no query reaching our database at all. No repository test can see that — the leak goes
around the predicate rather than through it — so it is asserted here, on the arguments of
the call, and on every branch that answers a query rather than only on the happy one.

The handler is fed through a real `Dispatcher`, like the rest of this package: what is being
checked is that an inline update reaches the handler and that every road out of it *answers*
the query. An unanswered inline query is not a silent failure, it is a client that loads
until it gives up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerInlineQuery
from aiogram.types import InlineQuery, Update
from aiogram.types import User as TelegramUser
from src.bot import texts
from src.bot.handlers import inline as handlers
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.services import SERVICES_KEY
from src.bot.views import inline as views
from src.bot.views import recipes as catalogue
from src.services.recipes import MAX_SEARCH_RESULTS, CardPage, RecipeCard

from tests.bot.test_middlewares import STAFF, STAFF_TELEGRAM_ID, make_bot, session_of
from tests.bot.test_recipes_screen import make_card, make_line

#: Telegram's own word for the private chat with the person who sent the query — which, for
#: a bot, is its own dialogue. Everything else is somewhere a recipe must not be sent
#: (open question 12 of the stage 1 spec).
OWN_CHAT = "sender"


def make_query(
    text: str = "mojito",
    *,
    offset: str = "",
    chat_type: str | None = OWN_CHAT,
) -> Update:
    return Update(
        update_id=1,
        inline_query=InlineQuery(
            id="q-1",
            from_user=TelegramUser(id=STAFF_TELEGRAM_ID, is_bot=False, first_name="X"),
            query=text,
            offset=offset,
            chat_type=chat_type,
        ),
    )


class FakeRecipes:
    """`RecipeService.search_cards`, with the paging arithmetic the real one has."""

    def __init__(self, cards: list[RecipeCard] | None = None) -> None:
        self.cards = cards if cards is not None else []
        self.asked: list[tuple[str, int]] = []

    async def search_cards(
        self,
        query: str,
        *,
        offset: int = 0,
        limit: int = MAX_SEARCH_RESULTS,
    ) -> CardPage:
        cleaned = " ".join(query.split())
        self.asked.append((cleaned, offset))
        window = min(max(limit, 1), MAX_SEARCH_RESULTS)
        if not cleaned:
            return CardPage(query="", cards=(), offset=offset, limit=window, has_next=False)
        visible = self.cards[offset : offset + window]
        return CardPage(
            query=cleaned,
            cards=tuple(visible),
            offset=offset,
            limit=window,
            has_next=len(self.cards) > offset + window,
        )


@dataclass
class Bundle:
    recipes: FakeRecipes = field(default_factory=FakeRecipes)


@dataclass
class Stand:
    dispatcher: Dispatcher
    bot: Bot
    services: Bundle

    async def feed(self, update: Update, **extra: Any) -> None:
        await self.dispatcher.feed_update(
            self.bot,
            update,
            **{ACTOR_KEY: STAFF, SERVICES_KEY: self.services, **extra},
        )

    @property
    def answer(self) -> AnswerInlineQuery:
        """The one answer this update produced — and there must be exactly one."""
        answers = [
            call for call in session_of(self.bot).calls if isinstance(call, AnswerInlineQuery)
        ]
        assert len(answers) == 1, f"expected one inline answer, got {len(answers)}"
        return answers[0]


def build_stand(cards: list[RecipeCard] | None = None) -> Stand:
    bot = make_bot()
    dispatcher = Dispatcher()
    dispatcher.include_router(handlers.router())
    return Stand(dispatcher=dispatcher, bot=bot, services=Bundle(FakeRecipes(cards)))


def card_named(name: str, recipe_id: int = 1, **kwargs: Any) -> RecipeCard:
    return make_card(name=name, recipe_id=recipe_id, **kwargs)


# --------------------------------------------------------------------------------------
# The rules every answer holds (TZ 3.3, 5.5)
# --------------------------------------------------------------------------------------


async def test_the_answer_is_personal_and_never_cached() -> None:
    """The one test of this file that is about isolation and not about a screen.

    `is_personal=False` (the Bot API default) lets Telegram serve this venue's recipe to
    anybody who types the same word, from its own cache, without our database being asked.
    `cache_time` above zero is the window in which TZ 5.5's «immediately available to the
    whole shift» is not true.

    Verified by mutation: dropping either argument from `src/bot/inline.py::answer` makes
    this test — and the refusal test below — fail.
    """
    stand = build_stand([card_named("Mojito")])

    await stand.feed(make_query())

    assert stand.answer.is_personal is True
    assert stand.answer.cache_time == 0


@pytest.mark.parametrize(
    ("chat_type", "extra"),
    [
        pytest.param("group", {}, id="another chat"),
        pytest.param(OWN_CHAT, {SERVICES_KEY: None}, id="no venue"),
    ],
)
async def test_a_refusal_holds_the_same_two_rules(
    chat_type: str,
    extra: dict[str, Any],
) -> None:
    """Each branch that answers with nothing still answers under both rules.

    The refusals are the easy place to forget them, and an empty answer cached for everybody
    is how a venue that has just filled its catalogue keeps seeing an empty list.
    """
    stand = build_stand([card_named("Mojito")])

    await stand.feed(make_query(chat_type=chat_type), **extra)

    assert stand.answer.results == []
    assert stand.answer.is_personal is True
    assert stand.answer.cache_time == 0


# --------------------------------------------------------------------------------------
# What the list shows (TZ 5.5)
# --------------------------------------------------------------------------------------


async def test_a_typed_name_comes_back_as_the_drinks_that_match() -> None:
    stand = build_stand([card_named("Mojito", 7), card_named("Mojito Royal", 8)])

    await stand.feed(make_query("moji"))

    assert stand.services.recipes.asked == [("moji", 0)]
    assert [result.id for result in stand.answer.results] == ["7", "8"]
    assert [result.title for result in stand.answer.results] == ["Mojito", "Mojito Royal"]


async def test_a_row_sends_the_very_card_the_section_shows() -> None:
    """The point of the feature: the recipe lands in the chat, not a link to it.

    Compared against `views.recipes.card` rather than against a string, so the day the card
    gains a line the two cannot come apart (`src/bot/views/inline.py`).
    """
    card = card_named("Mojito", 7, ingredients=(make_line("White rum"),))
    stand = build_stand([card])

    await stand.feed(make_query())

    (result,) = stand.answer.results
    assert result.input_message_content.message_text == catalogue.card(card).text


async def test_a_row_carries_its_category_and_composition() -> None:
    """Decision D6: two drinks may share a name, so the second line is not decoration."""
    card = card_named(
        "Americano",
        7,
        category="coffee",
        ingredients=(make_line("Espresso"), make_line("Water")),
    )
    stand = build_stand([card])

    await stand.feed(make_query("americano"))

    (result,) = stand.answer.results
    assert result.description == texts.TTK_INLINE_HINT_TEMPLATE.format(
        category="coffee",
        composition=texts.TTK_INLINE_HINT_SEPARATOR.join(["Espresso", "Water"]),
    )


async def test_a_card_with_no_composition_is_described_by_its_category_alone() -> None:
    """TZ 8.1: a card with nothing but a name is valid, and its row must still read."""
    stand = build_stand([card_named("Mojito", 7, category="classics")])

    await stand.feed(make_query())

    (result,) = stand.answer.results
    assert result.description == "classics"


def test_a_long_composition_is_cut_rather_than_wrapped() -> None:
    """A dropdown row is one short line on a phone (view-level, no bot needed)."""
    card = card_named(
        "Punch",
        7,
        ingredients=tuple(make_line(f"ingredient {index}") for index in range(6)),
    )

    (result,) = views.results([card])

    assert result.description is not None
    assert result.description.endswith(texts.TTK_INLINE_HINT_MORE_MARK)
    assert "ingredient 3" not in result.description


# --------------------------------------------------------------------------------------
# Paging (TZ 5.5)
# --------------------------------------------------------------------------------------


async def test_the_answer_says_where_the_next_page_starts() -> None:
    stand = build_stand([card_named(f"Drink {index}", index) for index in range(1, 13)])

    await stand.feed(make_query())

    assert len(stand.answer.results) == MAX_SEARCH_RESULTS
    assert stand.answer.next_offset == str(MAX_SEARCH_RESULTS)


async def test_the_last_page_says_there_is_no_next_one() -> None:
    """An empty `next_offset` is Telegram's own «that was all»; a missing one is not."""
    stand = build_stand([card_named("Mojito", 7)])

    await stand.feed(make_query())

    assert stand.answer.next_offset == ""


async def test_the_offset_the_client_sends_back_is_parsed_and_not_trusted() -> None:
    stand = build_stand([card_named(f"Drink {index}", index) for index in range(1, 13)])

    await stand.feed(make_query(offset="10"))
    assert stand.services.recipes.asked == [("mojito", 10)]

    await stand.feed(make_query(offset="not a number"))
    assert stand.services.recipes.asked[-1] == ("mojito", 0)


@pytest.mark.parametrize("raw", ["", "-5", "12x", "1 0"])
def test_only_a_plain_number_is_an_offset(raw: str) -> None:
    assert handlers.page_offset(raw) == 0


# --------------------------------------------------------------------------------------
# Where it may be used (open question 12 of the stage 1 spec)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel", "private", None])
async def test_a_query_typed_outside_the_bots_chat_finds_nothing(chat_type: str | None) -> None:
    """A recipe is the venue's asset, and an inline result goes into the chat it was typed
    in. The customer's answer on 15.08.2026 was «only in the chat with the bot».

    `None` is in the list on purpose: `chat_type` is optional in the Bot API, and the safe
    reading of a chat we cannot name is not the one that publishes a recipe into it.
    """
    stand = build_stand([card_named("Mojito")])

    await stand.feed(make_query(chat_type=chat_type))

    assert stand.answer.results == []
    assert stand.services.recipes.asked == [], "the database is not even asked"


async def test_the_wrong_chat_is_told_where_the_search_lives() -> None:
    """An empty dropdown with no explanation reads as «there is no such drink» (TZ 8.1)."""
    stand = build_stand([card_named("Mojito")])

    await stand.feed(make_query(chat_type="group"))

    button = stand.answer.button
    assert button is not None
    assert button.text == texts.TTK_INLINE_ELSEWHERE_BUTTON
    assert button.start_parameter == views.OPEN_BOT_PARAMETER


async def test_the_bots_own_chat_is_the_one_that_works() -> None:
    stand = build_stand([card_named("Mojito")])

    await stand.feed(make_query(chat_type=OWN_CHAT))

    assert len(stand.answer.results) == 1
    assert stand.answer.button is None


# --------------------------------------------------------------------------------------
# Who may use it (TZ 5.1)
# --------------------------------------------------------------------------------------


async def test_somebody_with_no_venue_is_told_nothing_at_all() -> None:
    """The gate lets through an invite being typed and a choice between two venues.

    Neither has a venue to search, and TZ 5.1 gives them no hint of what the bot can do —
    so the answer is empty *and* carries no button.
    """
    stand = build_stand([card_named("Mojito")])

    await stand.feed(make_query(), **{ACTOR_KEY: None, SERVICES_KEY: None})

    assert stand.answer.results == []
    assert stand.answer.button is None
    assert stand.services.recipes.asked == []
