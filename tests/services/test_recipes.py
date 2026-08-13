"""Recipe service (plan, task 18; TZ 5.5).

Typo tolerance itself is SQL — `similarity()` over a trigram index, plan task 13 — so the
fake repository here does a plain case-insensitive match and the tests check the two things
that are genuinely this service's job: that the query reaches the repository intact (so the
index has something to be fuzzy about), and that everything around the query is right —
the ten-hit cap, the pager, the category on every hit, and a card that survives a recipe
with nothing but a name and a list of ingredients.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from src.db.models import Base, Recipe, RecipeIngredient, Unit
from src.db.repositories.recipes import RecipeRepo
from src.services.recipes import (
    MAX_SEARCH_RESULTS,
    AmountKind,
    MissingRecipeAlert,
    RecipeService,
    classify_amount,
)

# The recording session lives with the repository tests: one stand-in for `AsyncSession`,
# used wherever a statement has to be inspected without a database.
from tests.db.test_repositories_checklists import Recorder, as_session

VENUE_ID = 1
OTHER_VENUE_ID = 2
USER_ID = 7

COCKTAILS = "cocktails"
COFFEE = "coffee"


# --------------------------------------------------------------------------------------
# Model factories (tests/factories.py belongs to task 5a; these live with their test)
# --------------------------------------------------------------------------------------


def make_recipe(
    recipe_id: int,
    *,
    name: str,
    category: str = COCKTAILS,
    venue_id: int | None = VENUE_ID,
    aliases: Sequence[str] = (),
    glassware: str | None = None,
    method: str | None = None,
    ice: str | None = None,
    garnish: str | None = None,
    instruction: str | None = None,
    season_date: dt.date | None = None,
) -> Recipe:
    return Recipe(
        id=recipe_id,
        venue_id=venue_id,
        name=name,
        aliases=list(aliases),
        category=category,
        glassware=glassware,
        method=method,
        ice=ice,
        garnish=garnish,
        instruction=instruction,
        season_date=season_date,
        is_active=True,
    )


def make_ingredient(
    ingredient_id: int,
    recipe_id: int,
    *,
    name: str,
    qty: Decimal | None = None,
    unit: Unit | None = None,
    qty_text: str | None = None,
    order_index: int = 0,
    product_id: int | None = None,
    prep_id: int | None = None,
) -> RecipeIngredient:
    return RecipeIngredient(
        id=ingredient_id,
        recipe_id=recipe_id,
        name=name,
        qty=qty,
        unit=unit,
        qty_text=qty_text,
        order_index=order_index,
        product_id=product_id,
        prep_id=prep_id,
    )


# --------------------------------------------------------------------------------------
# Fake repositories
# --------------------------------------------------------------------------------------


class FakeRecipes:
    """`RecipeRepository`. Matching is plain and case-insensitive on purpose.

    A repository that returns the venue's rows *and* the library's is the contract
    (`BaseRepository.library()`); deciding that a local row wins over a global twin is the
    service's, which is what `test_a_local_recipe_hides_its_library_twin` checks.

    `_visible()` is that `library()`: `venue_id = :vid OR venue_id IS NULL`, over a table
    every venue writes into. Both halves are load-bearing — dropping the second hides the
    shared library, dropping the first hands over the bar next door's recipes (TZ 3.3).
    """

    def __init__(
        self,
        ingredients: FakeIngredients,
        recipes: Sequence[Recipe] = (),
        *,
        venue_id: int = VENUE_ID,
        table: list[Recipe] | None = None,
    ) -> None:
        #: The whole `recipes` table, every venue and the library in it. `table` adopts the
        #: table of another repository, the way two venues share the real one.
        self.recipes: list[Recipe] = list(recipes) if table is None else table
        self.venue_id = venue_id
        self.queries: list[str] = []
        self._ingredients = ingredients
        # The child repository reaches its venue through this table and nowhere else.
        ingredients.bind_recipes(self.recipes)

    @property
    def ingredients(self) -> FakeIngredients:
        """The child repository bound to this one — same tables, same venue."""
        return self._ingredients

    def neighbour(self, venue_id: int) -> FakeRecipes:
        """The same two tables seen through another venue's repositories (TZ 9, 11.3).

        What makes "another venue's recipe" a row that genuinely exists and is genuinely
        unreachable from here, rather than a row nobody ever wrote.
        """
        ingredients = FakeIngredients(venue_id=venue_id, table=self._ingredients.ingredients)
        return FakeRecipes(ingredients, venue_id=venue_id, table=self.recipes)

    def _visible(self) -> list[Recipe]:
        return [row for row in self.recipes if row.venue_id in (self.venue_id, None)]

    async def get(self, recipe_id: int) -> Recipe | None:
        return next((row for row in self._visible() if row.id == recipe_id), None)

    async def search(self, query: str, *, limit: int, offset: int = 0) -> Sequence[Recipe]:
        self.queries.append(query)
        folded = query.casefold()
        found = [
            row
            for row in self._visible()
            if folded in row.name.casefold()
            or any(folded == alias.casefold() for alias in row.aliases)
        ]
        return found[offset : offset + limit]

    async def count_search(self, query: str) -> int:
        return len(await self.search(query, limit=len(self.recipes) + 1))

    async def list_by_category(
        self,
        category: str,
        *,
        limit: int,
        offset: int,
    ) -> Sequence[Recipe]:
        self.queries.append(category)
        folded = category.casefold()
        found = [row for row in self._visible() if row.category.casefold() == folded]
        return found[offset : offset + limit]

    async def list_categories(self) -> Sequence[str]:
        seen: list[str] = []
        for row in self._visible():
            if row.category not in seen:
                seen.append(row.category)
        return seen

    async def create(self, **fields: Any) -> Recipe:
        raise NotImplementedError

    async def update(self, recipe_id: int, **fields: Any) -> Recipe | None:
        raise NotImplementedError


class FakeIngredients:
    """`RecipeIngredientRepository`; hands rows back shuffled so the service must sort.

    A child repository (decision D9): `recipe_ingredients` owns no `venue_id`, so
    `RecipeIngredientRepo.list_for_recipe` reaches its scope through a join to `recipes` —
    and it is the one child repository that joins with `library()` rather than
    `for_venue()`, because the card of a global recipe has to render its ingredients
    (TZ 3.3). :meth:`_parent` is that join: a recipe of another venue is not there, a
    library recipe is.
    """

    def __init__(
        self,
        ingredients: Sequence[RecipeIngredient] = (),
        *,
        venue_id: int = VENUE_ID,
        table: list[RecipeIngredient] | None = None,
    ) -> None:
        #: The whole `recipe_ingredients` table, every venue in it: the scope is the `WHERE`
        #: and not the storage, so another venue's repository may adopt the same list.
        self.ingredients: list[RecipeIngredient] = list(ingredients) if table is None else table
        self.venue_id = venue_id
        #: The recipes table this child hangs off. `FakeRecipes` owns the list and hands it
        #: over, so a recipe written after this fake was built is joinable here too.
        self._recipes: list[Recipe] = []

    def bind_recipes(self, recipes: list[Recipe]) -> None:
        """Called by `FakeRecipes`: the same list object, not a copy of it."""
        self._recipes = recipes

    @property
    def parent(self) -> type[Base]:
        return Recipe

    @property
    def parent_fk(self) -> str:
        return "recipe_id"

    def _parent(self, recipe_id: int) -> Recipe | None:
        """`JOIN recipes ON ... WHERE recipes.venue_id = :vid OR recipes.venue_id IS NULL`.

        `None` means the recipe is unknown *or* belongs to another venue — one answer for
        both, which is what makes a forged `recipe_id` address nothing (TZ 9).
        """
        recipe = next((row for row in self._recipes if row.id == recipe_id), None)
        if recipe is None or recipe.venue_id not in (self.venue_id, None):
            return None
        return recipe

    async def list_for_recipe(self, recipe_id: int) -> Sequence[RecipeIngredient]:
        if self._parent(recipe_id) is None:
            return []
        rows = [row for row in self.ingredients if row.recipe_id == recipe_id]
        return list(reversed(rows))

    async def replace_all(
        self,
        recipe_id: int,
        ingredients: Sequence[dict[str, Any]],
    ) -> Sequence[RecipeIngredient]:
        raise NotImplementedError


class FakeNotifier:
    def __init__(self) -> None:
        self.missing: list[MissingRecipeAlert] = []

    async def recipe_missing(self, alert: MissingRecipeAlert) -> None:
        self.missing.append(alert)


class Harness:
    def __init__(
        self,
        *,
        recipes: Sequence[Recipe] = (),
        ingredients: Sequence[RecipeIngredient] = (),
    ) -> None:
        self.ingredients = FakeIngredients(ingredients)
        self.recipes = FakeRecipes(self.ingredients, recipes)
        self.notifier = FakeNotifier()
        self.service = RecipeService(
            recipes=self.recipes,
            ingredients=self.ingredients,
            notifier=self.notifier,
        )


# --------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------


def many(count: int, *, prefix: str = "mojito", category: str = COCKTAILS) -> list[Recipe]:
    """`count` distinct rows that all match `prefix` — enough to need a second page."""
    return [
        make_recipe(index, name=f"{prefix}_{index}", category=category)
        for index in range(1, count + 1)
    ]


async def test_a_page_never_exceeds_ten_hits() -> None:
    """TZ 5.5: at most 10, with pagination."""
    harness = Harness(recipes=many(25))

    page = await harness.service.search("mojito")

    assert len(page.hits) == MAX_SEARCH_RESULTS
    assert page.has_next is True
    assert page.has_previous is False
    assert page.next_offset == 10
    assert page.previous_offset is None
    assert page.page_number == 1


async def test_a_caller_cannot_ask_for_more_than_ten() -> None:
    harness = Harness(recipes=many(25))

    page = await harness.service.search("mojito", limit=50)

    assert page.limit == MAX_SEARCH_RESULTS
    assert len(page.hits) == MAX_SEARCH_RESULTS


async def test_the_last_page_reports_no_next() -> None:
    harness = Harness(recipes=many(12))

    page = await harness.service.search("mojito", offset=10)

    assert len(page.hits) == 2
    assert page.has_next is False
    assert page.has_previous is True
    assert page.previous_offset == 0
    assert page.page_number == 2


async def test_every_hit_carries_its_category() -> None:
    """Decision D6: two rows may legitimately share a name in different categories."""
    harness = Harness(
        recipes=[
            make_recipe(1, name="Americano", category=COCKTAILS),
            make_recipe(2, name="Americano", category=COFFEE),
        ]
    )

    page = await harness.service.search("americano")

    assert sorted(hit.category for hit in page.hits) == sorted([COCKTAILS, COFFEE])
    assert len({(hit.name, hit.category) for hit in page.hits}) == 2


async def test_search_is_case_insensitive() -> None:
    harness = Harness(recipes=[make_recipe(1, name="Mojito")])

    for query in ("MOJITO", "mojito", "MoJiTo"):
        page = await harness.service.search(query)
        assert [hit.recipe_id for hit in page.hits] == [1]


async def test_search_finds_a_synonym() -> None:
    harness = Harness(recipes=[make_recipe(1, name="Mojito", aliases=["mohito"])])

    page = await harness.service.search("mohito")

    assert [hit.recipe_id for hit in page.hits] == [1]


async def test_the_query_reaches_the_repository_trimmed_but_otherwise_intact() -> None:
    """Typo tolerance is `similarity()` in SQL; mangling the query here would defeat it."""
    harness = Harness(recipes=[make_recipe(1, name="Mojito")])

    await harness.service.search("  Mo    jito \n")

    assert harness.recipes.queries == ["Mo jito"]


async def test_a_blank_query_returns_an_empty_page_without_asking_the_database() -> None:
    """TZ 5.5 rules out dumping the whole table, so a blank query searches for nothing."""
    harness = Harness(recipes=[make_recipe(1, name="Mojito")])

    page = await harness.service.search("   ")

    assert page.is_empty is True
    assert page.has_next is False
    assert harness.recipes.queries == []


async def test_no_match_gives_an_empty_page() -> None:
    harness = Harness(recipes=[make_recipe(1, name="Mojito")])

    page = await harness.service.search("negroni")

    assert page.is_empty is True
    assert page.has_next is False


async def test_a_local_recipe_hides_its_library_twin() -> None:
    """TZ 3.3 with decision D6: same category, same folded name -> the venue's row wins."""
    harness = Harness(
        recipes=[
            make_recipe(1, name="Mojito", venue_id=None),
            make_recipe(2, name="  mojito ", venue_id=VENUE_ID),
        ]
    )

    page = await harness.service.search("mojito")

    assert [hit.recipe_id for hit in page.hits] == [2]
    assert page.hits[0].is_library is False


