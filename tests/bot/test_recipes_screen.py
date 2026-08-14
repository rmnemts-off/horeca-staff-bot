"""The catalogue section: search, the hit list, the card (TZ 5.5; plan task 25).

Two halves, and they are tested differently on purpose.

**The views are pure functions**, so they are asserted as strings with no bot anywhere near
them (`src/bot/views/__init__.py`). That is what makes the card of a venue that has typed in
nothing but a name and a composition an ordinary `assert` rather than a screenshot — and
that card is the point of the whole exercise: TZ 5.5 draws glassware, method, ice, garnish
and instruction, and the reference book leaves every one of them empty on most classics.
A heading with nothing behind it is worse than no heading, so the absence is asserted
directly (:func:`test_a_minimal_card_carries_no_empty_headings`).

**The handlers are fed through a real `Dispatcher`**, because what is being checked is the
routing: which screen a caption opens, that a typed line becomes a search, that the pager
knows what was searched for when the button carries only a number. The recipe service is a
fake with the signatures of the real one — including the ones that refuse (`report_missing`
of a blank query returns `None`), because a fake that promises more than the service does
makes a test green about behaviour that does not exist (CLAUDE.md).

Nothing here reaches a database, and nothing here contains venue data: the names are
`Americano` twice, which is decision D6's own example and the reason a hit caption carries
its category.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, Update
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
from src.bot.handlers import recipes as handlers
from src.bot.middlewares.auth import ACTOR_KEY
from src.bot.middlewares.resolver import PAYLOAD_KEY
from src.bot.middlewares.services import SERVICES_KEY
from src.bot.states import RecipeSearch
from src.bot.views import recipes as views
from src.db.models import Unit
from src.services.recipes import (
    MAX_SEARCH_RESULTS,
    IngredientLine,
    MissingRecipeAlert,
    RecipeCard,
    RecipeHit,
    SearchPage,
    classify_amount,
)

from tests.bot.test_middlewares import (
    CHAT_ID,
    STAFF,
    STAFF_TELEGRAM_ID,
    make_bot,
    make_callback,
    make_message,
    session_of,
)

#: Every part of the card TZ 5.5 draws only when the venue filled it in. A card without them
#: must not carry their headings — the list is what turns that into one assertion.
OPTIONAL_PARTS: Final = (
    texts.TTK_CARD_GLASSWARE_TEMPLATE,
    texts.TTK_CARD_METHOD_TEMPLATE,
    texts.TTK_CARD_ICE_TEMPLATE,
    texts.TTK_CARD_GARNISH_TEMPLATE,
    texts.TTK_CARD_INSTRUCTION_TEMPLATE,
)


# --------------------------------------------------------------------------------------
# Vocabulary of these tests
# --------------------------------------------------------------------------------------


def make_line(
    name: str,
    *,
    qty: Decimal | None = None,
    unit: Unit | None = None,
    qty_text: str | None = None,
    prep_id: int | None = None,
    order_index: int = 0,
) -> IngredientLine:
    """One composition line, with the branch chosen the way the service chooses it."""
    return IngredientLine(
        name=name,
        kind=classify_amount(qty, unit, qty_text),
        qty=qty,
        unit=unit,
        qty_text=qty_text,
        product_id=None,
        prep_id=prep_id,
        order_index=order_index,
    )


def make_card(
    *,
    recipe_id: int = 7,
    name: str = "Mojito",
    category: str = "classics",
    glassware: str | None = None,
    method: str | None = None,
    ice: str | None = None,
    garnish: str | None = None,
    instruction: str | None = None,
    season_date: dt.date | None = None,
    ingredients: Sequence[IngredientLine] = (),
) -> RecipeCard:
    """A card whose defaults are the minimal one: a name, and whatever is passed in."""
    return RecipeCard(
        recipe_id=recipe_id,
        name=name,
        category=category,
        is_library=False,
        glassware=glassware,
        method=method,
        ice=ice,
        garnish=garnish,
        instruction=instruction,
        season_date=season_date,
        yield_variants=None,
        photo_file_id=None,
        ingredients=tuple(ingredients),
    )


def make_hit(recipe_id: int, name: str, category: str) -> RecipeHit:
    return RecipeHit(recipe_id=recipe_id, name=name, category=category, is_library=False)


def make_page(
    hits: Sequence[RecipeHit] = (),
    *,
    query: str = "americano",
    offset: int = 0,
    has_next: bool = False,
) -> SearchPage:
    return SearchPage(
        query=query,
        hits=tuple(hits),
        offset=offset,
        limit=MAX_SEARCH_RESULTS,
        has_next=has_next,
    )


def payloads(markup: InlineKeyboardMarkup | None) -> list[str]:
    assert markup is not None, "a screen of this section always carries navigation"
    return [str(button.callback_data) for row in markup.inline_keyboard for button in row]


def captions(markup: InlineKeyboardMarkup | None) -> list[str]:
    assert markup is not None
    return [button.text for row in markup.inline_keyboard for button in row]


def heading_of(template: str) -> str:
    """The part of a card template that is printed before the venue data, heading and all."""
    return template.split("{", 1)[0]


# --------------------------------------------------------------------------------------
# The card (TZ 5.5)
# --------------------------------------------------------------------------------------


def test_a_minimal_card_carries_no_empty_headings() -> None:
    """A name and a composition is the whole card of a venue that has just started (8.1)."""
    screen = views.card(
        make_card(ingredients=[make_line("White rum", qty=Decimal("50"), unit=Unit.ML)])
    )

    assert screen.text == "\n".join(
        (
            texts.TTK_CARD_NAME_TEMPLATE.format(name="Mojito"),
            texts.TTK_CARD_COMPOSITION_TITLE,
            texts.TTK_CARD_LINE_TEMPLATE.format(
                name="White rum",
                amount=texts.TTK_CARD_AMOUNT_TEMPLATE.format(
                    qty="50", unit=texts.unit_label(Unit.ML)
                ),
            ),
        )
    )
    for template in OPTIONAL_PARTS:
        assert heading_of(template) not in screen.text, (
            f"{template!r} was rendered for a field the venue never filled in; a card with "
            "a heading and nothing behind it is worse than a card without the word"
        )


def test_a_card_with_nothing_but_a_name_is_still_a_card() -> None:
    """The composition is optional too: an entered name is already worth a screen."""
    screen = views.card(make_card(name="Espresso"))

    assert screen.text == texts.TTK_CARD_NAME_TEMPLATE.format(name="Espresso")
    assert texts.TTK_CARD_COMPOSITION_TITLE not in screen.text


def test_a_full_card_prints_the_parts_of_tz_5_5() -> None:
    screen = views.card(
        make_card(
            glassware="highball",
            method="build",
            ice="crushed",
            garnish="mint",
            instruction="stir",
            ingredients=[make_line("Soda", qty_text="top up")],
        )
    )

    assert screen.text.splitlines() == [
        texts.TTK_CARD_NAME_TEMPLATE.format(name="Mojito"),
        texts.TTK_CARD_SEPARATOR.join(
            (
                texts.TTK_CARD_GLASSWARE_TEMPLATE.format(value="highball"),
                texts.TTK_CARD_METHOD_TEMPLATE.format(value="build"),
                texts.TTK_CARD_ICE_TEMPLATE.format(value="crushed"),
            )
        ),
        texts.TTK_CARD_COMPOSITION_TITLE,
        texts.TTK_CARD_LINE_TEMPLATE.format(name="Soda", amount="top up"),
        texts.TTK_CARD_INSTRUCTION_TEMPLATE.format(value="stir"),
        texts.TTK_CARD_GARNISH_TEMPLATE.format(value="mint"),
    ]


def test_one_missing_part_does_not_empty_the_line_it_shared() -> None:
    """Glassware, method and ice share a line; two of them still make one."""
    screen = views.card(make_card(glassware="highball", ice="crushed"))

    assert screen.text.splitlines()[1] == texts.TTK_CARD_SEPARATOR.join(
        (
            texts.TTK_CARD_GLASSWARE_TEMPLATE.format(value="highball"),
            texts.TTK_CARD_ICE_TEMPLATE.format(value="crushed"),
        )
    )
    assert heading_of(texts.TTK_CARD_METHOD_TEMPLATE) not in screen.text


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(
            make_line("White rum", qty=Decimal("50.00"), unit=Unit.ML),
            texts.TTK_CARD_LINE_TEMPLATE.format(
                name="White rum",
                amount=texts.TTK_CARD_AMOUNT_TEMPLATE.format(
                    qty="50", unit=texts.unit_label(Unit.ML)
                ),
            ),
            id="measured",
        ),
        pytest.param(
            make_line("Lime juice", qty=Decimal("45.0")),
            texts.TTK_CARD_LINE_TEMPLATE.format(name="Lime juice", amount="45"),
            id="a bare number, as the reference book stores it",
        ),
        pytest.param(
            make_line("Mint", qty_text="8 leaves"),
            texts.TTK_CARD_LINE_TEMPLATE.format(name="Mint", amount="8 leaves"),
            id="free text, kept as it was typed",
        ),
        pytest.param(
            make_line("Soda"),
            texts.TTK_CARD_LINE_PLAIN_TEMPLATE.format(name="Soda"),
            id="an ingredient named without an amount",
        ),
        pytest.param(
            make_line("Syrup", qty=Decimal("1.5"), unit=Unit.L),
            texts.TTK_CARD_LINE_TEMPLATE.format(
                name="Syrup",
                amount=texts.TTK_CARD_AMOUNT_TEMPLATE.format(
                    qty="1.5", unit=texts.unit_label(Unit.L)
                ),
            ),
            id="a fraction keeps its fraction",
        ),
    ],
)
def test_every_branch_of_an_amount_is_printed_as_the_service_classified_it(
    line: IngredientLine, expected: str
) -> None:
    """Decision D4: the branch is the service's; this module only writes it down."""
    screen = views.card(make_card(ingredients=[line]))
    assert screen.text.splitlines()[-1] == expected


