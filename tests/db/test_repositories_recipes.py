"""Recipe repositories (plan, task 13; acceptance test 40).

The search has to do three things at once (TZ 4.6): match the name, match a synonym, and
survive a typo. The shape test reads the compiled statement and needs no server; the
behaviour tests run the real thing, where `similarity()` and `unnest(aliases)` are the
whole point and a stub would prove nothing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import Unit
from src.db.repositories.recipes import RecipeIngredientRepo, RecipeRepo

# The recording session lives next door: one stand-in for `AsyncSession`, used by every
# shape test of this package.
from tests.db.test_repositories_checklists import Recorder, as_session
from tests.factories import create_recipe, create_venue

VENUE_ID = 7

#: A cocktail and a misspelling of it — the pair acceptance test 40 is written about.
DRINK = "Мохито"
TYPO = "мохита"


# --------------------------------------------------------------------------------------
# Shape of the statement — no database
# --------------------------------------------------------------------------------------


async def test_search_looks_at_name_aliases_and_similarity() -> None:
    recorder = Recorder()
    repo = RecipeRepo(as_session(recorder), VENUE_ID)

    await repo.search("mohita", limit=10)

    statement = recorder.sql()
    # The venue plus the shared BarPoint library (TZ 3.3).
    assert "recipes.venue_id = " in statement
    assert "recipes.venue_id IS NULL" in statement
    assert "similarity(recipes.name" in statement
    assert "ILIKE" in statement
    # Synonyms are matched element by element, and the subquery carries the scope itself.
    assert "unnest(recipes.aliases)" in statement
    assert "similarity(anon_1.alias" in statement
    assert "ORDER BY" in statement


async def test_a_blank_query_never_reaches_the_database() -> None:
    """TZ 5.5 rules out dumping the whole table, so an empty query is not a query."""
    recorder = Recorder()
    repo = RecipeRepo(as_session(recorder), VENUE_ID)

    assert await repo.search("   ", limit=10) == []
    assert await repo.count_search("") == 0
    assert not recorder.statements


# --------------------------------------------------------------------------------------
# Behaviour — against a real PostgreSQL
# --------------------------------------------------------------------------------------


@pytest.mark.db
async def test_a_typo_still_finds_the_drink(session: AsyncSession) -> None:
    """Acceptance test 40: the misspelling reaches the recipe, and the hit carries D6."""
    venue = await create_venue(session)
    await create_recipe(session, venue, name=DRINK, category="cocktails")
    repo = RecipeRepo(session, venue.id)

    found = await repo.search(TYPO, limit=10)

    assert [row.name for row in found] == [DRINK]
    assert found[0].category == "cocktails", "the category is what tells two same names apart"
    assert await repo.count_search(TYPO) == 1


@pytest.mark.db
async def test_a_synonym_is_searched_too(session: AsyncSession) -> None:
    venue = await create_venue(session)
    recipe = await create_recipe(session, venue, name="Highball 1", aliases=[DRINK])
    repo = RecipeRepo(session, venue.id)

    by_alias = await repo.search(TYPO, limit=10)
    assert [row.id for row in by_alias] == [recipe.id]


@pytest.mark.db
async def test_an_exact_name_outranks_a_similar_one(session: AsyncSession) -> None:
    venue = await create_venue(session)
    await create_recipe(session, venue, name="Tonic 1", category="soft")
    exact = await create_recipe(session, venue, name="Tonic", category="soft")
    repo = RecipeRepo(session, venue.id)

    found = await repo.search("tonic", limit=10)
    assert found[0].id == exact.id
    assert len(found) == 2


@pytest.mark.db
async def test_the_library_is_visible_and_another_venue_is_not(session: AsyncSession) -> None:
    venue = await create_venue(session)
    stranger = await create_venue(session)
    own = await create_recipe(session, venue, name="Spritz own", category="own")
    shared = await create_recipe(session, None, name="Spritz shared", category="shared")
    foreign = await create_recipe(session, stranger, name="Spritz foreign", category="foreign")

    repo = RecipeRepo(session, venue.id)
    found = {row.id for row in await repo.search("spritz", limit=10)}

    assert own.id in found
    assert shared.id in found, "TZ 3.3: the shared BarPoint library is readable"
    assert foreign.id not in found
    assert await repo.get(shared.id) is not None
    assert await repo.get(foreign.id) is None
    assert set(await repo.list_categories()) == {"own", "shared"}
    assert [row.id for row in await repo.list_by_category("OWN", limit=10, offset=0)] == [own.id]


@pytest.mark.db
async def test_a_library_row_is_read_only(session: AsyncSession) -> None:
    """Question C4 is open: a venue reads the library and does not edit it (decision D9)."""
    venue = await create_venue(session)
    shared = await create_recipe(session, None, name="Negroni shared", category="shared")
    repo = RecipeRepo(session, venue.id)
    ingredients = RecipeIngredientRepo(session, venue.id)

    assert await repo.update(shared.id, name="edited") is None
    assert await ingredients.replace_all(shared.id, [{"name": "gin"}]) == []


@pytest.mark.db
async def test_ingredients_are_replaced_as_a_whole(session: AsyncSession) -> None:
    venue = await create_venue(session)
    recipe = await create_recipe(session, venue, name="Sour 1")
    ingredients = RecipeIngredientRepo(session, venue.id)

    await ingredients.replace_all(
        recipe.id,
        [
            {"name": "first", "qty": Decimal("50"), "unit": Unit.ML},
            {"name": "second", "qty_text": "top"},
        ],
    )
    written = await ingredients.replace_all(recipe.id, [{"name": "only"}])

    assert [row.name for row in written] == ["only"]
    listed = await ingredients.list_for_recipe(recipe.id)
    assert [row.name for row in listed] == ["only"]
    assert listed[0].order_index == 0


@pytest.mark.db
async def test_a_recipe_of_another_venue_is_not_editable(session: AsyncSession) -> None:
    venue = await create_venue(session)
    stranger = await create_venue(session)
    recipe = await create_recipe(session, venue, name="Daiquiri 1")

    foreign = RecipeRepo(session, stranger.id)
    foreign_ingredients = RecipeIngredientRepo(session, stranger.id)

    assert await foreign.update(recipe.id, name="edited") is None
    assert await foreign_ingredients.list_for_recipe(recipe.id) == []
    assert await foreign_ingredients.replace_all(recipe.id, [{"name": "x"}]) == []

    created = await foreign.create(name="Own recipe", category="c")
    assert created.venue_id == stranger.id, "a repository writes into its own venue only"