async def test_a_library_recipe_without_a_local_twin_stays_visible() -> None:
    harness = Harness(recipes=[make_recipe(1, name="Mojito", venue_id=None)])

    page = await harness.service.search("mojito")

    assert [hit.recipe_id for hit in page.hits] == [1]
    assert page.hits[0].is_library is True


# --------------------------------------------------------------------------------------
# Seasonal first in a category listing, relevance first in a search (TZ 5.5)
# --------------------------------------------------------------------------------------

#: What `_seasonal_first()` compiles to. The ordering itself is SQL — the service only has
#: to leave it alone — so the assertion is made on the statement, the way the shape tests in
#: `tests/db/` make theirs. No database is involved.
SEASONAL_FIRST = "recipes.season_date IS NULL ASC"

#: What the leading relevance key compiles to: the exact-name test, `TRUE` first.
EXACT_NAME_FIRST = "lower(btrim(recipes.name))"


def order_by(statement: str) -> str:
    return statement.rsplit("ORDER BY", 1)[1].strip()


async def test_seasonal_positions_lead_a_category_listing() -> None:
    """TZ 5.5 literally: seasonal positions are shown first in the list.

    The list it is about is the one a category opens. Every row there is on screen for the
    same reason — the tap that opened the category — so nothing about the rows themselves
    orders them, and the seasonal ones take the top.
    """
    recorder = Recorder()
    repo = RecipeRepo(as_session(recorder), VENUE_ID)

    await repo.list_by_category(COCKTAILS, limit=MAX_SEARCH_RESULTS, offset=0)

    keys = order_by(recorder.sql())
    assert keys.startswith(SEASONAL_FIRST), keys
    # Not the only key: a listing ordered by seasonality alone is arbitrary underneath.
    assert "lower(recipes.name)" in keys