def test_a_line_pointing_at_a_prep_is_printed_and_not_linked() -> None:
    """TZ 5.5 puts preps in a section of their own, and stage 0 does not build it."""
    line = make_line("Raspberry cordial", qty=Decimal("20"), unit=Unit.ML, prep_id=3)
    screen = views.card(make_card(ingredients=[line]))

    assert line.links_to_prep
    assert "Raspberry cordial" in screen.text
    assert captions(screen.markup) == [texts.BACK_BUTTON, texts.HOME_BUTTON]


def test_a_seasonal_card_says_so() -> None:
    screen = views.card(make_card(season_date=dt.date(2026, 9, 1)))
    assert texts.TTK_CARD_SEASONAL_MARK in screen.text.splitlines()[0]


def test_venue_text_is_safe_inside_the_html_the_card_is_written_in() -> None:
    """`TTK_CARD_NAME_TEMPLATE` is `<b>{name}</b>`, so what the venue typed is quoted."""
    screen = views.card(make_card(name="Rum & Cola", ingredients=[make_line("Cola <chilled>")]))

    assert "Rum &amp; Cola" in screen.text
    assert "<chilled>" not in screen.text


# --------------------------------------------------------------------------------------
# The hit list (TZ 5.5, decision D6)
# --------------------------------------------------------------------------------------


