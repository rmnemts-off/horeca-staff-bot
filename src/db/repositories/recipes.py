"""Recipes and their ingredients (TZ 4.6, 5.5; plan task 13).

Search is the whole point of this module. TZ 4.6 asks for three things at once — by `name`,
by `aliases`, and with typos survived — and TZ 5.5 measures the result in seconds behind the
bar, so all three happen in one query:

* an exact fold of the name, then a substring of it, then trigram `similarity()` above a
  threshold. A misspelled name typed with one hand still reaches the drink through the
  last one (test 40 of the plan);
* the same two tests are applied to every element of `aliases`, inside a correlated
  `EXISTS (SELECT ... FROM unnest(aliases))`. The array has a GIN index for exact hits and
  the similarity pass over it is a scan on purpose (schema notes: at the ~200 rows of
  TZ 5.5 it stays far inside the five-second rule);
* the result is ordered by relevance — exact name, then substring, then everything else by
  similarity — so the first button is the one the bartender meant.

Seasonality is a key of `list_by_category` and of nothing else. TZ 5.5 says seasonal
positions are *shown first in the list*, and the list it means is the one a category opens:
there the rows are equally relevant to the tap that produced them, so the order is the
venue's to decide and the seasonal ones lead (`_seasonal_first`). A search is answered by
what was typed. Putting the same key ahead of relevance there would answer a query for
"mojito" with the seasonal pumpkin drink above the Mojito itself — and it is the same TZ 5.5
that measures this screen in five seconds.

Reads use `library()`: a venue sees its own rows plus the shared BarPoint library
(`venue_id IS NULL`, TZ 3.3). Writes use `for_venue()`: editing a library row is question
C4 and is deliberately not possible yet. Which of the two a row is, the service decides by
`recipe.venue_id`, and it needs `category` to do it — decision D6 makes
`(venue_id, category, lower(btrim(name)))` the key, so two rows may legitimately share a
name and only the category tells them apart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import ColumnElement, delete, func, or_, select, update

from src.db.models import Recipe, RecipeIngredient
from src.db.repositories.base import BaseRepository, ChildRepository

#: Trigram threshold for a typo to still count as a hit. The pg_trgm default: a name with
#: one wrong letter scores well above it, an unrelated word well below.
SIMILARITY_THRESHOLD = 0.3

#: Escape character of the `ILIKE` patterns below. Not a backslash on purpose: how one is
#: quoted inside a string literal depends on `standard_conforming_strings`, and the pattern
#: escapes the character itself anyway, so a name containing it is still matched.
LIKE_ESCAPE = "/"

#: Columns that `update(**fields)` may never touch: the identity of a row and its scope.
PROTECTED_COLUMNS = frozenset({"id", "venue_id"})


def _writable(
    model: type[Any],
    fields: Mapping[str, Any],
    *,
    exclude: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Keep the fields that are columns of `model` and are allowed to change."""
    columns = model.__table__.c
    blocked = PROTECTED_COLUMNS | exclude
    return {
        name: value for name, value in fields.items() if name in columns and name not in blocked
    }