async def test_a_search_is_led_by_relevance_not_by_the_season() -> None:
    """A typed query is a statement about which drink is wanted (TZ 5.5, five seconds).

    Seasonality ahead of relevance would answer a name with a seasonal position that
    merely also matched — the exact hit pushed below it. Ordering a search starts with
    what was typed; the seasonal key stays in the category listing above.
    """
    recorder = Recorder()
    repo = RecipeRepo(as_session(recorder), VENUE_ID)

    await repo.search("mojito", limit=MAX_SEARCH_RESULTS)

    keys = order_by(recorder.sql())
    assert SEASONAL_FIRST not in keys, keys
    assert keys.startswith(EXACT_NAME_FIRST), keys
    assert "similarity" in keys, "the typo pass still orders what the exact tests leave"


async def test_the_service_hands_the_page_over_in_the_order_it_was_given() -> None:
    """The repository's ordering is worth nothing if the service re-sorts the rows."""
    harness = Harness(
        recipes=[
            make_recipe(1, name="Pumpkin spice", season_date=dt.date(2026, 9, 1)),
            make_recipe(2, name="Pumpkin cooler"),
        ]
    )

    page = await harness.service.search("pumpkin")

    assert [hit.recipe_id for hit in page.hits] == [1, 2]
    card = await harness.service.card(1)
    assert card is not None
    assert card.is_seasonal is True