def test_two_drinks_of_one_name_are_told_apart_by_their_category() -> None:
    """Decision D6: a bar has an Americano the cocktail and an Americano the coffee."""
    page = make_page(
        [make_hit(1, "Americano", "classics"), make_hit(2, "Americano", "coffee")],
    )

    screen = views.results(page)
    buttons = captions(screen.markup)[:2]

    assert buttons[0] != buttons[1], "two identical captions is a list nobody can choose from"
    assert "classics" in buttons[0]
    assert "coffee" in buttons[1]
    assert payloads(screen.markup)[:2] == [
        RecipeShow(recipe_id=1).pack(),
        RecipeShow(recipe_id=2).pack(),
    ]


def test_a_page_offers_only_the_ends_of_the_list_that_exist() -> None:
    first = views.results(make_page([make_hit(1, "Americano", "coffee")], has_next=True))
    assert RecipePage(offset=MAX_SEARCH_RESULTS).pack() in payloads(first.markup)

    middle = views.results(
        make_page([make_hit(1, "Americano", "coffee")], offset=MAX_SEARCH_RESULTS, has_next=False)
    )
    assert RecipePage(offset=0).pack() in payloads(middle.markup)
    assert RecipePage(offset=2 * MAX_SEARCH_RESULTS).pack() not in payloads(middle.markup)