def _contains(value: str) -> str:
    """`%value%` with the wildcards of the query itself escaped."""
    escaped = (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


def _folded(value: str) -> str:
    """Case- and space-insensitive form, the same one decision D6 keys a recipe by."""
    return value.strip().casefold()


def _seasonal_first() -> ColumnElement[bool]:
    """TZ 5.5 literally: seasonal positions are shown first in the list.

    `season_date IS NULL` sorted ascending puts `FALSE` — the rows that have a date — at the
    top and lets everything else fall in behind them, without a `CASE` and without assuming
    the column is filled. The date itself is not a sort key: TZ 5.5 binds a seasonal
    position to the date it was introduced on and says nothing about ordering seasonals
    among themselves, so the key that follows (the name) keeps doing that.

    Used by `list_by_category` alone — the module docstring says why search is ordered by
    relevance instead.
    """
    return Recipe.season_date.is_(None).asc()


def _positioned(ingredient: Mapping[str, Any], index: int) -> dict[str, Any]:
    """One ingredient row, keeping the order the caller listed it in (TZ 5.5)."""
    fields = _writable(RecipeIngredient, ingredient, exclude=frozenset({"recipe_id"}))
    fields.setdefault("order_index", index)
    return fields


class RecipeRepo(BaseRepository[Recipe]):
    """`recipes` (TZ 4.6). One of the two tables that may read the shared library."""

    model = Recipe

    async def get(self, recipe_id: int) -> Recipe | None:
        """A card may be opened for a venue row or for a library row (TZ 3.3)."""
        stmt = self.library().where(Recipe.id == recipe_id)
        rows = await self.session.scalars(stmt.execution_options(populate_existing=True))
        return rows.first()

    async def search(self, query: str, *, limit: int, offset: int = 0) -> Sequence[Recipe]:
        """Name, synonyms and typos in one pass, most relevant first (TZ 4.6, 5.5)."""
        cleaned = query.strip()
        if not cleaned:
            return []
        stmt = (
            self.library()
            .where(Recipe.is_active, self._matches(cleaned))
            .order_by(*self._relevance(cleaned))
            .limit(limit)
            .offset(offset)
        )
        rows = await self.session.scalars(stmt)
        return rows.all()

    async def count_search(self, query: str) -> int:
        """Total number of hits, for the pagination of TZ 5.5."""
        cleaned = query.strip()
        if not cleaned:
            return 0
        stmt = (
            self.library()
            .where(Recipe.is_active, self._matches(cleaned))
            .with_only_columns(func.count())
        )
        found = await self.session.scalar(stmt)
        return int(found or 0)

    async def list_by_category(
        self,
        category: str,
        *,
        limit: int,
        offset: int,
    ) -> Sequence[Recipe]:
        """The second entry of TZ 5.5: pick a category instead of typing a name."""
        stmt = (
            self.library()
            .where(
                Recipe.is_active,
                func.lower(func.btrim(Recipe.category)) == _folded(category),
            )
            .order_by(_seasonal_first(), func.lower(Recipe.name), Recipe.id)
            .limit(limit)
            .offset(offset)
        )
        rows = await self.session.scalars(stmt)
        return rows.all()

    async def list_categories(self) -> Sequence[str]:
        """Categories the venue has actually used — empty until it enters its first recipe."""
        stmt = (
            self.library()
            .where(Recipe.is_active)
            .with_only_columns(Recipe.category)
            .distinct()
            .order_by(Recipe.category)
        )
        rows = await self.session.scalars(stmt)
        return rows.all()

    async def create(self, **fields: Any) -> Recipe:
        """Always a row of this venue: the library is filled by BarPoint, not by a bar."""
        recipe = Recipe(venue_id=self.venue_id, **_writable(Recipe, fields))
        self.session.add(recipe)
        await self.session.flush()
        return recipe

    async def update(self, recipe_id: int, **fields: Any) -> Recipe | None:
        """`for_venue()`, not `library()`: editing a library row is question C4."""
        values = _writable(Recipe, fields)
        if not values:
            return await self.get(recipe_id)
        stmt = (
            update(Recipe)
            .where(Recipe.id == recipe_id, Recipe.venue_id == self.venue_id)
            .values(**values)
            .returning(Recipe.id)
            .execution_options(synchronize_session=False)
        )
        changed = await self.session.scalars(stmt)
        if changed.first() is None:
            return None
        return await self.get(recipe_id)

    def _matches(self, query: str) -> ColumnElement[bool]:
        """The predicate of TZ 4.6: name, synonyms, typos."""
        return or_(
            func.lower(func.btrim(Recipe.name)) == _folded(query),
            Recipe.name.ilike(_contains(query), escape=LIKE_ESCAPE),
            func.similarity(Recipe.name, query) >= SIMILARITY_THRESHOLD,
            self._alias_matches(query),
        )

    def _alias_matches(self, query: str) -> ColumnElement[bool]:
        """The same two tests over every element of `aliases` (TZ 4.6).

        The subquery repeats the library scope of the enclosing query rather than
        inheriting it: it is correlated to `recipes`, and a query in this package is
        venue-filtered on its own or not at all (TZ 3.3).
        """
        # `render_derived()` is what makes the column addressable: without it the
        # single output column of `unnest` takes the name of the alias itself.
        alias = func.unnest(Recipe.aliases).table_valued("alias").render_derived()
        hits = (
            select(alias.c.alias)
            .where(or_(Recipe.venue_id == self.venue_id, Recipe.venue_id.is_(None)))
            .where(
                or_(
                    func.lower(func.btrim(alias.c.alias)) == _folded(query),
                    func.similarity(alias.c.alias, query) >= SIMILARITY_THRESHOLD,
                )
            )
            .correlate(Recipe)
        )
        return hits.exists()

    def _relevance(self, query: str) -> tuple[Any, ...]:
        """Exact name, then substring, then closest match — and nothing above them.

        Two ordered booleans rather than a ranking `CASE`: in `CASE WHEN ... THEN :a ELSE
        :b END` every branch is a parameter of no known type, and PostgreSQL has to guess
        one before the driver can send anything. A comparison is a boolean, and `TRUE`
        sorts first under `DESC`.

        Seasonality is deliberately absent: a typed query is a statement about which drink
        is wanted, and no other key may come between it and the answer (module docstring).
        """
        return (
            (func.lower(func.btrim(Recipe.name)) == _folded(query)).desc(),
            Recipe.name.ilike(_contains(query), escape=LIKE_ESCAPE).desc(),
            func.similarity(Recipe.name, query).desc(),
            func.lower(Recipe.name),
            Recipe.id,
        )


class RecipeIngredientRepo(ChildRepository[RecipeIngredient, Recipe]):
    """`recipe_ingredients` — a child of `recipes` (decision D9).

    The only child repository that may use `library()`: the card of a global recipe has to
    render its ingredients (TZ 3.3).
    """

    model = RecipeIngredient
    parent = Recipe
    parent_fk = "recipe_id"

    async def list_for_recipe(self, recipe_id: int) -> Sequence[RecipeIngredient]:
        stmt = (
            self.library()
            .where(RecipeIngredient.recipe_id == recipe_id)
            .order_by(RecipeIngredient.order_index, RecipeIngredient.id)
        )
        rows = await self.session.scalars(stmt)
        return rows.all()

    async def replace_all(
        self,
        recipe_id: int,
        ingredients: Sequence[dict[str, Any]],
    ) -> Sequence[RecipeIngredient]:
        """Rewrite the composition of one recipe of this venue.

        `for_parent()`, not `library()`: a library recipe is read-only until question C4 is
        answered, so its ingredients are not rewritten from a venue either — nothing is
        deleted and nothing is written when the id is not the venue's own.
        """
        stmt = select(Recipe.id).where(Recipe.id == recipe_id, Recipe.venue_id == self.venue_id)
        found = await self.session.scalars(stmt)
        if found.first() is None:
            return []

        removal = (
            delete(RecipeIngredient)
            .where(
                RecipeIngredient.id.in_(
                    self.for_parent(recipe_id).with_only_columns(RecipeIngredient.id)
                )
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(removal)

        rows = [
            RecipeIngredient(recipe_id=recipe_id, **_positioned(ingredient, index))
            for index, ingredient in enumerate(ingredients)
        ]
        if not rows:
            return []
        self.session.add_all(rows)
        await self.session.flush()
        return rows


__all__ = [
    "LIKE_ESCAPE",
    "PROTECTED_COLUMNS",
    "SIMILARITY_THRESHOLD",
    "RecipeIngredientRepo",
    "RecipeRepo",
]