# --------------------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------------------


async def test_categories_are_empty_on_a_fresh_venue() -> None:
    """TZ 8.1: the empty state is the first thing every new venue sees."""
    harness = Harness()

    assert await harness.service.categories() == ()


async def test_browsing_a_category_pages_like_search() -> None:
    harness = Harness(
        recipes=[
            *many(12, prefix="drink"),
            make_recipe(90, name="espresso", category=COFFEE),
        ]
    )

    page = await harness.service.browse(COCKTAILS)

    assert len(page.hits) == MAX_SEARCH_RESULTS
    assert page.has_next is True
    assert {hit.category for hit in page.hits} == {COCKTAILS}


async def test_browsing_a_blank_category_asks_the_database_nothing() -> None:
    harness = Harness(recipes=[make_recipe(1, name="Mojito")])

    page = await harness.service.browse("  ")

    assert page.is_empty is True
    assert harness.recipes.queries == []


# --------------------------------------------------------------------------------------
# The card
# --------------------------------------------------------------------------------------


def mojito() -> Harness:
    recipe = make_recipe(
        1,
        name="Mojito",
        glassware="highball",
        method="build",
        ice="crushed",
        garnish="mint_sprig",
        instruction="muddle_and_build",
    )
    ingredients = [
        make_ingredient(1, 1, name="white_rum", qty=Decimal("50"), unit=Unit.ML, order_index=0),
        make_ingredient(2, 1, name="lime_juice", qty=Decimal("25"), unit=Unit.ML, order_index=1),
        make_ingredient(3, 1, name="mint", qty=Decimal("8"), order_index=2),
        make_ingredient(4, 1, name="soda", qty_text="top", order_index=3),
        make_ingredient(5, 1, name="basil", qty_text="5_leaves", order_index=4),
    ]
    return Harness(recipes=[recipe], ingredients=ingredients)