def test_nothing_found_offers_the_button_that_tells_the_manager() -> None:
    screen = views.results(make_page(query="mojito"))

    assert screen.text == texts.TTK_NOTHING_FOUND_TEMPLATE.format(query="mojito")
    assert RecipeMissing().pack() in payloads(screen.markup)


def test_suggestions_are_offered_above_the_report_button() -> None:
    """TZ 5.5: "not found, did you mean ..." — the second half of that screen."""
    screen = views.results(make_page(query="mahito"), suggestions=[make_hit(1, "Mojito", "bar")])

    assert texts.TTK_MAYBE_TITLE in screen.text
    assert payloads(screen.markup)[0] == RecipeShow(recipe_id=1).pack()
    assert RecipeMissing().pack() in payloads(screen.markup)


def test_a_blank_line_is_not_a_failed_search() -> None:
    """The service answers a blank query with an empty page; that is a re-invitation."""
    screen = views.results(make_page(query=""))

    assert screen.text == texts.TTK_SEARCH_PROMPT
    assert RecipeMissing().pack() not in payloads(screen.markup)


def test_every_second_level_screen_returns_to_this_section() -> None:
    """Decision D10: back on a card leads to the section, not to the main menu."""
    back = Nav(target=NavTarget.BACK, section=MenuAction.CATALOGUE).pack()

    assert back in payloads(views.card(make_card()).markup)
    assert back in payloads(views.results(make_page([make_hit(1, "Mojito", "bar")])).markup)
    assert back in payloads(views.results(make_page(query="mojito")).markup)


# --------------------------------------------------------------------------------------
# The empty state of the section (TZ 8.1)
# --------------------------------------------------------------------------------------


def test_an_empty_section_says_so_and_offers_nothing_to_press() -> None:
    """TZ 8.1: every venue starts here, and a button leading nowhere is worse than none."""
    screen = views.section(())

    assert texts.TTK_EMPTY in screen.text
    assert texts.TTK_SEARCH_PROMPT not in screen.text
    assert captions(screen.markup) == [texts.HOME_BUTTON]


def test_a_filled_section_invites_a_search() -> None:
    screen = views.section(("classics", "coffee"))

    assert texts.TTK_SEARCH_PROMPT in screen.text
    assert texts.TTK_EMPTY not in screen.text


# --------------------------------------------------------------------------------------
# The handlers, through a real dispatcher
# --------------------------------------------------------------------------------------


class FakeRecipes:
    """`RecipeService` as this section calls it, with the refusals it really has.

    `search` normalises and refuses a blank query, and `report_missing` answers `None` to
    one, because that is what the service does — a fake that reported a blank query would
    make `test_reporting_a_missing_recipe_reaches_the_service` green about a notification
    the manager would never get.
    """

    def __init__(
        self,
        *,
        hits: Sequence[RecipeHit] = (),
        categories: Sequence[str] = ("classics",),
        card: RecipeCard | None = None,
    ) -> None:
        self.rows = list(hits)
        self.known_categories = tuple(categories)
        self.the_card = card
        self.searched: list[tuple[str, int]] = []
        self.reported: list[tuple[str, int]] = []

    async def search(
        self,
        query: str,
        *,
        offset: int = 0,
        limit: int = MAX_SEARCH_RESULTS,
    ) -> SearchPage:
        cleaned = " ".join(query.split())
        self.searched.append((cleaned, offset))
        window = min(max(limit, 1), MAX_SEARCH_RESULTS)
        if not cleaned:
            return SearchPage(query="", hits=(), offset=offset, limit=window, has_next=False)
        visible = self.rows[offset : offset + window]
        return SearchPage(
            query=cleaned,
            hits=tuple(visible),
            offset=offset,
            limit=window,
            has_next=len(self.rows) > offset + window,
        )

    async def categories(self) -> tuple[str, ...]:
        return self.known_categories

    async def card(self, recipe_id: int) -> RecipeCard | None:
        if self.the_card is None or self.the_card.recipe_id != recipe_id:
            return None
        return self.the_card

    async def report_missing(self, *, query: str, reported_by: int) -> MissingRecipeAlert | None:
        cleaned = " ".join(query.split())
        if not cleaned:
            return None
        self.reported.append((cleaned, reported_by))
        return MissingRecipeAlert(query=cleaned, reported_by=reported_by)