async def test_the_card_keeps_the_ingredient_order() -> None:
    card = await mojito().service.card(1)

    assert card is not None
    assert [line.name for line in card.ingredients] == [
        "white_rum",
        "lime_juice",
        "mint",
        "soda",
        "basil",
    ]


async def test_the_three_amount_branches_appear_on_one_card() -> None:
    card = await mojito().service.card(1)

    assert card is not None
    kinds = {line.name: line.kind for line in card.ingredients}
    assert kinds["white_rum"] is AmountKind.MEASURED
    assert kinds["mint"] is AmountKind.NUMERIC
    assert kinds["soda"] is AmountKind.TEXT
    assert kinds["basil"] is AmountKind.TEXT


async def test_the_card_carries_the_serving_line() -> None:
    card = await mojito().service.card(1)

    assert card is not None
    assert card.has_serving_line is True
    assert (card.glassware, card.method, card.ice) == ("highball", "build", "crushed")
    assert card.garnish == "mint_sprig"
    assert card.instruction == "muddle_and_build"


async def test_blank_fields_are_absent_rather_than_empty() -> None:
    """Plan, task 18: an empty field must vanish, not render as a dangling colon."""
    harness = Harness(
        recipes=[
            make_recipe(
                1,
                name="Negroni",
                glassware="   ",
                method="",
                ice=None,
                garnish=" ",
                instruction="\n",
            )
        ],
        ingredients=[make_ingredient(1, 1, name="gin", qty=Decimal("30"), unit=Unit.ML)],
    )

    card = await harness.service.card(1)

    assert card is not None
    assert card.glassware is None
    assert card.method is None
    assert card.ice is None
    assert card.garnish is None
    assert card.instruction is None
    assert card.has_serving_line is False


async def test_a_card_with_nothing_but_a_name_and_ingredients_is_valid() -> None:
    """The normal state of a venue that has just started typing its recipes in (TZ 8.1)."""
    harness = Harness(
        recipes=[make_recipe(1, name="House Special")],
        ingredients=[make_ingredient(1, 1, name="secret_syrup", qty_text="dash")],
    )

    card = await harness.service.card(1)

    assert card is not None
    assert card.name == "House Special"
    assert card.category == COCKTAILS
    assert card.has_ingredients is True
    assert card.has_serving_line is False
    assert card.is_seasonal is False
    assert card.ingredients[0].kind is AmountKind.TEXT


async def test_a_card_without_ingredients_is_still_a_card() -> None:
    harness = Harness(recipes=[make_recipe(1, name="Water")])

    card = await harness.service.card(1)

    assert card is not None
    assert card.ingredients == ()
    assert card.has_ingredients is False


async def test_an_ingredient_can_point_at_a_prep() -> None:
    """TZ 5.5: from a cocktail card, jump to the card of the prep it contains."""
    harness = Harness(
        recipes=[make_recipe(1, name="Lemonade")],
        ingredients=[make_ingredient(1, 1, name="berry_cordial", qty_text="top", prep_id=77)],
    )

    card = await harness.service.card(1)

    assert card is not None
    assert card.ingredients[0].links_to_prep is True
    assert card.ingredients[0].prep_id == 77


async def test_a_foreign_recipe_has_no_card() -> None:
    """TZ 9: an id from someone else's callback_data resolves to nothing."""
    harness = Harness(recipes=[make_recipe(1, name="Mojito", venue_id=OTHER_VENUE_ID)])

    assert await harness.service.card(1) is None