@dataclass
class Bundle:
    """`VenueServices` as a handler of this section reads it: one service, by name."""

    recipes: FakeRecipes = field(default_factory=FakeRecipes)


@dataclass
class Stand:
    """A dispatcher with this router behind it, and the storage its state lives in."""

    dispatcher: Dispatcher
    bot: Bot
    state: FSMContext
    services: Bundle

    async def feed(self, update: Update, **extra: Any) -> None:
        await self.dispatcher.feed_update(
            self.bot,
            update,
            **{ACTOR_KEY: STAFF, SERVICES_KEY: self.services, **extra},
        )

    @property
    def sent(self) -> list[str]:
        return session_of(self.bot).sent_texts()

    @property
    def toasts(self) -> list[str | None]:
        return [call.text for call in session_of(self.bot).answers()]


def build_stand(
    services: Bundle | None = None,
    *,
    storage: BaseStorage | None = None,
) -> Stand:
    """A fresh process over (optionally) an existing store — a restart, in one argument."""
    bot = make_bot()
    store = storage if storage is not None else MemoryStorage()
    dispatcher = Dispatcher(storage=store)
    dispatcher.include_router(handlers.router())
    return Stand(
        dispatcher=dispatcher,
        bot=bot,
        state=FSMContext(
            storage=store,
            key=StorageKey(bot_id=bot.id, chat_id=CHAT_ID, user_id=STAFF_TELEGRAM_ID),
        ),
        services=services if services is not None else Bundle(),
    )


def typed(text: str) -> Update:
    return Update(update_id=1, message=make_message(text))


def pressed(payload: str) -> Update:
    return Update(update_id=1, callback_query=make_callback(payload))


async def test_the_caption_opens_the_section_and_waits_for_a_name() -> None:
    stand = build_stand()

    await stand.feed(typed(texts.MENU_CATALOGUE_BUTTON))

    assert stand.sent == [views.section(("classics",)).text]
    assert await stand.state.get_state() == RecipeSearch.query.state


async def test_an_empty_venue_is_told_the_truth_and_asked_for_nothing() -> None:
    """TZ 8.1: the section of a venue that has entered no recipe yet."""
    stand = build_stand(Bundle(FakeRecipes(categories=())))

    await stand.feed(typed(texts.MENU_CATALOGUE_BUTTON))

    assert texts.TTK_EMPTY in stand.sent[0]
    assert stand.services.recipes.searched == []


async def test_the_inline_button_opens_the_same_section() -> None:
    """TZ 5.2: the caption and `OpenSection` are two roads to one screen."""
    stand = build_stand()

    await stand.feed(
        pressed(OpenSection(section=MenuAction.CATALOGUE).pack()),
        **{PAYLOAD_KEY: OpenSection(section=MenuAction.CATALOGUE)},
    )

    assert stand.sent == [views.section(("classics",)).text]
    assert stand.toasts == [None], "TZ 9: the spinner is closed"


async def test_a_typed_line_is_searched_and_the_query_is_remembered() -> None:
    stand = build_stand(Bundle(FakeRecipes(hits=[make_hit(1, "Mojito", "classics")])))
    await stand.state.set_state(RecipeSearch.query)

    await stand.feed(typed("  mojito "))

    assert stand.services.recipes.searched == [("mojito", 0)]
    assert await stand.state.get_state() == RecipeSearch.results.state
    assert await stand.state.get_data() == {
        handlers.QUERY_KEY: "mojito",
        handlers.OFFSET_KEY: 0,
    }
    assert texts.TTK_FOUND_TEMPLATE.format(count=1) in stand.sent[0]


async def test_the_next_line_is_the_next_search() -> None:
    stand = build_stand()
    await stand.state.set_state(RecipeSearch.results)
    await stand.state.set_data({handlers.QUERY_KEY: "mojito", handlers.OFFSET_KEY: 0})

    await stand.feed(typed("negroni"))

    assert stand.services.recipes.searched == [("negroni", 0)]
    assert (await stand.state.get_data())[handlers.QUERY_KEY] == "negroni"


async def test_typing_outside_the_section_is_none_of_this_routers_business() -> None:
    """The chat belongs to whatever screen is open; this one listens in two states only."""
    stand = build_stand()

    await stand.feed(typed("mojito"))

    assert stand.services.recipes.searched == []
    assert stand.sent == []


async def test_pagination_keeps_the_query_through_a_restart_of_the_screen() -> None:
    """The button carries an offset and no text (D14), so the query must survive elsewhere.

    The second stand is a second process over the same store: nothing of the first one is
    left except the FSM state, and the pager still knows what was searched for.
    """
    store = MemoryStorage()
    rows = [make_hit(number, f"Drink {number}", "classics") for number in range(1, 13)]
    services = Bundle(FakeRecipes(hits=rows))

    first = build_stand(services, storage=store)
    await first.state.set_state(RecipeSearch.query)
    await first.feed(typed("drink"))

    second = build_stand(services, storage=store)
    await second.feed(
        pressed(RecipePage(offset=MAX_SEARCH_RESULTS).pack()),
        **{PAYLOAD_KEY: RecipePage(offset=MAX_SEARCH_RESULTS)},
    )

    assert services.recipes.searched == [("drink", 0), ("drink", MAX_SEARCH_RESULTS)]
    assert await second.state.get_data() == {
        handlers.QUERY_KEY: "drink",
        handlers.OFFSET_KEY: MAX_SEARCH_RESULTS,
    }
    assert "EditMessageText" in session_of(second.bot).method_names(), "TZ 8.2: one screen"


async def test_a_card_replaces_the_list_it_was_chosen_from() -> None:
    card = make_card(ingredients=[make_line("White rum", qty=Decimal("50"), unit=Unit.ML)])
    stand = build_stand(Bundle(FakeRecipes(card=card)))

    await stand.feed(
        pressed(RecipeShow(recipe_id=card.recipe_id).pack()),
        **{PAYLOAD_KEY: RecipeShow(recipe_id=card.recipe_id)},
    )

    assert "EditMessageText" in session_of(stand.bot).method_names()
    assert session_of(stand.bot).sent_texts() == []


async def test_a_recipe_deleted_while_the_list_was_open_is_refused_politely() -> None:
    stand = build_stand()

    await stand.feed(
        pressed(RecipeShow(recipe_id=404).pack()),
        **{PAYLOAD_KEY: RecipeShow(recipe_id=404)},
    )

    assert stand.toasts == [texts.ERROR_GONE]


async def test_back_returns_to_the_page_the_card_was_opened_from() -> None:
    """The page is rebuilt from the state, so it is the page and not the top of the list."""
    rows = [make_hit(number, f"Drink {number}", "classics") for number in range(1, 13)]
    stand = build_stand(Bundle(FakeRecipes(hits=rows)))
    await stand.state.set_state(RecipeSearch.results)
    await stand.state.set_data(
        {handlers.QUERY_KEY: "drink", handlers.OFFSET_KEY: MAX_SEARCH_RESULTS}
    )
    back = Nav(target=NavTarget.BACK, section=MenuAction.CATALOGUE)

    await stand.feed(pressed(back.pack()), **{PAYLOAD_KEY: back})

    assert stand.services.recipes.searched == [("drink", MAX_SEARCH_RESULTS)]


async def test_reporting_a_missing_recipe_reaches_the_service_and_nobody_else() -> None:
    """TZ 5.5: the service notifies the manager; the handler says one word to the reporter."""
    stand = build_stand()
    await stand.state.set_state(RecipeSearch.results)
    await stand.state.set_data({handlers.QUERY_KEY: "mahito", handlers.OFFSET_KEY: 0})

    await stand.feed(pressed(RecipeMissing().pack()), **{PAYLOAD_KEY: RecipeMissing()})

    assert stand.services.recipes.reported == [("mahito", STAFF.user_id)]
    assert stand.toasts == [texts.TTK_REPORTED]
    assert stand.sent == [], "TZ 6: a message to another person goes through notifications"


async def test_reporting_without_a_query_tells_nobody_anything() -> None:
    """The screen outlived the state that gave it meaning (the FSM has a day's TTL)."""
    stand = build_stand()

    await stand.feed(pressed(RecipeMissing().pack()), **{PAYLOAD_KEY: RecipeMissing()})

    assert stand.services.recipes.reported == []
    assert stand.toasts == [texts.ERROR_OUTDATED_SCREEN]