async def test_the_ingredients_of_another_venues_recipe_are_unreachable() -> None:
    """The join of decision D9, both halves of it (TZ 3.3, TZ 9, acceptance 11.3).

    `recipe_ingredients` carries no `venue_id`, so the composition of a drink is scoped by
    the recipe it hangs off and by nothing else. The neighbour's rows genuinely exist in the
    same table, with ids a forged `callback_data` could name; the library's rows exist there
    too and must stay readable. A fake that filtered on `recipe_id` alone was green about
    both — and would have rendered the bar next door's recipe card here.
    """
    harness = Harness(
        recipes=[
            make_recipe(1, name="Mojito", venue_id=OTHER_VENUE_ID),
            make_recipe(2, name="Highball", venue_id=None),
            make_recipe(3, name="Spritz"),
        ],
        ingredients=[
            make_ingredient(1, 1, name="foreign_rum"),
            make_ingredient(2, 2, name="library_soda"),
            make_ingredient(3, 3, name="local_prosecco"),
        ],
    )

    assert list(await harness.ingredients.list_for_recipe(1)) == []
    assert list(await harness.ingredients.list_for_recipe(404)) == []
    # The other half of `library()`: a global recipe still renders its ingredients.
    assert [row.name for row in await harness.ingredients.list_for_recipe(2)] == ["library_soda"]
    assert [row.name for row in await harness.ingredients.list_for_recipe(3)] == ["local_prosecco"]

    # Hidden by the predicate, not by an absence: its own venue reads the rows back.
    neighbour = harness.recipes.neighbour(OTHER_VENUE_ID).ingredients
    assert [row.name for row in await neighbour.list_for_recipe(1)] == ["foreign_rum"]


async def test_a_foreign_card_leaks_neither_the_recipe_nor_its_ingredients() -> None:
    """The forged `callback_data` of acceptance 11.3, end to end, while a library card works."""
    harness = Harness(
        recipes=[
            make_recipe(1, name="Mojito", venue_id=OTHER_VENUE_ID),
            make_recipe(2, name="Highball", venue_id=None),
        ],
        ingredients=[
            make_ingredient(1, 1, name="foreign_rum"),
            make_ingredient(2, 2, name="library_soda"),
        ],
    )

    assert await harness.service.card(1) is None
    library = await harness.service.card(2)
    assert library is not None
    assert [line.name for line in library.ingredients] == ["library_soda"]


async def test_an_unknown_recipe_has_no_card() -> None:
    harness = Harness(recipes=[make_recipe(1, name="Mojito")])

    assert await harness.service.card(404) is None


# --------------------------------------------------------------------------------------
# The amount branches, one by one (decision D4, TZ 7)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("qty", "unit", "qty_text", "expected"),
    [
        (Decimal("50"), Unit.ML, None, AmountKind.MEASURED),
        (Decimal("45"), None, None, AmountKind.NUMERIC),
        (None, None, "top", AmountKind.TEXT),
        (None, Unit.ML, "top", AmountKind.TEXT),
        (None, None, None, AmountKind.ABSENT),
        (None, Unit.ML, None, AmountKind.ABSENT),
        (None, None, "   ", AmountKind.ABSENT),
    ],
)
def test_the_amount_branch_is_chosen_by_what_is_filled_in(
    qty: Decimal | None,
    unit: Unit | None,
    qty_text: str | None,
    expected: AmountKind,
) -> None:
    assert classify_amount(qty, unit, qty_text) is expected


def test_free_text_wins_over_a_bare_number() -> None:
    """ "5 leaves" says more than "5"; TZ 7 keeps the text precisely so it can be used."""
    assert classify_amount(Decimal("5"), None, "5_leaves") is AmountKind.TEXT


def test_a_measured_amount_wins_over_free_text() -> None:
    assert classify_amount(Decimal("50"), Unit.ML, "about_50") is AmountKind.MEASURED


async def test_a_blank_amount_text_does_not_reach_the_card() -> None:
    harness = Harness(
        recipes=[make_recipe(1, name="Negroni")],
        ingredients=[make_ingredient(1, 1, name="gin", qty_text="  ")],
    )

    card = await harness.service.card(1)

    assert card is not None
    assert card.ingredients[0].qty_text is None
    assert card.ingredients[0].kind is AmountKind.ABSENT


# --------------------------------------------------------------------------------------
# "The recipe is missing"
# --------------------------------------------------------------------------------------


async def test_reporting_a_missing_recipe_reaches_the_manager() -> None:
    harness = Harness()

    alert = await harness.service.report_missing(query="  spicy   margarita ", reported_by=USER_ID)

    assert alert is not None
    assert alert.query == "spicy margarita"
    assert alert.reported_by == USER_ID
    assert harness.notifier.missing == [alert]


async def test_reporting_nothing_tells_the_manager_nothing() -> None:
    harness = Harness()

    assert await harness.service.report_missing(query="   ", reported_by=USER_ID) is None
    assert harness.notifier.missing == []
